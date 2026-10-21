"""What the engine gives a rule set's Python functions that the model cannot supply."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
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
        character: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def update_location(self, name: str, description: str) -> dict[str, Any]: ...

    def delete_location(self, name: str) -> dict[str, Any]: ...

    def update_npc(
        self,
        name: str,
        description: str | None = None,
        personality: str | None = None,
        accent: str | None = None,
        quirks: Sequence[str] | None = None,
    ) -> dict[str, Any]: ...

    def delete_npc(self, name: str) -> dict[str, Any]: ...

    def move_actor(self, name: str, location: str) -> dict[str, str]: ...

    def add_note(self, text: str, subject: str | None = None) -> dict[str, Any]: ...

    def update_note(self, note_id: int, text: str) -> dict[str, Any]: ...

    def delete_note(self, note_id: int) -> dict[str, Any]: ...

    def search_notes(self, query: str) -> dict[str, list[dict[str, Any]]]: ...

    def speaker_profile(self, name: str) -> str: ...


@dataclass(frozen=True)
class ToolContext:
    world: World
    # Writes an NPC's dialogue and queues it for the narration. Takes the speaker, profile, intent,
    # and whether to replace the line already queued for that speaker.
    speak: Callable[[str, str, str, bool], Awaitable[None]]
    # Marks the player's message as out of character. Takes whether the story is narrated after the
    # out-of-character reply.
    ooc: Callable[[bool], None]
