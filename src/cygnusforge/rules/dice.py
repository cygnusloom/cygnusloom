"""Dice rolls. A roll is described by a formula such as "1d20" or "1d8+1d8", which rolls each
term and adds the results together."""

from __future__ import annotations

import random
import re

# A single term such as "3d6". Stats and other ranges need exactly one term.
FORMULA = re.compile(r"([1-9][0-9]?)d([1-9][0-9]*)")
# One or more terms joined by "+", such as "2d6+1d4" or the "d6+d6" written in Cairn's bestiary.
ROLL = re.compile(r"[1-9][0-9]?d[1-9][0-9]*(?:\+[1-9][0-9]?d[1-9][0-9]*)*")


def is_formula(text: object) -> bool:
    return isinstance(text, str) and ROLL.fullmatch(text) is not None


def roll_total(formula: str, rng: random.Random | None = None) -> int:
    """Roll each term of a formula and return the sum of all the dice."""
    if not is_formula(formula):
        raise ValueError(f"not a dice formula: {formula!r}")
    generator = rng or random.Random()
    total = 0
    for term in formula.split("+"):
        count, sides = (int(part) for part in term.split("d"))
        total += sum(generator.randint(1, sides) for _ in range(count))
    return total
