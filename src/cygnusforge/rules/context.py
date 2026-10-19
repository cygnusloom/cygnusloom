"""What the engine gives a rule set's Python functions that the model cannot supply."""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from sqlalchemy import Engine


@dataclass(frozen=True)
class ToolContext:
    engine: Engine
    campaign_id: int
    rng: random.Random
    tables: Mapping[str, tuple[str, ...]]
    # Asks the speech model for an NPC's dialogue. Takes the speaker's profile and the intent.
    speak: Callable[[str, str], Awaitable[str]]
