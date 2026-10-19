"""Dice rolls. A roll is described by a formula such as "1d20"."""

from __future__ import annotations

import random
import re

FORMULA = re.compile(r"([1-9][0-9]?)d([1-9][0-9]*)")


def is_formula(text: object) -> bool:
    return isinstance(text, str) and FORMULA.fullmatch(text) is not None


def roll_total(formula: str, rng: random.Random | None = None) -> int:
    """Roll an NdM formula and return the sum of the dice."""
    match = FORMULA.fullmatch(formula)
    if match is None:
        raise ValueError(f"not a dice formula: {formula!r}")
    count, sides = int(match.group(1)), int(match.group(2))
    generator = rng or random.Random()
    return sum(generator.randint(1, sides) for _ in range(count))
