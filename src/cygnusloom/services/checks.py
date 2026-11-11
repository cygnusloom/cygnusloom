"""Checks a rule set rolls outside its moves, such as a skill check: the check's dice, plus the
skill's level, the stat's modifier, and any situational modifier, against a difficulty. The dice,
the untrained penalty, and the difficulty bounds all come from the rule set's check, so nothing
here names a particular game."""

from __future__ import annotations

import random
from collections.abc import Mapping
from typing import Any

from sqlmodel import Session

from cygnusloom.content.character import CharacterSpec, Feature
from cygnusloom.content.library import ALL_STATS, CheckRules, ConditionRules
from cygnusloom.content.lookup import AmbiguousId, resolve_id
from cygnusloom.services import reactions, resolution
from cygnusloom.services.common import InvalidInput, whole_number
from cygnusloom.services.conditions import active_conditions
from cygnusloom.services.events import CheckMade, load_state, name_key, record


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
        stat_bonus = spec.modifier(found.stats, stat_id)
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
    if roll is None:
        result = resolution.roll_check(
            rng,
            check.dice,
            skill_level=skill_bonus,
            stat_modifier=stat_bonus,
            difficulty=target,
            modifier=modifier + condition_modifier + armor_penalty,
            plus=plus,
        )
    else:
        rolled = whole_number(roll, "roll", 0, 1000)
        total = rolled + bonus
        result = resolution.CheckResult(
            roll=rolled, total=total, difficulty=target, passed=total >= target
        )
    triggered = reactions.match_check(
        found, spec, features, check, skill_id, stat_id, result.total, result.passed
    )
    if triggered is not None:
        effect = triggered.reaction.effect
        if effect["op"] == "reroll":
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
            armor_penalty=armor_penalty,
        ),
    )
    if triggered is not None:
        reactions.use(db, campaign_id, found, triggered)
    db.commit()
    return {
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
        **({"reaction": triggered.text} if triggered is not None else {}),
    }
