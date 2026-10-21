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

    def create_encounter_map(
        self, name: str, location: str, columns: int, rows: int, description: str
    ) -> dict[str, Any]: ...

    def update_encounter_map(
        self,
        name: str,
        columns: int,
        rows: int,
        description: str,
        new_name: str | None = None,
        location: str | None = None,
    ) -> dict[str, Any]: ...

    def delete_encounter_map(self, name: str) -> dict[str, Any]: ...

    def start_encounter(self, map: str) -> dict[str, Any]: ...

    def end_encounter(self) -> dict[str, Any]: ...

    def add_participant(self, name: str, side: str | None = None) -> dict[str, Any]: ...

    def remove_participant(self, name: str) -> dict[str, Any]: ...

    def search_monster(self, query: str) -> dict[str, list[dict[str, Any]]]: ...

    def create_monster(
        self,
        name: str,
        location: str,
        description: str | None = None,
        template: str | None = None,
        stats: Mapping[str, int | None] | None = None,
        armor: int | None = None,
        attacks: Sequence[Mapping[str, str]] | None = None,
    ) -> dict[str, Any]: ...

    def attack(self, attacker: str, attack: str, target: str) -> dict[str, Any]: ...

    def player_attack(self, target: str, weapon: str, damage: int) -> dict[str, Any]: ...

    def player_save(self, roll: int) -> dict[str, Any]: ...

    def player_initiative(self, roll: int) -> dict[str, Any]: ...

    def set_armor(self, name: str, armor: int) -> dict[str, Any]: ...

    def recover_character(
        self, name: str, explanation: str, consequence: str
    ) -> dict[str, Any]: ...

    def place_participant(self, name: str, column: int, row: int) -> dict[str, Any]: ...

    def end_npc_turn(self, name: str) -> dict[str, Any]: ...

    def add_note(self, text: str, subject: str | None = None) -> dict[str, Any]: ...

    def update_note(self, note_id: int, text: str) -> dict[str, Any]: ...

    def delete_note(self, note_id: int) -> dict[str, Any]: ...

    def search_notes(self, query: str) -> dict[str, list[dict[str, Any]]]: ...

    def all_at_location(self, location: str | None = None) -> dict[str, Any]: ...

    def search_location(self, query: str) -> dict[str, list[dict[str, Any]]]: ...

    def search_npc(self, query: str) -> dict[str, list[dict[str, Any]]]: ...

    def get_statblock(self, name: str) -> dict[str, Any]: ...

    def character_options(self, field: str | None = None) -> dict[str, Any]: ...

    def lookup_index(self) -> dict[str, list[dict[str, Any]]]: ...

    def speaker_profile(self, name: str) -> str: ...


def brief(text: str, limit: int = 120) -> str:
    """The first sentence of text, cut to limit characters."""
    first = text.strip().split(". ", 1)[0].rstrip(".")
    if len(first) <= limit:
        return first
    return first[: limit - 1].rstrip() + "…"


@dataclass(frozen=True)
class ToolContext:
    world: World
    # Writes an NPC's dialogue and queues it for the narration. Takes the speaker, profile, intent,
    # and whether to replace the line already queued for that speaker.
    speak: Callable[[str, str, str, bool], Awaitable[None]]
    # Marks the player's message as out of character. Takes whether the story is narrated after the
    # out-of-character reply.
    ooc: Callable[[bool], None]
    # The rule set's full rule text by id, for lookup_rule. Rules the prompt does not show are here.
    rules: Mapping[str, str]
    # Name and brief of each lookup tool, for list_lookups.
    lookup_tools: Mapping[str, str]
