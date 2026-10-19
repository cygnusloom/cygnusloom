"""What the engine gives a rule set's Python functions that the model cannot supply."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


class World(Protocol):
    """The campaign's world as rule-set tools may change and read it."""

    def create_location(self, name: str, description: str) -> dict[str, Any]: ...

    def create_npc(
        self,
        name: str,
        location: str,
        description: str,
        personality: str | None = None,
        accent: str | None = None,
        quirks: Sequence[str] | None = None,
    ) -> dict[str, Any]: ...

    def move_actor(self, name: str, location: str) -> dict[str, str]: ...

    def add_note(self, text: str, subject: str | None = None) -> dict[str, Any]: ...

    def search_notes(self, query: str) -> dict[str, list[dict[str, str]]]: ...

    def speaker_profile(self, name: str) -> str: ...


@dataclass(frozen=True)
class ToolContext:
    world: World
    # Writes an NPC's dialogue and queues it for the narration. Takes the speaker, profile, intent.
    speak: Callable[[str, str, str], Awaitable[None]]
