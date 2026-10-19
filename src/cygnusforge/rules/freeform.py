"""Python side of the freeform rule set. Its JSON names these functions."""

from __future__ import annotations

import random
from dataclasses import dataclass


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
    relationship: int, circumstance: int, rng: random.Random | None = None
) -> SocialRoll:
    """One d20, plus the relationship and circumstance modifiers the GM sets."""
    die = (rng or random.Random()).randint(1, 20)
    return SocialRoll(die=die, relationship=relationship, circumstance=circumstance)
