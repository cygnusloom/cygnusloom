"""Checks a rule set rolls outside its moves, such as a skill check: the check's dice, plus the
skill's level, the stat's modifier, and any situational modifier, against a difficulty. The dice,
the untrained penalty, and the difficulty bounds all come from the rule set's check, so nothing
here names a particular game."""

from __future__ import annotations

import random
import re
from collections.abc import Mapping
from typing import Any

from sqlmodel import Session

from cygnusloom.content.character import CharacterSpec, Feature
from cygnusloom.content.library import ALL_STATS, CheckRules, ConditionRules
from cygnusloom.content.lookup import AmbiguousId, resolve_id
from cygnusloom.rules.dice import roll_values
from cygnusloom.services import reactions, resolution
from cygnusloom.services.common import InvalidInput, whole_number
from cygnusloom.services.conditions import active_conditions
from cygnusloom.services.events import CheckMade, load_state, name_key, record, roll_breakdown


def resolve_skill(spec: CharacterSpec, skill: str) -> str:
    """The rule set's canonical skill id for a loose id or display name, or "" when none is
    named. An exact id wins; an ambiguous display name is an error."""
    wanted = skill.strip()
    if not wanted:
        return ""
    try:
        return resolve_id(spec.skills, wanted, kind="skill", names=lambda rules: (rules.name,))
    except AmbiguousId as exc:
        raise InvalidInput(str(exc)) from exc
    except LookupError:
        options = ", ".join(sorted(spec.skills)) or "none"
        raise InvalidInput(
            f"there is no skill named {wanted!r}; the rule set's skills are: {options}"
        ) from None


def resolve_stat(spec: CharacterSpec, stat: str) -> str:
    """The rule set's canonical stat id for a loose id or display name, or "" when none is
    named. An exact id wins; an ambiguous display name is an error."""
    wanted = stat.strip()
    if not wanted:
        return ""
    try:
        return resolve_id(spec.stats, wanted, kind="stat", names=lambda rules: (rules.name,))
    except AmbiguousId as exc:
        raise InvalidInput(str(exc)) from exc
    except LookupError:
        options = ", ".join(sorted(spec.stats)) or "none"
        raise InvalidInput(
            f"there is no stat named {wanted!r}; the rule set's stats are: {options}"
        ) from None


def _check_reaction_pending(
    name: str,
    triggered: Any,
    check: CheckRules,
    skill: str,
    skill_name: str,
    skill_level: int,
    stat: str,
    stat_name: str,
    result: Any,
    bonus: int,
    difficulty: int,
    cause: str,
    armor_penalty: int,
    *,
    dice: str,
    ones: list[int] | None = None,
) -> dict[str, Any]:
    """What the player's roll prompt needs to finish a reroll reaction."""
    effect = triggered.reaction.effect
    return {
        "kind": "check",
        "name": name,
        "feature": triggered.feature.id,
        "feature_name": triggered.feature.name,
        "reaction": triggered.reaction.id,
        "shared": triggered.reaction.shared,
        "reset": triggered.reaction.reset,
        "uses": triggered.reaction.uses,
        "text": triggered.text,
        "sheet": triggered.reaction.sheet or triggered.text,
        "optional": triggered.reaction.optional,
        "effect": effect["op"],
        "keep": effect.get("keep", "better"),
        "dice": dice,
        "ones": list(ones or []),
        "check": check.label,
        "skill": skill,
        "skill_name": skill_name,
        "skill_level": skill_level,
        "stat": stat,
        "stat_name": stat_name,
        "bonus": bonus,
        "difficulty": difficulty,
        "original_roll": result.roll,
        "original_total": result.total,
        "original_success": result.passed,
        "cause": cause,
        "armor_penalty": armor_penalty,
    }


def _best_of_die(dice: str) -> str:
    """The single die a best-of check rolls several times, such as "1d6" for a "2d6" check.
    Empty when the check's dice are not a simple handful of one die."""
    match = re.fullmatch(r"\s*(?:\d+)?d(\d+)\s*", dice)
    return f"1d{match.group(1)}" if match else ""


def roll_check(
    db: Session,
    campaign_id: int,
    character: str,
    check: CheckRules,
    spec: CharacterSpec,
    rng: random.Random,
    *,
    plus: str,
    skill: str,
    stat: str,
    difficulty: int | None = None,
    modifier: int = 0,
    cause: str = "",
    conditions: Mapping[str, ConditionRules] | None = None,
    armor_penalty: int = 0,
    features: Mapping[str, Feature] | None = None,
    roll: int | None = None,
    dice_values: list[int] | None = None,
    use_optional: bool = True,
    player_rolls_reaction: bool = False,
) -> dict[str, Any]:
    """Roll `check` for a character and record what it made. The roll is the check's dice plus the
    skill's level (or the check's untrained penalty when the character has no level in it), the
    stat's modifier, `modifier`, and any penalty or bonus the character's conditions add to a
    check; it succeeds at or over the difficulty. A missing difficulty is the check's default,
    and any difficulty is clamped to the check's bounds.

    `skill` or `stat` may be empty for a roll that adds neither. `roll` is the dice total a
    player reported; when it is None the engine rolls the check's dice. `features` are a world's
    own feature definitions, in addition to the rule set's, for reactions."""
    found = load_state(db, campaign_id).characters.get(name_key(character))
    if found is None:
        raise InvalidInput(f"there is no character named {character!r}")
    skill_id = resolve_skill(spec, skill)
    skill_name = ""
    skill_level = 0
    skill_bonus = 0
    if skill_id:
        skill_rules = spec.skills[skill_id]
        skill_name = skill_rules.name
        skill_level = spec.skill_level(found.skills, skill_id)
        skill_bonus = skill_level if spec.skill_trained(found.skills, skill_id) else check.unskilled
    stat_id = resolve_stat(spec, stat)
    stat_name = ""
    stat_bonus = 0
    if stat_id:
        stat_rules = spec.stats[stat_id]
        if stat_id not in found.stats:
            options = ", ".join(sorted(found.stats)) or "none"
            raise InvalidInput(
                f"{found.name} has no stat {stat_id!r}; the character's stats are: {options}"
            )
        stat_name = stat_rules.name
        stat_bonus = found.modifier(spec, stat_id)
    target = check.default_difficulty if difficulty is None else difficulty
    if isinstance(target, bool) or not isinstance(target, int):
        raise InvalidInput("difficulty must be a whole number")
    target = min(max(target, check.minimum_difficulty), check.maximum_difficulty)
    # The check gives no bounds on a situational modifier, so it may not swing a total by more
    # than the hardest difficulty the check allows.
    modifier = whole_number(
        modifier, "modifier", -check.maximum_difficulty, check.maximum_difficulty
    )
    condition_modifier = sum(
        rules.modifies.get("check", 0) + rules.modifies.get(ALL_STATS, 0)
        for rules in active_conditions(found, conditions or {})
    )
    bonus = skill_bonus + stat_bonus + modifier + condition_modifier + armor_penalty
    rolled_values: list[int] = list(dice_values or [])
    best = reactions.check_best(found, spec, features, skill_id) if skill_id else None
    best_die = _best_of_die(check.dice) if best is not None else ""
    if roll is None and best is not None and best_die:
        dice, keep = best
        rolled = sorted(
            (resolution.roll_die(rng, best_die, plus=plus) for _ in range(dice)), reverse=True
        )
        rolled_values = rolled
        kept = sum(rolled[:keep])
        result = resolution.CheckResult(
            roll=kept, total=kept + bonus, difficulty=target, passed=kept + bonus >= target
        )
    elif roll is None:
        rolled_values, rolled = roll_values(check.dice, rng, plus=plus)
        result = resolution.CheckResult(
            roll=rolled,
            total=rolled + bonus,
            difficulty=target,
            passed=rolled + bonus >= target,
        )
    else:
        rolled = whole_number(roll, "roll", 0, 1000)
        if not rolled_values and re.fullmatch(r"\s*(?:1)?d\d+\s*", check.dice):
            # A single die's reported face is the whole roll, so the Log can still show the dice.
            rolled_values = [rolled]
        total = rolled + bonus
        result = resolution.CheckResult(
            roll=rolled, total=total, difficulty=target, passed=total >= target
        )
    triggered = reactions.match_check(
        found,
        spec,
        features,
        check,
        skill_id,
        stat_id,
        result.total,
        result.passed,
        cause,
        use_optional,
    )
    pending: dict[str, Any] | None = None
    if triggered is not None:
        effect = triggered.reaction.effect
        if player_rolls_reaction and effect["op"] == "reroll":
            pending = _check_reaction_pending(
                found.name,
                triggered,
                check,
                skill_id,
                skill_name,
                skill_level,
                stat_id,
                stat_name,
                result,
                bonus,
                target,
                cause,
                armor_penalty,
                dice=check.dice,
            )
        elif player_rolls_reaction and effect["op"] == "reroll_ones":
            die = _best_of_die(check.dice)
            ones = [value for value in rolled_values if value == 1] if die else []
            if ones:
                sides = int(die[2:])
                pending = _check_reaction_pending(
                    found.name,
                    triggered,
                    check,
                    skill_id,
                    skill_name,
                    skill_level,
                    stat_id,
                    stat_name,
                    result,
                    bonus,
                    target,
                    cause,
                    armor_penalty,
                    dice=f"{len(ones)}d{sides}",
                    ones=ones,
                )
            else:
                # No 1 was rolled, so there is nothing to reroll.
                triggered = None
        elif effect["op"] == "reroll":
            rerolled = resolution.roll_check(
                rng,
                check.dice,
                skill_level=skill_bonus,
                stat_modifier=stat_bonus,
                difficulty=target,
                modifier=modifier + condition_modifier + armor_penalty,
                plus=plus,
            )
            keep = "better" if effect.get("keep", "better") == "better" else "worse"
            better = (rerolled.passed and not result.passed) or (
                rerolled.passed == result.passed and rerolled.total > result.total
            )
            worse = (result.passed and not rerolled.passed) or (
                rerolled.passed == result.passed and rerolled.total < result.total
            )
            if (keep == "better" and better) or (keep == "worse" and worse):
                result = rerolled
        elif effect["op"] == "add_total":
            total = result.total + int(effect["amount"])
            result = resolution.CheckResult(
                roll=result.roll, total=total, difficulty=target, passed=total >= target
            )
        elif effect["op"] == "reroll_ones":
            die = _best_of_die(check.dice)
            if die and rolled_values:
                sides = int(die[2:])
                rerolled_values = [
                    rng.randint(1, sides) if value == 1 else value for value in rolled_values
                ]
                rerolled = sum(rerolled_values)
                result = resolution.CheckResult(
                    roll=rerolled,
                    total=rerolled + bonus,
                    difficulty=target,
                    passed=rerolled + bonus >= target,
                )
            else:
                # Without the individual dice there is no honest way to reroll only the 1s.
                triggered = None
    record(
        db,
        campaign_id,
        CheckMade(
            name=found.name,
            check=check.label,
            skill=skill_id,
            skill_name=skill_name,
            skill_level=skill_level,
            skill_bonus=skill_bonus,
            stat=stat_id,
            stat_name=stat_name,
            roll=result.roll,
            bonus=bonus,
            total=result.total,
            difficulty=target,
            success=result.passed,
            cause=cause,
            stat_bonus=stat_bonus,
            condition_modifier=condition_modifier,
            situational_modifier=modifier,
            dice=check.dice,
            rolls=rolled_values,
            breakdown=roll_breakdown(
                check.dice,
                rolled_values,
                [
                    (skill_name, skill_bonus) if skill_name else ("", 0),
                    (stat_name, stat_bonus) if stat_name else ("", 0),
                    ("situational", modifier),
                    ("conditions", condition_modifier),
                    ("armor", armor_penalty),
                ],
                result.total,
                target=target,
                target_label="difficulty",
                note="the player's roll" if roll is not None else "",
            ),
            armor_penalty=armor_penalty,
            # A limited reaction records its use on its own event, which the Log shows. A
            # reaction waiting for the player's own roll adds its text to that roll instead.
            reaction=(
                triggered.text
                if triggered is not None and pending is None and triggered.reaction.uses <= 0
                else ""
            ),
        ),
    )
    if triggered is not None and pending is None:
        reactions.use(db, campaign_id, found, triggered)
    db.commit()
    payload: dict[str, Any] = {
        "name": found.name,
        "check": check.label,
        "skill": skill_id,
        "skill_name": skill_name,
        "skill_level": skill_level,
        "stat": stat_id,
        "stat_name": stat_name,
        "roll": result.roll,
        "bonus": bonus,
        "total": result.total,
        "difficulty": target,
        "success": result.passed,
        "armor_penalty": armor_penalty,
        "cause": cause,
        **({"reaction": triggered.text} if triggered is not None and pending is None else {}),
    }
    if pending is not None:
        pending["initial"] = dict(payload)
        payload["reaction_pending"] = pending
    return payload
