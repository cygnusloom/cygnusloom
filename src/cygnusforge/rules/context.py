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
        force: bool = False,
        hp_tier: str | None = None,
        pronouns: str | None = None,
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
        character: Mapping[str, Any] | None = None,
        armor: int | None = None,
        attacks: Sequence[Mapping[str, str]] | None = None,
    ) -> dict[str, Any]: ...

    def delete_npc(self, name: str) -> dict[str, Any]: ...

    def hire_hireling(
        self,
        name: str,
        employer: str,
        wage: int,
        description: str,
        personality: str | None = None,
        accent: str | None = None,
        quirks: Sequence[str] | None = None,
        character: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def dismiss_hireling(self, name: str, cause: str) -> dict[str, Any]: ...

    def move_actor(
        self,
        name: str,
        location: str | None = None,
        travel: str = "on_foot",
        mode: str = "in_settlement",
        route: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...

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

    def attack(
        self,
        attacker: str,
        attack: str,
        target: str,
        modifier: str = "",
        targets: Sequence[str] | None = None,
    ) -> dict[str, Any]: ...

    def resolve_attacks(
        self, target: str, attacks: Sequence[Mapping[str, str]]
    ) -> dict[str, Any]: ...

    def player_attack(self, target: str, weapon: str, modifier: str = "") -> dict[str, Any]: ...

    def attack_dice(self, weapon: str, modifier: str = "") -> str:
        """The dice a player character's attack with `weapon` rolls: the modifier's dice when one
        is named, else the weapon's own dice. Raises InvalidInput when the weapon is not carried."""

    def resolve_group_break(self, name: str, outcome: str) -> dict[str, Any]:
        """Resolve a group whose break save failed: `outcome` says what happens to it."""

    def roll_tables(self, tables: list[str]) -> dict[str, Any]: ...

    def dungeon_cycle(self, triggers: list[str], reason: str) -> dict[str, Any]: ...

    def roll_reaction(self, reason: str) -> dict[str, Any]: ...

    def advance_time(self, days: int = 0, parts: int = 0, hours: int = 0) -> dict[str, Any]: ...

    def rest(self, length: str, interrupted: bool = False, reason: str = "") -> dict[str, Any]: ...

    def rest_history(self) -> dict[str, Any]: ...

    def set_condition(self, name: str, deprived: bool, cause: str = "") -> dict[str, Any]:
        """Mark or clear the ruleset's need condition on a character, recording `cause`."""

    def add_penalty(self, name: str, cause: str, force: bool = False) -> dict[str, Any]:
        """Add the ruleset's penalty item to a character's inventory. A deprived character is
        refused unless `force` is true."""

    def search_marketplace(self, query: str) -> dict[str, Any]: ...

    def buy_item(
        self, name: str, item: str, price: int | None = None, size: str = ""
    ) -> dict[str, Any]: ...

    def buy_service(self, names: list[str], service: str) -> dict[str, Any]: ...

    def change_gold(self, name: str, amount: int, cause: str) -> dict[str, Any]: ...

    def place_object(
        self,
        place: str,
        contents: str,
        hidden: bool,
        cause: str,
        gold: int | None = None,
        item: str | None = None,
        item_size: str = "",
        name: str = "",
    ) -> dict[str, Any]: ...

    def take_object(self, object_name: str, character: str, cause: str) -> dict[str, Any]: ...

    def give_item(
        self, name: str, item: str, size: str = "normal", damage: str = ""
    ) -> dict[str, Any]: ...

    def drop_item(self, name: str, item: str) -> dict[str, Any]: ...

    def use_item(self, name: str, item: str, target: str | None = None) -> dict[str, Any]: ...

    def stabilize(self, name: str, explanation: str) -> dict[str, Any]: ...

    def give_spellbook(self, name: str, spell: str, description: str) -> dict[str, Any]: ...

    def give_scroll(self, name: str, spell: str, description: str) -> dict[str, Any]: ...

    def give_charged_item(
        self, name: str, item: str, uses: int, recharge: str, size: str = "normal"
    ) -> dict[str, Any]: ...

    def cast_spell(self, name: str, spell: str) -> dict[str, Any]: ...

    def use_charge(self, name: str, item: str) -> dict[str, Any]: ...

    def recharge_charged_item(self, name: str, item: str) -> dict[str, Any]: ...

    def roll_save(self, name: str, stat: str, reason: str) -> dict[str, Any]: ...

    def modifier_dice(self, modifier: str) -> str | None: ...

    def player_save(self, roll: int) -> dict[str, Any]: ...

    def player_initiative(self, roll: int) -> dict[str, Any]: ...

    def set_armor(self, name: str, armor: int) -> dict[str, Any]: ...

    def recover_character(
        self, name: str, explanation: str, consequence: str
    ) -> dict[str, Any]: ...

    def harm_character(self, name: str, stat: str, amount: int, cause: str) -> dict[str, Any]: ...

    def end_condition(self, name: str, condition: str, how: str) -> dict[str, Any]: ...
    def retreat(self, name: str, safe: bool) -> dict[str, Any]: ...

    def player_retreat(self, safe: bool, roll: int) -> dict[str, Any]: ...

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
    # Checks a new place's name against the existing places before it is added. It raises when the
    # name repeats one. None when the rule set does not check.
    check_new_location: Callable[[str], Awaitable[None]] | None = None
