"""One resolution path for every roll against a target. A save, check, morale check, initiative
roll, and retreat roll all ask the same question: did this roll pass? A score answers it under the
rule set's mode, "roll_under" or "roll_over"; a difficulty answers it after skill, stat, and
situational bonuses are added. Nothing here names a game or a rule set; the die, mode, and numbers
arrive as arguments.

A non-integer number raises `TypeError`; a malformed formula or an unknown mode raises
`ValueError`. Either is a rule set or caller mistake, not a value a player submitted; tools that
accept player input validate it first.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from cygnusloom.rules.dice import is_formula, roll_total

ROLL_UNDER = "roll_under"
ROLL_OVER = "roll_over"
ROLL_MODES = (ROLL_UNDER, ROLL_OVER)


@dataclass(frozen=True)
class RollResult:
    """A roll compared with a score. `passed` is the outcome. `automatic` is true when an
    automatic success or failure decided it rather than the comparison."""

    roll: int
    score: int
    passed: bool
    automatic: bool = False


@dataclass(frozen=True)
class CheckResult:
    """A roll plus its bonuses compared with a difficulty. `margin` is the total minus the
    difficulty, so it is negative when the check fell short."""

    roll: int
    total: int
    difficulty: int
    passed: bool

    @property
    def margin(self) -> int:
        """How far the total cleared the difficulty, negative when it fell short."""
        return self.total - self.difficulty


def roll_die(rng: random.Random, die: str, *, plus: str = "sum") -> int:
    """Roll a dice formula. `plus` is the rule set's meaning for "+" between terms: "sum" adds
    them, and "highest" keeps the higher total. A single term needs no mode, and flat bonuses
    always add."""
    _formula(die, "die")
    return roll_total(die, rng, plus=plus)


def roll_score(
    rng: random.Random,
    die: str,
    score: int,
    *,
    mode: str = ROLL_UNDER,
    plus: str = "sum",
    auto_success: int | None = None,
    auto_fail: int | None = None,
) -> RollResult:
    """Roll `die` and compare it with `score`. Under "roll_under" a roll passes at or under the
    score; under "roll_over" it passes at or over it, so equality passes both ways.

    `auto_success` and `auto_fail` are rolls that always pass or fail. Automatic success is
    checked first, so it wins when both name the same roll, matching the combat save rules."""
    _mode(mode)
    _whole(score, "score")
    _optional_whole(auto_success, "auto_success")
    _optional_whole(auto_fail, "auto_fail")
    roll = roll_die(rng, die, plus=plus)
    return resolve_roll(roll, score, mode=mode, auto_success=auto_success, auto_fail=auto_fail)


def resolve_roll(
    roll: int,
    score: int,
    *,
    mode: str = ROLL_UNDER,
    auto_success: int | None = None,
    auto_fail: int | None = None,
) -> RollResult:
    """Compare a roll already made with `score`, such as a roll the player reported. The rules are
    `roll_score`'s without the dice: equality passes under both modes, and automatic success is
    checked before automatic failure."""
    _whole(roll, "roll")
    _whole(score, "score")
    _mode(mode)
    _optional_whole(auto_success, "auto_success")
    _optional_whole(auto_fail, "auto_fail")
    if auto_success is not None and roll == auto_success:
        passed = True
        automatic = True
    elif auto_fail is not None and roll == auto_fail:
        passed = False
        automatic = True
    else:
        passed = roll <= score if mode == ROLL_UNDER else roll >= score
        automatic = False
    return RollResult(roll=roll, score=score, passed=passed, automatic=automatic)


def roll_check(
    rng: random.Random,
    dice: str,
    *,
    skill_level: int,
    stat_modifier: int,
    difficulty: int,
    modifier: int = 0,
    plus: str = "sum",
) -> CheckResult:
    """Roll `dice` and add the skill level, stat modifier, and situational `modifier`. The check
    passes when the total reaches `difficulty`, and an equal total passes. A level or modifier may
    be negative.

    Difficulty bounds belong to the caller: this primitive uses the difficulty it is given, so a
    rule set clamps it to its own minimum and maximum, or supplies the check's default, first."""
    _whole(skill_level, "skill_level")
    _whole(stat_modifier, "stat_modifier")
    _whole(difficulty, "difficulty")
    _whole(modifier, "modifier")
    roll = roll_die(rng, dice, plus=plus)
    total = roll + skill_level + stat_modifier + modifier
    return CheckResult(roll=roll, total=total, difficulty=difficulty, passed=total >= difficulty)


def _mode(mode: object) -> None:
    if mode not in ROLL_MODES:
        raise ValueError(f"unknown roll mode: {mode!r}")


def _formula(value: object, label: str) -> None:
    if not is_formula(value):
        raise ValueError(f"{label} must be a dice formula such as '1d20', not {value!r}")


def _whole(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be a whole number, not {value!r}")


def _optional_whole(value: object, label: str) -> None:
    if value is not None:
        _whole(value, label)
