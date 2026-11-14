"""Traps, falls, fire, poison, and other hazards: the save, the harm, and the condition.

A hazard is not an attack. The GM names the danger and the save and damage the fiction
gives, and the engine rolls every NPC's save and applies what failing costs. A player
character's save is rolled by the player through the player-roll path, so this tool never
rolls for a player character.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from typing import Any

from sqlmodel import Session

from cygnusloom.content.character import CharacterSpec, Feature
from cygnusloom.content.library import CombatRules, ConditionRules
from cygnusloom.content.lookup import AmbiguousId, resolve_id
from cygnusloom.rules import dice
from cygnusloom.services.combat.constants import MAX_CAUSE_TEXT
from cygnusloom.services.combat.damage import _end_if_one_side_left, harm_character
from cygnusloom.services.common import InvalidInput, bounded_text
from cygnusloom.services.conditions import add_condition
from cygnusloom.services.events import PC_KIND, defeat_status, load_state, name_key

# The words that tell a trap-related ability, such as the Trapmaster focus, that the save
# came from a trap or hazard.
_TRAP_WORDS = ("trap", "snare", "hazard")


def _save_stat(save: str, spec: CharacterSpec) -> str:
    """The canonical stat id a hazard save names, accepting a display name and the short
    form `evasion` for `save_evasion`."""
    wanted = save.strip()
    try:
        return resolve_id(spec.stats, wanted, kind="stat", names=lambda rules: (rules.name,))
    except AmbiguousId as exc:
        raise InvalidInput(str(exc)) from exc
    except LookupError:
        pass
    try:
        return resolve_id(
            spec.stats, f"save_{wanted}", kind="stat", names=lambda rules: (rules.name,)
        )
    except AmbiguousId as exc:
        raise InvalidInput(str(exc)) from exc
    except LookupError:
        raise InvalidInput(f"there is no save named {save!r}") from None


def _damage_stat(stat: str, combat: CombatRules, spec: CharacterSpec) -> str:
    """The stat the hazard's damage takes. It defaults to the rule set's first outside-harm
    stat, which is HP for Worlds Without Number and an attribute for Cairn."""
    if not stat:
        if not combat.outside_harm:
            raise InvalidInput(
                "this rule set has no outside-harm stat for hazards; name one with stat"
            )
        return combat.outside_harm[0]
    try:
        resolved = resolve_id(spec.stats, stat, kind="stat", names=lambda rules: (rules.name,))
    except AmbiguousId as exc:
        raise InvalidInput(str(exc)) from exc
    except LookupError:
        raise InvalidInput(f"there is no stat named {stat!r}") from None
    if resolved not in combat.outside_harm:
        options = ", ".join(combat.outside_harm) or "none"
        raise InvalidInput(f"hazard damage can only take {options}, not {resolved!r}")
    return resolved


def _damage_amount(damage: str) -> int | str:
    """A hazard's damage: a dice formula to roll, or a whole number taken as it is."""
    text = damage.strip()
    if not text:
        return ""
    if dice.is_formula(text):
        return text
    if text.isdigit():
        amount = int(text)
        if amount < 1:
            raise InvalidInput("fixed hazard damage must be 1 or more")
        return amount
    raise InvalidInput("hazard damage must be dice such as 1d6, or a whole number of points")


def _hazard_reason(hazard: str, cause: str) -> str:
    """The cause recorded with the save and the harm. A trap-related ability needs the
    reason to say the save came from a trap, snare, or hazard."""
    reason = cause.strip() or hazard
    if not any(word in reason.casefold() for word in _TRAP_WORDS):
        reason = f"{reason} hazard"
    return bounded_text(reason, "cause", MAX_CAUSE_TEXT)


def resolve_hazard(
    db: Session,
    campaign_id: int,
    combat: CombatRules,
    rng: random.Random,
    hazard: str,
    targets: Sequence[str],
    *,
    save: str = "",
    save_modifier: int = 0,
    damage: str = "",
    half_on_save: bool = False,
    condition: str = "",
    stat: str = "",
    cause: str = "",
    spec: CharacterSpec,
    conditions: Mapping[str, ConditionRules] | None = None,
    features: Mapping[str, Feature] | None = None,
    plus: str = "sum",
) -> dict[str, Any]:
    """Resolve one hazard against its NPC victims. Each target saves with the engine, then
    takes the damage and condition the result calls for. A player character is rolled by the
    player instead, so naming one here is refused with what to call first."""
    name = bounded_text(hazard, "hazard", MAX_CAUSE_TEXT)
    names = [target for target in targets if isinstance(target, str) and target.strip()]
    if not names:
        raise InvalidInput("a hazard needs at least one target")
    if len(names) != len({name_key(target) for target in names}):
        raise InvalidInput("a hazard cannot name the same target twice")
    save_stat = _save_stat(save, spec) if save.strip() else ""
    if half_on_save and not save_stat:
        raise InvalidInput("half_on_save needs a save")
    if half_on_save and not damage.strip():
        raise InvalidInput("half_on_save needs damage")
    amount = _damage_amount(damage)
    if not save_stat and not amount and not condition.strip():
        raise InvalidInput("a hazard needs a save, damage, or a condition")
    if (
        isinstance(save_modifier, bool)
        or not isinstance(save_modifier, int)
        or not (-20 <= save_modifier <= 20)
    ):
        raise InvalidInput("the save modifier must be a whole number from -20 to 20")
    if condition.strip() and (conditions is None or condition.strip() not in conditions):
        raise InvalidInput(f"there is no condition named {condition.strip()!r}")
    harm_stat = _damage_stat(stat, combat, spec) if (amount or condition.strip()) else ""
    reason = _hazard_reason(name, cause)

    state = load_state(db, campaign_id)
    victims = []
    for target in names:
        character = state.characters.get(name_key(target))
        if character is None:
            raise InvalidInput(f"there is no character named {target!r}")
        if character.kind == PC_KIND:
            raise InvalidInput(
                f"{character.name} is a player character; roll their own save with the "
                "rule set's player-save tool, then apply what the result costs with "
                "harm_character"
            )
        if character.story_only:
            raise InvalidInput(
                f"{character.name} has no stats yet; give them stats with update_npc first"
            )
        victims.append(character)
    results: list[dict[str, Any]] = []
    for character in victims:
        if character.defeated or character.fled or character.surrendered:
            results.append(
                {
                    "name": character.name,
                    "skipped": f"already {defeat_status(character, combat.defeat)}",
                }
            )
            continue
        result: dict[str, Any] = {"name": character.name}
        passed = False
        if save_stat:
            from cygnusloom.services import magic as magic_service

            saved = magic_service.roll_save(
                db,
                campaign_id,
                combat,
                spec,
                rng,
                character.name,
                save_stat,
                reason,
                plus=plus,
                features=features,
                modifier=save_modifier,
            )
            passed = bool(saved["passed"])
            result["save"] = {
                "stat": saved["stat"],
                "roll": saved["roll"],
                "score": saved["score"],
                "passed": passed,
            }
        taken = 0
        if amount and not (passed and not half_on_save):
            if isinstance(amount, int):
                rolled = amount
            else:
                rolled = dice.roll_total(str(amount), rng, plus=plus)
            taken = rolled
            if passed and half_on_save:
                taken = rolled // 2
            if taken:
                harmed = harm_character(
                    db,
                    campaign_id,
                    character.name,
                    harm_stat,
                    taken,
                    reason,
                    combat,
                    spec=spec,
                )
                result["harm"] = {
                    "stat": harm_stat,
                    "rolled": rolled,
                    "taken": harmed["taken"],
                    "current": harmed["current"],
                    "defeated": harmed["defeated"],
                }
                if harmed["defeated"]:
                    updated = load_state(db, campaign_id).characters[name_key(character.name)]
                    result["status"] = defeat_status(updated, combat.defeat)
            else:
                result["harm"] = {"stat": harm_stat, "rolled": rolled, "taken": 0}
        if condition.strip() and not passed:
            add_condition(
                db,
                campaign_id,
                character.name,
                condition.strip(),
                reason,
                conditions or {},
                combat,
            )
            result["condition"] = condition.strip()
        results.append(result)

    had_encounter = load_state(db, campaign_id).encounter is not None
    _end_if_one_side_left(db, campaign_id)
    ended = had_encounter and load_state(db, campaign_id).encounter is None
    return {"hazard": name, "results": results, "encounter_ended": ended}
