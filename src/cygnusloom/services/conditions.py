"""Conditions a character has. A stored condition is recorded on the character and ends when a
trigger the rule set names happens. A derived condition holds while the character's stats say so,
and has nothing stored to end."""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence

from sqlmodel import Session

from cygnusloom.content.character import CharacterSpec
from cygnusloom.content.library import ALWAYS_CONDITION_END, CombatRules, ConditionRules
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import (
    Character,
    ConditionEnded,
    current,
    load_state,
    name_key,
    record,
)
from cygnusloom.services.injuries import EffectContext, gain_condition, run_effects


def derived_holds(character: Character, rules: ConditionRules) -> bool:
    """Whether a derived condition holds: its stat is at the set score and, if it needs a maximum,
    the stat's maximum is above 0."""
    derived = rules.derived
    assert derived is not None
    if current(character, derived.stat) != derived.current:
        return False
    return not derived.needs_max or character.stats.get(derived.stat, 0) > 0


def active_conditions(
    character: Character, conditions: Mapping[str, ConditionRules]
) -> list[ConditionRules]:
    """The conditions a character has now: the stored ones in the order gained, then the derived
    ones that hold."""
    stored = [conditions[cid] for cid in character.conditions if cid in conditions]
    derived = [
        rules
        for rules in conditions.values()
        if rules.derived is not None and derived_holds(character, rules)
    ]
    return stored + derived


def add_condition(
    db: Session,
    campaign_id: int,
    name: str,
    condition: str,
    cause: str,
    conditions: Mapping[str, ConditionRules],
    combat: CombatRules | None,
) -> dict[str, object]:
    """Give a character a stored condition, recording `cause`. A condition that is already held is
    left as it is."""
    state = load_state(db, campaign_id)
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    rules = conditions.get(condition)
    if rules is None:
        raise InvalidInput(f"there is no condition named {condition!r}")
    if rules.derived is not None:
        raise InvalidInput(f"{rules.label} holds while the stats say so and cannot be added")
    gain_condition(db, campaign_id, character.name, condition, cause, combat)
    db.commit()
    return {"character": character.name, "condition": condition}


def end_condition(
    db: Session,
    campaign_id: int,
    name: str,
    condition: str,
    how: str,
    combat: CombatRules | None,
    conditions: Mapping[str, ConditionRules],
    rng: random.Random,
    *,
    plus: str,
    spec: CharacterSpec | None = None,
    force: bool = False,
    condition_ends: Sequence[str] = (),
) -> dict[str, object]:
    """End a stored condition. `how` is the trigger that ends it, which must be one the condition
    lists, or `gm` for the GM ending it by decision. `condition_ends` is the rule set's trigger
    vocabulary, checked alongside the condition's own list; a trigger the rule set does not name
    is refused even when it appears in the condition. `force` ends the condition for an effect
    that names it directly, with `how` used only as the Log's explanation. Then the condition's
    on-end effects run."""
    state = load_state(db, campaign_id)
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    rules = conditions.get(condition)
    if rules is None:
        raise InvalidInput(f"there is no condition named {condition!r}")
    if rules.derived is not None:
        raise InvalidInput(f"{rules.label} holds while the stats say so and cannot be ended")
    if condition not in character.conditions:
        raise InvalidInput(f"{character.name} does not have the condition {rules.label}")
    if not force:
        if condition_ends and how != ALWAYS_CONDITION_END and how not in condition_ends:
            options = ", ".join(condition_ends) or "none"
            raise InvalidInput(
                f"{how!r} is not a condition-ending trigger; the rule set has {options}"
            )
        if how != ALWAYS_CONDITION_END and how not in rules.ends:
            allowed = ", ".join(rules.ends) or "none"
            raise InvalidInput(f"{rules.label} ends by {allowed}, not by {how}")
    record(db, campaign_id, ConditionEnded(name=character.name, condition=condition, how=how))
    db.commit()
    run_effects(
        db,
        campaign_id,
        character.name,
        rules.on_end,
        EffectContext(rng=rng, combat=combat, spec=spec),
        f"{rules.label} ended",
        plus=plus,
    )
    return {"character": character.name, "condition": condition, "ended_by": how}
