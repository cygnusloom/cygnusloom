"""Checks a rule set rolls outside its moves, such as a skill check: the check's dice, plus the
skill's level, the stat's modifier, and any situational modifier, against a difficulty. The dice,
the untrained penalty, and the difficulty bounds all come from the rule set's check, so nothing
here names a particular game."""

from __future__ import annotations

import random
from collections.abc import Mapping
from typing import Any

from sqlmodel import Session

from cygnusforge.content.character import CharacterSpec
from cygnusforge.content.library import ALL_STATS, CheckRules, ConditionRules
from cygnusforge.services import resolution
from cygnusforge.services.common import InvalidInput, whole_number
from cygnusforge.services.conditions import active_conditions
from cygnusforge.services.events import CheckMade, load_state, name_key, record


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
) -> dict[str, Any]:
    """Roll `check` for a character and record what it made. The roll is the check's dice plus the
    skill's level (or the check's untrained penalty when the character has no level in it), the
    stat's modifier, `modifier`, and any penalty or bonus the character's conditions add to a
    check; it succeeds at or over the difficulty. A missing difficulty is the check's default,
    and any difficulty is clamped to the check's bounds.

    `skill` or `stat` may be empty for a roll that adds neither."""
    found = load_state(db, campaign_id).characters.get(name_key(character))
    if found is None:
        raise InvalidInput(f"there is no character named {character!r}")
    skill_id = skill.strip()
    skill_name = ""
    skill_level = 0
    skill_bonus = 0
    if skill_id:
        skill_rules = spec.skills.get(skill_id)
        if skill_rules is None:
            options = ", ".join(sorted(spec.skills)) or "none"
            raise InvalidInput(
                f"there is no skill named {skill_id!r}; the rule set's skills are: {options}"
            )
        skill_name = skill_rules.name
        skill_level = spec.skill_level(found.skills, skill_id)
        skill_bonus = skill_level if spec.skill_trained(found.skills, skill_id) else check.unskilled
    stat_id = stat.strip()
    stat_name = ""
    stat_bonus = 0
    if stat_id:
        stat_rules = spec.stats.get(stat_id)
        if stat_rules is None or stat_id not in found.stats:
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
    result = resolution.roll_check(
        rng,
        check.dice,
        skill_level=skill_bonus,
        stat_modifier=stat_bonus,
        difficulty=target,
        modifier=modifier + condition_modifier + armor_penalty,
        plus=plus,
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
    }
