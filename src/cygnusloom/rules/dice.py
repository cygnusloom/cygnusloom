"""Dice rolls. A formula is one or more terms joined by "+", such as "1d20" or "2d6+1d4". What
"+" means between terms belongs to the rule set: "sum" adds every term, and "highest" rolls each
term and keeps the higher total, such as "1d8+1d8" for two weapons."""

from __future__ import annotations

import random
import re

# A single term such as "3d6". Stats and other ranges need exactly one term.
FORMULA = re.compile(r"([1-9][0-9]?)d([1-9][0-9]*)")
TERM = r"[1-9][0-9]?d[1-9][0-9]*"
# A formula starts with a die. Later terms are dice or flat bonuses and penalties, such as
# "2d6+1d4+2" or "1d6-1".
ROLL = re.compile(rf"{TERM}(?:(?:[+-]{TERM})|(?:[+-][0-9]+))*")
TERM_PATTERN = re.compile(r"([+-]?)(?:([1-9][0-9]?)d([1-9][0-9]*)|([0-9]+))")
PLUS_MODES = ("sum", "highest")
# How a d20-style roll treats a second die: normal keeps one, advantage keeps the higher total,
# and disadvantage keeps the lower.
ROLL_MODES = ("normal", "advantage", "disadvantage")


def is_formula(text: object) -> bool:
    return isinstance(text, str) and ROLL.fullmatch(text) is not None


def roll_total(formula: str, rng: random.Random | None = None, *, plus: str | None = None) -> int:
    """Roll a formula. A single term is the same in every rule set. A formula with several terms
    needs the rule set's `plus` mode, which says whether "+" sums the terms or keeps the highest.
    Flat bonuses and penalties always add, whatever the mode."""
    if not is_formula(formula):
        raise ValueError(f"not a dice formula: {formula!r}")
    generator = rng or random.Random()
    dice, flat = _terms(formula)
    if len(dice) == 1:
        return _roll_term(dice[0], generator) + flat
    if plus is None:
        raise ValueError(f"{formula!r} has several terms, so the rule set's plus mode is needed")
    if plus not in PLUS_MODES:
        raise ValueError(f"unknown plus mode: {plus!r}")
    totals = [_roll_term(term, generator) for term in dice]
    kept = max(totals) if plus == "highest" else sum(totals)
    return kept + flat


def roll_values(
    formula: str, rng: random.Random | None = None, *, plus: str | None = None
) -> tuple[list[int], int]:
    """Roll a formula and return every die value in order, plus the total `roll_total` gives.
    The values include dice a `highest` mode did not keep, so a caller can reroll 1s."""
    if not is_formula(formula):
        raise ValueError(f"not a dice formula: {formula!r}")
    generator = rng or random.Random()
    dice, flat = _terms(formula)
    values: list[int] = []
    totals: list[int] = []
    for count, sides in dice:
        rolled = [generator.randint(1, sides) for _ in range(count)]
        values.extend(rolled)
        totals.append(sum(rolled))
    if len(dice) == 1:
        return values, totals[0] + flat
    if plus is None:
        raise ValueError(f"{formula!r} has several terms, so the rule set's plus mode is needed")
    if plus not in PLUS_MODES:
        raise ValueError(f"unknown plus mode: {plus!r}")
    kept = max(totals) if plus == "highest" else sum(totals)
    return values, kept + flat


def roll_mode_values(
    formula: str,
    rng: random.Random | None = None,
    *,
    plus: str | None = None,
    mode: str = "normal",
) -> tuple[list[int], int]:
    """Roll a formula under a roll mode and return the kept dice and their total. Advantage and
    disadvantage roll the formula twice and keep the higher or lower total; the discarded dice
    are left out of the returned values, so their sum matches the total when the formula is one
    die."""
    if mode not in ROLL_MODES:
        raise ValueError(f"unknown roll mode: {mode!r}")
    values, total = roll_values(formula, rng, plus=plus)
    if mode == "normal":
        return values, total
    other_values, other = roll_values(formula, rng, plus=plus)
    keep_other = other > total if mode == "advantage" else other < total
    return (other_values, other) if keep_other else (values, total)


def max_total(formula: str, plus: str | None = None) -> int:
    """The highest total a formula can roll. A multi-term formula uses the same `plus` mode as
    `roll_total`; flat bonuses and penalties always add."""
    if not is_formula(formula):
        raise ValueError(f"not a dice formula: {formula!r}")
    dice, flat = _terms(formula)
    if len(dice) == 1:
        count, sides = dice[0]
        return count * sides + flat
    if plus is None:
        raise ValueError(f"{formula!r} has several terms, so the rule set's plus mode is needed")
    if plus not in PLUS_MODES:
        raise ValueError(f"unknown plus mode: {plus!r}")
    totals = [count * sides for count, sides in dice]
    kept = max(totals) if plus == "highest" else sum(totals)
    return kept + flat


def _terms(formula: str) -> tuple[list[tuple[int, int]], int]:
    """The dice terms of a formula as (count, sides), and the flat total of its bonuses."""
    dice: list[tuple[int, int]] = []
    flat = 0
    for sign, count, sides, number in TERM_PATTERN.findall(formula):
        amount = -1 if sign == "-" else 1
        if number:
            flat += amount * int(number)
        else:
            dice.append((int(count), int(sides)))
    return dice, flat


def _roll_term(term: tuple[int, int], generator: random.Random) -> int:
    count, sides = term
    return sum(generator.randint(1, sides) for _ in range(count))
