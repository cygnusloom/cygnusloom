"""Default campaign labels and player display names."""

from __future__ import annotations

import random
import re
from collections.abc import Iterable

NUMBERED_LABEL = re.compile(r"#(\d+)\s*$")

# Two short, neutral words make a readable name that says nothing about the account. A player
# can replace it with any name they choose on the Account page.
DISPLAY_ADJECTIVES = (
    "Amber",
    "Bold",
    "Bright",
    "Calm",
    "Clever",
    "Copper",
    "Distant",
    "Gentle",
    "Golden",
    "Green",
    "Hidden",
    "Honest",
    "Ivory",
    "Kind",
    "Lively",
    "Lucky",
    "Misty",
    "Quiet",
    "Silver",
    "Steady",
    "Swift",
    "Wandering",
    "Wild",
    "Wise",
)
DISPLAY_NOUNS = (
    "Badger",
    "Beacon",
    "Cairn",
    "Comet",
    "Crane",
    "Dawn",
    "Ember",
    "Falcon",
    "Fern",
    "Fox",
    "Harbor",
    "Heron",
    "Lantern",
    "Lark",
    "Maple",
    "Moth",
    "Oak",
    "Otter",
    "Pine",
    "Raven",
    "Reed",
    "River",
    "Sparrow",
    "Willow",
)


def next_campaign_label(world_name: str, existing: Iterable[str]) -> str:
    """The default label for a new campaign in a world: the world's name and the next number,
    so campaigns of the same world are told apart ("Twin Lakes #1", "Twin Lakes #2")."""
    labels = list(existing)
    numbers = [int(match.group(1)) for label in labels if (match := NUMBERED_LABEL.search(label))]
    number = max(numbers, default=0) + 1
    taken = set(labels)
    while f"{world_name} #{number}" in taken:
        number += 1
    return f"{world_name} #{number}"


def random_display_name(rng: random.Random | None = None) -> str:
    """A two-word name and a four-digit number for a player who has not chosen one."""
    picker = rng if rng is not None else random.SystemRandom()
    adjective = picker.choice(DISPLAY_ADJECTIVES)
    noun = picker.choice(DISPLAY_NOUNS)
    return f"{adjective} {noun} {picker.randint(1000, 9999)}"
