"""Dice rolls. A roll is described by a formula such as "1d20", "1d8+1d8", or "1d6|1d6". Terms
joined by "+" are each rolled and summed. Terms joined by "|" are each rolled and the highest is
kept, as with two weapons. The two separators are never mixed in one formula."""

from __future__ import annotations

import random
import re

# A single term such as "3d6". Stats and other ranges need exactly one term.
FORMULA = re.compile(r"([1-9][0-9]?)d([1-9][0-9]*)")
TERM = r"[1-9][0-9]?d[1-9][0-9]*"
# One or more terms joined by "+", such as "2d6+1d4" or the "d6+d6" written in Cairn's bestiary,
# or two or more terms joined by "|", such as "1d6|1d6" for two weapons, where the higher is kept.
ROLL = re.compile(rf"{TERM}(?:\+{TERM})*|{TERM}(?:\|{TERM})+")


def is_formula(text: object) -> bool:
    return isinstance(text, str) and ROLL.fullmatch(text) is not None


def roll_total(formula: str, rng: random.Random | None = None) -> int:
    """Roll each term of a formula. A "+" formula returns the sum of every die. A "|" formula
    returns the higher of its terms' totals."""
    if not is_formula(formula):
        raise ValueError(f"not a dice formula: {formula!r}")
    generator = rng or random.Random()
    if "|" in formula:
        return max(_roll_term(term, generator) for term in formula.split("|"))
    return sum(_roll_term(term, generator) for term in formula.split("+"))


def _roll_term(term: str, generator: random.Random) -> int:
    count, sides = (int(part) for part in term.split("d"))
    return sum(generator.randint(1, sides) for _ in range(count))
