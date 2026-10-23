"""Dice rolls. A formula is one or more terms joined by "+", such as "1d20" or "2d6+1d4". What
"+" means between terms belongs to the rule set: "sum" adds every term, and "highest" rolls each
term and keeps the higher total, as Cairn 2e does with "1d8+1d8" for two weapons."""

from __future__ import annotations

import random
import re

# A single term such as "3d6". Stats and other ranges need exactly one term.
FORMULA = re.compile(r"([1-9][0-9]?)d([1-9][0-9]*)")
TERM = r"[1-9][0-9]?d[1-9][0-9]*"
ROLL = re.compile(rf"{TERM}(?:\+{TERM})*")
PLUS_MODES = ("sum", "highest")


def is_formula(text: object) -> bool:
    return isinstance(text, str) and ROLL.fullmatch(text) is not None


def roll_total(formula: str, rng: random.Random | None = None, *, plus: str | None = None) -> int:
    """Roll a formula. A single term is the same in every rule set. A formula with several terms
    needs the rule set's `plus` mode, which says whether "+" sums the terms or keeps the highest."""
    if not is_formula(formula):
        raise ValueError(f"not a dice formula: {formula!r}")
    generator = rng or random.Random()
    terms = formula.split("+")
    if len(terms) == 1:
        return _roll_term(terms[0], generator)
    if plus is None:
        raise ValueError(f"{formula!r} has several terms, so the rule set's plus mode is needed")
    if plus not in PLUS_MODES:
        raise ValueError(f"unknown plus mode: {plus!r}")
    totals = [_roll_term(term, generator) for term in terms]
    return max(totals) if plus == "highest" else sum(totals)


def _roll_term(term: str, generator: random.Random) -> int:
    count, sides = (int(part) for part in term.split("d"))
    return sum(generator.randint(1, sides) for _ in range(count))
