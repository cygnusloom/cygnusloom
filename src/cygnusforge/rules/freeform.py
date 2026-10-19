"""Python side of the freeform rule set. Its JSON names these functions."""

from __future__ import annotations

import random
from dataclasses import dataclass

from cygnusforge.rules.dice import roll_total


@dataclass(frozen=True)
class SocialRoll:
    die: int
    relationship: int
    circumstance: int

    @property
    def total(self) -> int:
        return self.die + self.relationship + self.circumstance

    def to_dict(self) -> dict[str, int]:
        return {
            "die": self.die,
            "relationship": self.relationship,
            "circumstance": self.circumstance,
            "total": self.total,
        }


def social_roll(
    relationship: int,
    circumstance: int,
    roll: int | None = None,
    rng: random.Random | None = None,
) -> SocialRoll:
    """One d20, plus the relationship and circumstance modifiers the GM sets.

    The player rolls the die when the rule set asks for it, and passes the result as `roll`."""
    die = roll if roll is not None else roll_total("1d20", rng)
    return SocialRoll(die=die, relationship=relationship, circumstance=circumstance)
