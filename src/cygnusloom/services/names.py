"""Default campaign labels."""

from __future__ import annotations

import re
from collections.abc import Iterable

NUMBERED_LABEL = re.compile(r"#(\d+)\s*$")


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
