"""What the engine gives a rule set's Python functions that the model cannot supply."""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeVar

from cygnusloom.content.character import CharacterSpec
from cygnusloom.content.library import CheckRules

T = TypeVar("T")


class CoreWorld(Protocol):
    """The campaign's world as the system-agnostic core tools may change and read it. Every
    rule set has these, whatever its game system.

    `character`, `checks`, `rule_data`, and `rng` let a rule set's Python module read the character
    spec, its structured rule data and checks, and the campaign's dice without opening the campaign
    itself."""

    @property
    def character(self) -> CharacterSpec:
        """The rule set's character specification."""

    @property
    def rule_data(self) -> Mapping[str, object]:
        """The rule set's structured JSON data, indexed by definition id."""

    @property
    def checks(self) -> Mapping[str, CheckRules]:
        """The rule set's checks, indexed by id."""

    @property
    def rng(self) -> random.Random:
        """The campaign's dice, shared by modules so rolls remain reproducible."""

    def draw_character_table(self, table: str) -> str:
        """Choose one option from a character table without recording a campaign event."""

    def create_location(
        self,
        name: str,
        description: str,
        discovered: bool = False,
        x: float | None = None,
        y: float | None = None,
        force: bool = False,
        parent: str | None = None,
        icon: str | None = None,
        summary: str | None = None,
    ) -> dict[str, Any]: ...

    def create_region(
        self,
        name: str,
        terrain: str,
        points: Sequence[Sequence[float]],
        force: bool = False,
        discovered: bool = False,
        color: str | None = None,
        pattern: str | None = None,
        ink: str | None = None,
        label: str | None = None,
    ) -> dict[str, Any]: ...

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

    def update_location(
        self,
        name: str,
        description: str | None = None,
        x: float | None = None,
        y: float | None = None,
        off_map: bool = False,
        force: bool = False,
        icon: str | None = None,
        summary: str | None = None,
    ) -> dict[str, Any]: ...

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

    def update_npc_knowledge(
        self,
        name: str,
        description: str | None = None,
        personality: str | None = None,
        accent: str | None = None,
        quirks: Sequence[str] | None = None,
        pronouns: str | None = None,
        relationship: str | None = None,
        location: str | None = None,
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
        pronouns: str | None = None,
    ) -> dict[str, Any]: ...

    def hire_existing_npc(
        self,
        name: str,
        employer: str,
        wage: int,
        character: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def dismiss_hireling(self, name: str, cause: str) -> dict[str, Any]: ...

    def move_actor(
        self,
        name: str,
        location: str | None = None,
        travel: str | None = None,
        mode: str = "in_settlement",
        route: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def create_encounter_map(
        self,
        name: str,
        location: str,
        columns: int | None,
        rows: int | None,
        description: str,
        template: str = "",
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

    def change_currency(self, name: str, amount: int, cause: str) -> dict[str, Any]: ...

    def transfer_currency(
        self, payer: str, payee: str, amount: int, cause: str
    ) -> dict[str, Any]: ...

    def roll_currency(
        self, name: str, dice: str, multiplier: int, cause: str = ""
    ) -> dict[str, Any]:
        """Give a character currency from a dice roll times a multiplier, in the rule set's
        currency."""

    def place_object(
        self,
        place: str,
        contents: str,
        hidden: bool,
        cause: str,
        currency: int | None = None,
        item: str | None = None,
        item_size: str = "",
        name: str = "",
        item_text: str = "",
    ) -> dict[str, Any]: ...

    def take_object(self, object_name: str, character: str, cause: str) -> dict[str, Any]: ...

    def place_participant(self, name: str, column: int, row: int) -> dict[str, Any]: ...

    def end_npc_turn(self, name: str) -> dict[str, Any]: ...

    def add_note(self, text: str, subject: str | None = None) -> dict[str, Any]: ...

    def update_note(self, note_id: int, text: str) -> dict[str, Any]: ...

    def delete_note(self, note_id: int) -> dict[str, Any]: ...

    def search_notes(self, query: str) -> dict[str, list[dict[str, Any]]]: ...

    def create_storyline(
        self,
        name: str,
        summary: str,
        plot: str | None = None,
        characters: Sequence[str] | None = None,
        locations: Sequence[str] | None = None,
        objects: Sequence[str] | None = None,
        notes: str | None = None,
        focus: bool = False,
    ) -> dict[str, Any]: ...

    def update_storyline(
        self,
        storyline: str,
        name: str | None = None,
        summary: str | None = None,
        plot: str | None = None,
        characters: Sequence[str] | None = None,
        locations: Sequence[str] | None = None,
        objects: Sequence[str] | None = None,
        notes: str | None = None,
        active: bool | None = None,
    ) -> dict[str, Any]: ...

    def focus_storyline(self, storyline: str) -> dict[str, Any]: ...

    def deactivate_storyline(self, storyline: str) -> dict[str, Any]: ...

    def resolve_storyline(self, storyline: str, resolution: str) -> dict[str, Any]: ...

    def search_storylines(self, query: str) -> dict[str, list[dict[str, Any]]]: ...

    def all_at_location(self, location: str | None = None) -> dict[str, Any]: ...

    def search_location(self, query: str) -> dict[str, list[dict[str, Any]]]: ...

    def search_npc(self, query: str) -> dict[str, list[dict[str, Any]]]: ...

    def get_statblock(self, name: str) -> dict[str, Any]: ...

    def character_options(self, field: str | None = None) -> dict[str, Any]: ...

    def lookup_index(self) -> dict[str, list[dict[str, Any]]]: ...

    def speaker_profile(self, name: str) -> str: ...

    def speaker_at_player(self, name: str) -> bool: ...

    def discover_location(self, name: str, summary: str | None = None) -> dict[str, Any]: ...

    def set_encounter_map_cells(
        self, map: str, cells: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]: ...

    def append_journal(self, text: str) -> dict[str, Any]: ...

    def replace_in_journal(self, old_text: str, new_text: str) -> dict[str, Any]: ...

    def search_journal(self, query: str, context: int = 1) -> dict[str, list[dict[str, Any]]]: ...

    def view_journal(self, start_line: int = 1, end_line: int | None = None) -> dict[str, Any]: ...

    def roll_move(
        self,
        move: str,
        character: str,
        modifier: int = 0,
        stat: str | None = None,
        spell: str = "",
    ) -> dict[str, Any]: ...

    def choose_move_option(self, move: str, band: str, choice: str) -> dict[str, Any]:
        """Pick one of the choices a move band offered for a roll already made this turn, and
        run that choice's effects. `move` is the move's id or label."""

    def adjust_counter(self, character: str, counter: str, change: int) -> dict[str, Any]: ...

    def add_condition(self, name: str, condition: str, cause: str) -> dict[str, Any]: ...

    def end_condition(self, name: str, condition: str, how: str) -> dict[str, Any]: ...

    def roll_check(
        self,
        check: str,
        character: str,
        skill: str,
        stat: str,
        difficulty: int | None = None,
        modifier: int = 0,
        cause: str = "",
    ) -> dict[str, Any]:
        """Roll one of the rule set's checks for a character: the check's dice plus the skill's
        level, the stat's modifier, and `modifier`, against `difficulty` or the check's default.
        Records the check in the log and returns its fields."""

    def raise_skill(self, name: str, skill: str, level: int, cause: str = "") -> dict[str, Any]:
        """Set a character's level in one skill to `level`, recording the change. A rule set's
        skill progression defines its allowed levels."""

    def restore_stat(self, name: str, stat: str, amount: int, cause: str = "") -> dict[str, Any]:
        """Give a character back `amount` of what it lost from one stat, such as hit points,
        never below none, recording the restoration."""

    def harm_stat(self, name: str, stat: str, amount: int, cause: str) -> dict[str, Any]:
        """Add `amount` of harm to one stat, never past its maximum, recording the harm."""

    def set_stat_max(self, name: str, stat: str, value: int, cause: str = "") -> dict[str, Any]:
        """Set a character's maximum score for one stat to `value`, recording the change."""


class World(CoreWorld, Protocol):
    """The campaign's world as a rule set's own modules may change and read it: the core
    methods plus those that only some rule sets provide."""

    def search_monster(self, query: str) -> dict[str, list[dict[str, Any]]]: ...

    def create_monster(
        self,
        name: str,
        location: str,
        description: str | None = None,
        template: str | None = None,
        stats: Mapping[str, int | None] | None = None,
        armor: int | None = None,
        attacks: Sequence[Mapping[str, Any]] | None = None,
        force: bool = False,
        detachment: bool | None = None,
    ) -> dict[str, Any]: ...

    def wilderness_action(
        self, action: str, reason: str, purchases: Sequence[Mapping[str, Any]] | None = None
    ) -> dict[str, Any]: ...

    def surrender(self, name: str) -> dict[str, Any]: ...

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

    def roll_creative(self, tables: list[str] | None) -> dict[str, Any]: ...

    def dungeon_cycle(self, triggers: list[str], reason: str) -> dict[str, Any]: ...

    def roll_reaction(self, reason: str) -> dict[str, Any]: ...

    def advance_time(self, days: int = 0, parts: int = 0, hours: int = 0) -> dict[str, Any]: ...

    def rest(
        self,
        length: str,
        interrupted: bool = False,
        reason: str = "",
        scope: str = "party",
        character: str = "",
        advance_clock: bool | None = None,
    ) -> dict[str, Any]: ...

    def rest_history(self) -> dict[str, Any]: ...

    def with_hook_context(self, action: Callable[[Any], T]) -> T:
        """Run `action` with the rule set's hook context in one session, and commit its changes.
        The context is a services.hooks.HookContext, which gives the character state and records
        character marks. Rule-set tools use it to keep their own state in marks."""

    def add_penalty(self, name: str, cause: str, force: bool = False) -> dict[str, Any]:
        """Add the ruleset's penalty item to a character's inventory. A character the hooks hold
        back is refused unless `force` is true."""

    def search_marketplace(self, query: str) -> dict[str, Any]: ...

    def buy_item(
        self, name: str, item: str, price: int | None = None, size: str = ""
    ) -> dict[str, Any]: ...

    def buy_service(
        self, names: list[str], service: str, price: int | None = None
    ) -> dict[str, Any]: ...

    def give_item(
        self, name: str, item: str, size: str = "normal", damage: str = ""
    ) -> dict[str, Any]: ...

    def drop_item(self, name: str, item: str) -> dict[str, Any]: ...

    def equip_armor(self, name: str, item: str, equipped: bool = True) -> dict[str, Any]: ...

    def use_item(self, name: str, item: str, target: str | None = None) -> dict[str, Any]: ...

    def stabilize(self, name: str, explanation: str) -> dict[str, Any]: ...

    def give_spell_item(
        self, name: str, spell: str, description: str, *, role: Literal["reusable", "single_use"]
    ) -> dict[str, Any]: ...

    def give_charged_item(
        self, name: str, item: str, uses: int, recharge: str, size: str = "normal"
    ) -> dict[str, Any]: ...

    def cast_spell(self, name: str, spell: str) -> dict[str, Any]: ...

    def use_charge(self, name: str, item: str) -> dict[str, Any]: ...

    def recharge_charged_item(self, name: str, item: str) -> dict[str, Any]: ...

    def roll_save(self, name: str, stat: str, reason: str) -> dict[str, Any]: ...

    def modifier_dice(self, modifier: str) -> str | None: ...

    def player_save(self, roll: int, stat: str = "", cause: str = "") -> dict[str, Any]: ...

    def player_initiative(self, roll: int) -> dict[str, Any]: ...

    def set_armor(self, name: str, armor: int) -> dict[str, Any]: ...

    def recalculate_defense(self, name: str) -> dict[str, Any]: ...

    def recover_character(
        self, name: str, explanation: str, consequence: str
    ) -> dict[str, Any]: ...

    def heal_character(
        self, name: str, amount: int, stat: str | None = None, cause: str = ""
    ) -> dict[str, Any]: ...

    def harm_character(self, name: str, stat: str, amount: int, cause: str) -> dict[str, Any]: ...

    def retreat(self, name: str, safe: bool) -> dict[str, Any]: ...

    def player_retreat(self, safe: bool, roll: int) -> dict[str, Any]: ...


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
