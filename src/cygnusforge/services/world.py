"""Places, characters, and notes: the world that a campaign's GM edits.

Writes check the campaign's current state, record events, and commit. Reads work on a state that
is already folded from the log. Problems the model can fix are raised as `InvalidInput`, and their
messages are returned to the model as they are."""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import Engine
from sqlmodel import Session

from cygnusforge.content.character import (
    CampaignSpec,
    CharacterError,
    CharacterSpec,
    Feature,
    Sheet,
    TableOption,
    campaign_schema,
    check_choices,
    check_swaps,
    choice_options,
    describe,
    resolve,
)
from cygnusforge.content.library import (
    ClockRules,
    CombatRules,
    ConditionRules,
    DiceTable,
    EncounterGrid,
    FoodRules,
    InventoryRules,
    MagicRules,
    Marketplace,
    MonsterTemplate,
    NeedRules,
    RestLength,
    ShopItem,
    WorldTemplate,
)
from cygnusforge.rules.context import brief
from cygnusforge.rules.dice import is_formula
from cygnusforge.services import aid, combat, encounters, morale
from cygnusforge.services import attacks as attack_modifiers
from cygnusforge.services import clock as clock_service
from cygnusforge.services import inventory as inventory_service
from cygnusforge.services import magic as magic_service
from cygnusforge.services import market as market_service
from cygnusforge.services import needs as needs_service
from cygnusforge.services import objects as objects_service
from cygnusforge.services.common import (
    InvalidInput,
    bounded_text,
    session_scope,
    whole_number,
)
from cygnusforge.services.conditions import active_conditions, end_condition
from cygnusforge.services.dice_tables import roll_tables as roll_dice_tables
from cygnusforge.services.dungeon import dungeon_cycle as dungeon_cycle_service
from cygnusforge.services.dungeon import roll_reaction as roll_reaction_service
from cygnusforge.services.events import (
    CAMPAIGN_WIDE,
    NPC_KIND,
    PC_KIND,
    ActorMoved,
    ArmorSet,
    CampaignOptionsChosen,
    CampaignState,
    Character,
    HirelingAdded,
    HirelingDismissed,
    HirelingPromoted,
    Item,
    LocationAdded,
    LocationDiscovered,
    LocationRemoved,
    LocationUpdated,
    Note,
    NoteAdded,
    NoteRemoved,
    NoteUpdated,
    NpcAdded,
    NpcRemoved,
    NpcStatted,
    NpcUpdated,
    OpeningStarted,
    PcAdded,
    Place,
    WorldObject,
    defeat_status,
    load_state,
    name_key,
    record,
    same_item_name,
    token_labels,
)
from cygnusforge.services.objects import visible_objects
from cygnusforge.services.tools import check_arguments

MAX_NAME = 120
MAX_STAT = 999
MAX_TEXT = 2000
MAX_QUIRKS = 10
QUIRKS_PER_NPC = 2
# The map always shows at least this many kilometers from its center, and leaves a margin.
MIN_MAP_RADIUS_KM = 1.0
MAP_MARGIN = 1.1
# Two places count as sharing a point when their positions agree to this many kilometers.
SAME_POINT_KM = 0.01


@dataclass(frozen=True)
class SceneView:
    """What the player may see: the player character, the place they are in, the NPCs there, the
    notes that concern them, and the player character's statblock."""

    pc: Character | None
    location: Place | None
    npcs: list[Character]
    notes: list[Note]
    sheet: list[str]
    pack: PackView | None = None
    hirelings: tuple[Character, ...] = ()
    objects: tuple[WorldObject, ...] = ()


@dataclass(frozen=True)
class PackItem:
    """One item the player character carries, as the player sees it."""

    name: str
    size: str
    slots: int
    uses: int
    max_uses: int
    kind: str
    text: str


@dataclass(frozen=True)
class PackView:
    """The player character's items in carried order, with the slots used and the capacity."""

    items: list[PackItem]
    used: int
    capacity: int


@dataclass(frozen=True)
class MapMarker:
    """A token for one place. Left and top are percentages of the map's width and height."""

    name: str
    label: str
    left: float
    top: float
    here: bool


@dataclass(frozen=True)
class MapView:
    """The player's map. Only discovered places appear on it. Places with a position are placed on a
    square centered on the origin and sized to fit the farthest one. Every discovered place is listed.
    """

    radius_km: float
    markers: list[MapMarker]
    places: list[Place]


@dataclass(frozen=True)
class WorldTools:
    """The world as the rule set's tools use it. Each call runs in its own session and commits."""

    engine: Engine
    campaign_id: int
    tables: Mapping[str, Sequence[TableOption]]
    rng: random.Random
    character: CharacterSpec
    # The rule set's encounter grid, or None when its encounters have no movement limit.
    grid: EncounterGrid | None = None
    # The rule set's combat, or None when it has none, and its monster statblocks by id.
    combat: CombatRules | None = None
    monsters: Mapping[str, MonsterTemplate] = field(default_factory=dict)
    # The world's own features (relics) by id. give_item finds them by name.
    features: Mapping[str, Feature] = field(default_factory=dict)
    # The rule set's conditions by id.
    conditions: Mapping[str, ConditionRules] = field(default_factory=dict)
    dice_tables: Mapping[str, DiceTable] = field(default_factory=dict)
    # Time, rest, and needs. Each is None or empty when the rule set does not track them.
    clock_rules: ClockRules | None = None
    rests: Mapping[str, RestLength] = field(default_factory=dict)
    interrupted_rest: str = ""
    need: NeedRules | None = None
    inventory: InventoryRules | None = None
    item_sizes: Mapping[str, int] = field(default_factory=dict)
    magic: MagicRules | None = None
    food: FoodRules | None = None
    marketplace: Marketplace | None = None
    # What "+" between dice terms means in this rule set: "sum" or "highest".
    plus: str = field(kw_only=True)

    def roll_tables(self, tables: list[str]) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return roll_dice_tables(
                db, self.campaign_id, tables, self.dice_tables, self.rng, plus=self.plus
            )

    def advance_time(self, days: int = 0, parts: int = 0, hours: int = 0) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return clock_service.advance_time(
                db,
                self.campaign_id,
                _required(self.clock_rules, "time"),
                self.need,
                self.inventory,
                days,
                parts,
                hours,
            )

    def rest(self, length: str, interrupted: bool = False, reason: str = "") -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return needs_service.take_rest(
                db,
                self.campaign_id,
                _required(self.clock_rules, "time"),
                self.rests,
                self.interrupted_rest,
                self.need,
                self.inventory,
                self.food,
                length,
                interrupted,
                reason,
            )

    def rest_history(self) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return needs_service.rest_history(db, self.campaign_id)

    def set_condition(self, name: str, deprived: bool, cause: str = "") -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return needs_service.set_condition(
                db, self.campaign_id, self.need, name, deprived, cause
            )

    def add_penalty(self, name: str, cause: str, force: bool = False) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            if not force and self.need is not None:
                character = inventory_service.character_named(
                    load_state(db, self.campaign_id), name
                )
                if character.unmet_since is not None:
                    penalty = _required(self.inventory, "inventory").penalty_name
                    raise InvalidInput(
                        f"{character.name} is deprived, so the engine adds their {penalty} each day "
                        f"and none was added here; set force only if something besides the "
                        f"need causes this {penalty}"
                    )
            return inventory_service.add_penalty(
                db, self.campaign_id, _required(self.inventory, "inventory"), name, cause
            )

    def buy_item(
        self, name: str, item: str, price: int | None = None, size: str = ""
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return market_service.buy_item(
                db,
                self.campaign_id,
                _required(self.marketplace, "a marketplace"),
                _required(self.inventory, "inventory"),
                self.item_sizes,
                name,
                item,
                price,
                size,
                armor_max=self.combat.armor_max if self.combat else 0,
            )

    def change_gold(self, name: str, amount: int, cause: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return market_service.change_gold(
                db,
                self.campaign_id,
                _required(self.marketplace, "a marketplace"),
                name,
                amount,
                cause,
            )

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
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return objects_service.place_object(
                db,
                self.campaign_id,
                self.marketplace,
                self.item_sizes,
                place,
                contents,
                hidden,
                cause,
                gold,
                item,
                item_size,
                name,
            )

    def take_object(self, object_name: str, character: str, cause: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return objects_service.take_object(
                db,
                self.campaign_id,
                self.marketplace,
                self.inventory,
                object_name,
                character,
                cause,
            )

    def buy_service(self, names: Sequence[str], service: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return market_service.buy_service(
                db,
                self.campaign_id,
                _required(self.marketplace, "a marketplace"),
                names,
                service,
            )

    def _listed(self, item: str) -> ShopItem | None:
        if self.marketplace is not None:
            listed = market_service.listed_item(self.marketplace, item)
            if listed is not None:
                return listed
        relic = next((f for f in self.features.values() if same_item_name(f.name, item)), None)
        if relic is None:
            return None
        return ShopItem(
            id=relic.id,
            name=relic.name,
            category=relic.feature_type,
            price=0,
            size=relic.size,
            armor=relic.armor,
            damage=relic.damage,
            uses=relic.uses,
            description=relic.description,
        )

    def give_item(
        self, name: str, item: str, size: str = "normal", damage: str = ""
    ) -> dict[str, Any]:
        listed = self._listed(item)
        with session_scope(self.engine) as db:
            return inventory_service.give_item(
                db,
                self.campaign_id,
                _required(self.inventory, "inventory"),
                self.item_sizes,
                name,
                item,
                size,
                listed,
                damage,
            )

    def drop_item(self, name: str, item: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return inventory_service.drop_item(
                db, self.campaign_id, _required(self.inventory, "inventory"), name, item
            )

    def use_item(self, name: str, item: str, target: str | None = None) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return aid.use_item(
                db,
                self.campaign_id,
                _required(self.inventory, "inventory"),
                self.combat,
                self.conditions,
                self.rng,
                name,
                item,
                target,
                self.magic.charged_kind if self.magic is not None else "",
                plus=self.plus,
            )

    def stabilize(self, name: str, explanation: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return aid.stabilize(
                db,
                self.campaign_id,
                name,
                explanation,
                self._rules(),
                self.conditions,
                self.rng,
                plus=self.plus,
            )

    def give_spellbook(self, name: str, spell: str, description: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return magic_service.give_spellbook(
                db,
                self.campaign_id,
                _required(self.magic, "magic"),
                _required(self.inventory, "inventory"),
                self.item_sizes,
                name,
                spell,
                description,
            )

    def give_scroll(self, name: str, spell: str, description: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return magic_service.give_scroll(
                db,
                self.campaign_id,
                _required(self.magic, "magic"),
                _required(self.inventory, "inventory"),
                self.item_sizes,
                name,
                spell,
                description,
            )

    def give_charged_item(
        self, name: str, item: str, uses: int, recharge: str, size: str = "normal"
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return magic_service.give_charged_item(
                db,
                self.campaign_id,
                _required(self.magic, "magic"),
                _required(self.inventory, "inventory"),
                self.item_sizes,
                name,
                item,
                uses,
                recharge,
                size,
            )

    def cast_spell(self, name: str, spell: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return magic_service.cast_spell(
                db,
                self.campaign_id,
                _required(self.magic, "magic"),
                _required(self.inventory, "inventory"),
                name,
                spell,
            )

    def use_charge(self, name: str, item: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return magic_service.use_charge(
                db, self.campaign_id, _required(self.magic, "magic"), name, item
            )

    def recharge_charged_item(self, name: str, item: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return magic_service.recharge_charged_item(
                db, self.campaign_id, _required(self.magic, "magic"), name, item
            )

    def dungeon_cycle(self, triggers: list[str], reason: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return dungeon_cycle_service(
                db, self.campaign_id, triggers, reason, self.dice_tables, self.rng, plus=self.plus
            )

    def roll_reaction(self, reason: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return roll_reaction_service(
                db, self.campaign_id, reason, self.dice_tables, self.rng, plus=self.plus
            )

    def roll_save(self, name: str, stat: str, reason: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return magic_service.roll_save(
                db,
                self.campaign_id,
                _required(self.combat, "combat"),
                self.character,
                self.rng,
                name,
                stat,
                reason,
                plus=self.plus,
            )

    def create_location(
        self,
        name: str,
        description: str,
        discovered: bool = False,
        x: float | None = None,
        y: float | None = None,
        force: bool = False,
        parent: str | None = None,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return create_location(
                db, self.campaign_id, name, description, discovered, x, y, force, parent
            )

    def discover_location(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return discover_location(db, self.campaign_id, name)

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
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return create_npc(
                db,
                self.campaign_id,
                self.tables,
                self.rng,
                self.character,
                name,
                location,
                description,
                personality,
                accent,
                quirks,
                character,
                monsters=self.monsters,
                force=force,
                plus=self.plus,
            )

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
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return hire_hireling(
                db,
                self.campaign_id,
                self.tables,
                self.rng,
                self.character,
                name,
                employer,
                wage,
                description,
                personality,
                accent,
                quirks,
                character,
                plus=self.plus,
            )

    def promote_hireling(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return promote_hireling(db, self.campaign_id, name)

    def dismiss_hireling(self, name: str, cause: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return dismiss_hireling(db, self.campaign_id, name, cause)

    def move_actor(self, name: str, location: str | None = None) -> dict[str, str]:
        with session_scope(self.engine) as db:
            return move_actor(db, self.campaign_id, name, location)

    def update_location(
        self,
        name: str,
        description: str,
        x: float | None = None,
        y: float | None = None,
        off_map: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return update_location(db, self.campaign_id, name, description, x, y, off_map, force)

    def delete_location(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return delete_location(db, self.campaign_id, name)

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
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return update_npc(
                db,
                self.campaign_id,
                self.character,
                self.rng,
                name,
                description,
                personality,
                accent,
                quirks,
                character,
                armor,
                attacks,
                armor_max=self.combat.armor_max if self.combat else 0,
                plus=self.plus,
            )

    def delete_npc(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return delete_npc(db, self.campaign_id, name)

    def create_encounter_map(
        self,
        name: str,
        location: str,
        columns: int,
        rows: int,
        description: str,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.create_map(
                db, self.campaign_id, name, location, columns, rows, description
            )

    def update_encounter_map(
        self,
        name: str,
        columns: int,
        rows: int,
        description: str,
        new_name: str | None = None,
        location: str | None = None,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.update_map(
                db, self.campaign_id, name, columns, rows, description, new_name, location
            )

    def delete_encounter_map(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.delete_map(db, self.campaign_id, name)

    def start_encounter(self, map: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.start_encounter(
                db, self.campaign_id, map, rolls_initiative=self.combat is not None
            )

    def end_encounter(self) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.end_encounter(db, self.campaign_id)

    def add_participant(self, name: str, side: str | None = None) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.add_participant(db, self.campaign_id, name, side)

    def remove_participant(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.remove_participant(
                db, self.campaign_id, name, self._initiative_order()
            )

    def create_monster(
        self,
        name: str,
        location: str,
        description: str | None = None,
        template: str | None = None,
        stats: Mapping[str, int | None] | None = None,
        armor: int | None = None,
        attacks: Sequence[Mapping[str, str]] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return create_monster(
                db,
                self.campaign_id,
                self.monsters,
                self.character,
                name,
                location,
                description,
                template,
                stats or {},
                armor,
                attacks,
                armor_max=self.combat.armor_max if self.combat else 0,
                force=force,
            )

    def search_monster(self, query: str) -> dict[str, list[dict[str, Any]]]:
        return search_monster(self.monsters, query)

    def search_marketplace(self, query: str) -> dict[str, Any]:
        return search_marketplace(_required(self.marketplace, "a marketplace"), query)

    def attack(self, attacker: str, attack: str, target: str, modifier: str = "") -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return combat.attack(
                db,
                self.campaign_id,
                attacker,
                attack,
                target,
                self._rules(),
                self.rng,
                modifier,
                plus=self.plus,
            )

    def resolve_attacks(self, target: str, attacks: Sequence[Mapping[str, str]]) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return combat.resolve_attacks(
                db, self.campaign_id, target, attacks, self._rules(), self.rng, plus=self.plus
            )

    def player_attack(self, target: str, weapon: str, modifier: str = "") -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return combat.player_attack(
                db,
                self.campaign_id,
                target,
                weapon,
                self._rules(),
                self.rng,
                modifier,
                plus=self.plus,
            )

    def attack_dice(self, weapon: str, modifier: str = "") -> str:
        with session_scope(self.engine) as db:
            return combat.attack_dice(db, self.campaign_id, weapon, self._rules(), modifier)

    def resolve_group_break(self, name: str, outcome: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return combat.resolve_group_break(db, self.campaign_id, name, outcome, self._rules())

    def modifier_dice(self, modifier: str) -> str | None:
        return attack_modifiers.modifier_dice(self._rules().modifiers, modifier)

    def player_save(self, roll: int) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return combat.player_save(db, self.campaign_id, roll, self._rules())

    def player_initiative(self, roll: int) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return combat.player_initiative(db, self.campaign_id, roll, self._rules())

    def set_armor(self, name: str, armor: int) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return combat.set_armor(db, self.campaign_id, name, armor, self._rules().armor_max)

    def recover_character(self, name: str, explanation: str, consequence: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return combat.recover_character(
                db, self.campaign_id, name, explanation, consequence, self._rules()
            )

    def harm_character(self, name: str, stat: str, amount: int, cause: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return combat.harm_character(
                db, self.campaign_id, name, stat, amount, cause, self._rules()
            )

    def end_condition(self, name: str, condition: str, how: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return end_condition(
                db,
                self.campaign_id,
                name,
                condition,
                how,
                self._rules(),
                self.conditions,
                self.rng,
                plus=self.plus,
            )

    def retreat(self, name: str, safe: bool) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return morale.retreat(
                db, self.campaign_id, name, safe, self._rules(), self.rng, plus=self.plus
            )

    def player_retreat(self, safe: bool, roll: int) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return morale.player_retreat(db, self.campaign_id, safe, roll, self._rules())

    def surrender(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return combat.surrender(db, self.campaign_id, name, self._rules())

    def _rules(self) -> CombatRules:
        if self.combat is None:
            raise InvalidInput("this rule set has no combat")
        return self.combat

    def _initiative_order(self) -> encounters.InitiativeOrder | None:
        return self.combat.module.initiative_order if self.combat is not None else None

    def place_participant(self, name: str, column: int, row: int) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.place_participant(db, self.campaign_id, name, column, row, self.grid)

    def end_npc_turn(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.end_turn(db, self.campaign_id, name, by_player=False)

    def add_note(self, text: str, subject: str | None = None) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return add_note(db, self.campaign_id, text, subject)

    def update_note(self, note_id: int, text: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return update_note(db, self.campaign_id, note_id, text)

    def delete_note(self, note_id: int) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return delete_note(db, self.campaign_id, note_id)

    def search_notes(self, query: str) -> dict[str, list[dict[str, Any]]]:
        with session_scope(self.engine) as db:
            return search_notes(load_state(db, self.campaign_id), query)

    def all_at_location(self, location: str | None = None) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return all_at_location(load_state(db, self.campaign_id), location)

    def search_location(self, query: str) -> dict[str, list[dict[str, Any]]]:
        with session_scope(self.engine) as db:
            return search_location(load_state(db, self.campaign_id), query)

    def search_npc(self, query: str) -> dict[str, list[dict[str, Any]]]:
        with session_scope(self.engine) as db:
            return search_npc(load_state(db, self.campaign_id), query)

    def get_statblock(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return get_statblock(
                load_state(db, self.campaign_id),
                name,
                self.character,
                self.combat,
                self.conditions,
                self.inventory,
            )

    def character_options(self, field: str | None = None) -> dict[str, Any]:
        return choice_options(self.character, field)

    def lookup_index(self) -> dict[str, list[dict[str, Any]]]:
        with session_scope(self.engine) as db:
            return lookup_index(load_state(db, self.campaign_id))

    def speaker_profile(self, name: str) -> str:
        with session_scope(self.engine) as db:
            return speaker_profile(load_state(db, self.campaign_id), name, self.character)


def seed_campaign(
    db: Session,
    campaign_id: int,
    world: WorldTemplate,
    tables: Mapping[str, Sequence[TableOption]],
    rng: random.Random,
    spec: CharacterSpec,
    *,
    monsters: Mapping[str, MonsterTemplate],
    plus: str,
) -> None:
    """Record a campaign's places and the world's NPCs as events. The player character is made
    later, by `create_player_character`. An NPC that names a statblock takes its stats."""
    # A place can be declared before the place it is inside, so every name is known first.
    names = {spot.id: spot.name for spot in world.locations}
    for spot in world.locations:
        record(
            db,
            campaign_id,
            LocationAdded(
                name=spot.name,
                description=spot.description,
                discovered=spot.discovered,
                x=spot.x,
                y=spot.y,
                parent=None if spot.parent is None else names[spot.parent],
            ),
        )
    for npc in world.npcs:
        base = None if npc.monster is None else monsters[npc.monster]
        event = _npc_event(
            tables,
            rng,
            spec,
            name=npc.name,
            location=names[npc.location],
            description=npc.description,
            personality=npc.personality,
            accent=npc.accent,
            quirks=npc.quirks,
            character=None if base is None else {"stats": dict(base.stats)},
            plus=plus,
            story_only=npc.story_only,
            rules=npc.rules,
        )
        if base is not None:
            event = _with_statblock(event, base)
        record(db, campaign_id, event)
    db.commit()


def set_campaign_options(
    db: Session, campaign_id: int, campaign: CampaignSpec, submitted: Mapping[str, Any]
) -> dict[str, Any]:
    """Record the campaign's shared options. They are chosen once, so the campaign's omen and
    the like cannot change after the characters are made."""
    if not campaign.fields:
        raise InvalidInput("this campaign has no options to choose")
    state = load_state(db, campaign_id)
    if state.options:
        raise InvalidInput("the campaign options are already chosen")
    problem = check_arguments(campaign_schema(campaign), dict(submitted))
    if problem is not None:
        raise InvalidInput(problem)
    record(db, campaign_id, CampaignOptionsChosen(options=dict(submitted)))
    db.commit()
    return {"chosen": "campaign"}


def _starting_item(feature: Feature, magic: MagicRules | None, item_sizes: Mapping[str, int]):
    """The inventory item a starting feature becomes: a spellbook for a spell, else the item."""
    if feature.feature_type == "spell":
        assert magic is not None
        return Item(
            feature.name,
            magic.spellbook_size,
            item_sizes[magic.spellbook_size],
            kind=magic.spellbook_kind,
            text=feature.description,
        )
    return Item(
        feature.name,
        feature.size,
        item_sizes[feature.size],
        damage=feature.damage,
        rules=feature.rules,
        uses=feature.uses,
        max_uses=feature.uses,
    )


def create_player_character(
    db: Session,
    campaign_id: int,
    campaign: CampaignSpec,
    spec: CharacterSpec,
    name: str,
    rolled: Mapping[str, int],
    submitted: Mapping[str, Any],
    rng: random.Random,
    inventory: InventoryRules | None = None,
    item_sizes: Mapping[str, int] | None = None,
    magic: MagicRules | None = None,
    currency: str = "",
    armor_max: int = 0,
    *,
    plus: str,
) -> dict[str, Any]:
    """Make the player character from the player's choices. `rolled` are the stats rolled for
    this character; the player may only keep them or swap within a swap group. Starting item
    features are carried as inventory items, spell features as spellbooks, and the armor they
    give is worn. The currency stays a count."""
    state = load_state(db, campaign_id)
    current = state.pc()
    if current is not None and not current.dead:
        raise InvalidInput("the player character already exists")
    if campaign.fields and not state.options:
        raise InvalidInput("the campaign options must be chosen before the player character")
    name = bounded_text(name, "name", MAX_NAME)
    _ensure_free_name(state, name)
    chosen = submitted.get("stats", {})
    if set(chosen) != set(spec.stats):
        raise InvalidInput("every stat needs a value")
    try:
        check_swaps(spec, rolled, chosen)
        check_choices(spec, submitted.get("fields", {}))
    except CharacterError as error:
        raise InvalidInput(str(error)) from error
    sheet = resolve(spec, submitted, rng, plus=plus)
    _check_sheet_text(sheet)
    fields = _sheet_fields(sheet)
    starting = fields["features"]
    carried: list[tuple[Feature, int]] = []
    if inventory is not None and item_sizes is not None:
        for feature_id in list(starting):
            feature = spec.features[feature_id]
            if feature_id == currency:
                continue
            if feature.feature_type == "item" or (
                feature.feature_type == "spell" and magic is not None
            ):
                carried.append((feature, starting.pop(feature_id)))
    record(db, campaign_id, PcAdded(name=name, **fields))
    armor = 0
    for feature, count in carried:
        for _ in range(count):
            inventory_service.carry_item(
                db,
                campaign_id,
                inventory,
                name,
                _starting_item(feature, magic, item_sizes),
                cause="starting gear",
            )
        armor += feature.armor * count
    if armor:
        cap = armor_max or combat.UNCAPPED_ARMOR
        record(db, campaign_id, ArmorSet(name=name, armor=min(armor, cap)))
    record(db, campaign_id, OpeningStarted())
    db.commit()
    return {"created": "pc", "name": name, "location": None}


def create_location(
    db: Session,
    campaign_id: int,
    name: str,
    description: str,
    discovered: bool = False,
    x: float | None = None,
    y: float | None = None,
    force: bool = False,
    parent: str | None = None,
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    name = bounded_text(name, "name", MAX_NAME)
    _ensure_free_name(state, name)
    text = bounded_text(description, "description", MAX_TEXT)
    if not isinstance(discovered, bool):
        raise InvalidInput("discovered must be true or false")
    px, py = _coordinates(x, y)
    inside = None
    if parent is not None:
        inside = _parent_for_new_place(state, parent, px, py)
        px, py = None, None
    else:
        _ensure_open_point(state, name, px, py, force)
    record(
        db,
        campaign_id,
        LocationAdded(
            name=name, description=text, discovered=discovered, x=px, y=py, parent=inside
        ),
    )
    db.commit()
    return {"created": "location", "name": name, "discovered": discovered}


def create_npc(
    db: Session,
    campaign_id: int,
    tables: Mapping[str, Sequence[TableOption]],
    rng: random.Random,
    spec: CharacterSpec,
    name: str,
    location: str,
    description: str,
    personality: str | None = None,
    accent: str | None = None,
    quirks: Sequence[str] | None = None,
    character: Mapping[str, Any] | None = None,
    *,
    monsters: Mapping[str, MonsterTemplate],
    force: bool = False,
    plus: str,
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    name = bounded_text(name, "name", MAX_NAME)
    _ensure_free_name(state, name)
    where = _place_named(state, location)
    try:
        check_choices(spec, (character or {}).get("fields", {}))
    except CharacterError as error:
        raise InvalidInput(str(error)) from error
    base = None if force else _statblock_named(monsters, name)
    if base is not None:
        if (character or {}).get("stats"):
            raise InvalidInput(_statblock_conflict(base, spec))
        character = {**(character or {}), "stats": dict(base.stats)}
    event = _npc_event(
        tables,
        rng,
        spec,
        name=name,
        location=where.name,
        description=bounded_text(description, "description", MAX_TEXT),
        personality=_optional_text(personality, "personality"),
        accent=_optional_text(accent, "accent"),
        quirks=_quirks(quirks),
        character=character,
        plus=plus,
    )
    if base is not None:
        event = _with_statblock(event, base)
    record(db, campaign_id, event)
    db.commit()
    return {
        "created": "npc",
        "name": event.name,
        "location": event.location,
        "personality": event.personality,
        "accent": event.accent,
        "quirks": list(event.quirks),
        "statblock": base.id if base else None,
    }


def hire_hireling(
    db: Session,
    campaign_id: int,
    tables: Mapping[str, Sequence[TableOption]],
    rng: random.Random,
    spec: CharacterSpec,
    name: str,
    employer: str,
    wage: int,
    description: str,
    personality: str | None = None,
    accent: str | None = None,
    quirks: Sequence[str] | None = None,
    character: Mapping[str, Any] | None = None,
    *,
    plus: str,
) -> dict[str, Any]:
    """Hire a character into the employer's company, at the employer's location. The hireling is
    controlled by the player and its wage is paid each day from the employer's gold. Its sheet is
    rolled the way an NPC's is."""
    state = load_state(db, campaign_id)
    name = bounded_text(name, "name", MAX_NAME)
    _ensure_free_name(state, name)
    boss = state.characters.get(name_key(employer))
    if boss is None or boss.kind != PC_KIND or boss.hireling:
        raise InvalidInput(f"{employer} is not a player character who can hire")
    if boss.location is None:
        raise InvalidInput(f"{boss.name} has no location yet, so cannot hire anyone")
    if not isinstance(wage, int) or isinstance(wage, bool) or wage < 0:
        raise InvalidInput("the wage must be a whole number of gold, zero or more")
    try:
        check_choices(spec, (character or {}).get("fields", {}))
    except CharacterError as error:
        raise InvalidInput(str(error)) from error
    event = _npc_event(
        tables,
        rng,
        spec,
        name=name,
        location=boss.location,
        description=bounded_text(description, "description", MAX_TEXT),
        personality=_optional_text(personality, "personality"),
        accent=_optional_text(accent, "accent"),
        quirks=_quirks(quirks),
        character=character,
        plus=plus,
    )
    record(
        db,
        campaign_id,
        HirelingAdded(
            name=event.name,
            employer=boss.name,
            wage=wage,
            location=event.location,
            description=event.description,
            personality=event.personality,
            accent=event.accent,
            quirks=list(event.quirks),
            stats=dict(event.stats),
            templates=dict(event.templates),
            features=dict(event.features),
            fields=dict(event.fields),
        ),
    )
    db.commit()
    return {
        "hired": event.name,
        "employer": boss.name,
        "wage": wage,
        "location": event.location,
        "personality": event.personality,
        "accent": event.accent,
        "quirks": list(event.quirks),
    }


def dismiss_hireling(db: Session, campaign_id: int, name: str, cause: str) -> dict[str, Any]:
    """The hireling leaves its employer's company and stays where it is, as an NPC."""
    state = load_state(db, campaign_id)
    hireling = state.characters.get(name_key(name))
    if hireling is None or not hireling.hireling:
        raise InvalidInput(f"{name} is not a hireling")
    cause = bounded_text(cause, "cause", MAX_TEXT)
    employer = hireling.employer
    record(db, campaign_id, HirelingDismissed(name=hireling.name, cause=cause))
    db.commit()
    return {"dismissed": hireling.name, "employer": employer}


def promote_hireling(db: Session, campaign_id: int, name: str) -> dict[str, Any]:
    """After the player character died, a living hireling takes over as the player character."""
    state = load_state(db, campaign_id)
    current = state.pc()
    if current is not None and not current.dead:
        raise InvalidInput("the player character is still alive")
    hireling = state.characters.get(name_key(name))
    if hireling is None or not hireling.hireling:
        raise InvalidInput(f"{name} is not a hireling")
    if hireling.dead or hireling.dying_hours is not None:
        raise InvalidInput(f"{hireling.name} cannot take up the part: they are not able to act")
    employer = hireling.employer
    record(db, campaign_id, HirelingPromoted(name=hireling.name, employer=employer))
    db.commit()
    return {"promoted": hireling.name, "employer": employer}


def create_monster(
    db: Session,
    campaign_id: int,
    monsters: Mapping[str, MonsterTemplate],
    spec: CharacterSpec,
    name: str,
    location: str,
    description: str | None,
    template: str | None,
    stats: Mapping[str, int | None],
    armor: int | None,
    attacks: Sequence[Mapping[str, str]] | None,
    armor_max: int = 0,
    force: bool = False,
) -> dict[str, Any]:
    """An NPC built from a statblock. A template gives the starting values, and any value given
    replaces it. Without a template, a name that matches a statblock uses it and takes no values
    of its own, unless force is set."""
    state = load_state(db, campaign_id)
    name = bounded_text(name, "name", MAX_NAME)
    _ensure_free_name(state, name)
    where = _place_named(state, location)
    base: MonsterTemplate | None = None
    given = {key: value for key, value in stats.items() if value is not None}
    if template is not None:
        base = monsters.get(template.strip())
        if base is None:
            raise InvalidInput(
                f"there is no monster template {template!r}; search_monster lists them"
            )
    elif not force:
        base = _statblock_named(monsters, name)
        if base is not None and (given or armor is not None or attacks is not None):
            raise InvalidInput(_statblock_conflict(base, spec))
    merged = {**(base.stats if base else {}), **given}
    unknown = [key for key in merged if key not in spec.stats]
    if unknown:
        raise InvalidInput("unknown stats: " + ", ".join(unknown))
    missing = [spec.stats[key].name for key in spec.stats if key not in merged]
    if missing:
        raise InvalidInput("missing stats: " + ", ".join(missing))
    for key, value in merged.items():
        whole_number(value, f"{spec.stats[key].name}", 0, MAX_STAT)
    if description is not None:
        text = description
    elif base is not None:
        text = base.description
    else:
        raise InvalidInput("a description is needed when there is no template")
    armor_value = whole_number(
        armor if armor is not None else (base.armor if base else 0),
        "armor",
        0,
        armor_max or combat.UNCAPPED_ARMOR,
    )
    attack_map = _attacks(attacks) if attacks is not None else dict(base.attacks if base else {})
    event = NpcAdded(
        name=name,
        location=where.name,
        description=bounded_text(text, "description", MAX_TEXT),
        personality="",
        accent="",
        quirks=[],
        stats=dict(merged),
        armor=armor_value,
        attacks=attack_map,
        creature=base.name if base else "",
        large_group=bool(base and base.large_group),
    )
    record(db, campaign_id, event)
    db.commit()
    return {
        "created": "monster",
        "name": event.name,
        "location": event.location,
        "template": base.id if base else None,
        "stats": dict(merged),
        "armor": armor_value,
        "attacks": attack_map,
    }


def _attacks(attacks: Sequence[Mapping[str, str]]) -> dict[str, str]:
    """Each attack is {"name", "dice"}, and its dice must be a formula such as '1d8' or '1d6+1d6'."""
    checked: dict[str, str] = {}
    seen: set[str] = set()
    for attack in attacks:
        label = bounded_text(attack.get("name", ""), "attack name", MAX_NAME)
        dice = attack.get("dice", "")
        if label.casefold() in seen:
            raise InvalidInput(f"attack {label!r} is listed twice")
        if not isinstance(dice, str) or not is_formula(dice):
            raise InvalidInput(f"attack {label!r} needs a dice formula such as '1d8' or '1d6+1d6'")
        seen.add(label.casefold())
        checked[label] = dice
    return checked


def _statblock_conflict(base: MonsterTemplate, spec: CharacterSpec) -> str:
    """The refusal when a named statblock creature is given values of its own."""
    stats = ", ".join(
        f"{spec.stats[key].name} {value}" for key, value in base.stats.items() if key in spec.stats
    )
    attacks = "".join(f"; {label} {dice}" for label, dice in base.attacks.items())
    return (
        f"{base.name} has a published statblock ({stats}{attacks}). "
        "Leave out the values you would give to use it, or set force to create a custom "
        "creature with this name."
    )


def _with_statblock(event: NpcAdded, base: MonsterTemplate) -> NpcAdded:
    """The NPC event with a statblock's armor, attacks, creature, and large-group flag. Its stats
    are set by the caller, before the event is made, so they are not rolled."""
    return replace(
        event,
        armor=base.armor,
        attacks=dict(base.attacks),
        creature=base.name,
        large_group=base.large_group,
    )


def _statblock_named(monsters: Mapping[str, MonsterTemplate], name: str) -> MonsterTemplate | None:
    """The statblock whose name or id is `name`, ignoring case, so a named module or bestiary
    creature gets its published stats without the caller supplying them."""
    wanted = name.strip().lower()
    slug = wanted.replace(" ", "-")
    for monster in monsters.values():
        if monster.id.lower() in (wanted, slug) or monster.name.lower() == wanted:
            return monster
    return None


def search_monster(
    monsters: Mapping[str, MonsterTemplate], query: str
) -> dict[str, list[dict[str, Any]]]:
    """Statblocks whose id, name, or description contain every word of `query`."""
    words = _words(query)
    matches = [
        {
            "id": monster.id,
            "name": monster.name,
            "description": monster.description,
            "stats": dict(monster.stats),
            "armor": monster.armor,
            "attacks": dict(monster.attacks),
        }
        for monster in monsters.values()
        if _has_words(words, monster.id, monster.name, monster.description)
    ]
    return {"monsters": matches}


def search_marketplace(marketplace: Marketplace, query: str) -> dict[str, Any]:
    """Catalogue items and services whose id, name, category, or description contain every word
    of `query`. Items come back in full, up to a limit, and `more` counts the rest."""
    words = _words(query)
    items = [
        {
            "item": item.name,
            "category": item.category,
            "price": item.price,
            "size": item.size,
            "description": item.description,
        }
        for item in marketplace.items
        if _has_words(words, item.id, item.name, item.category, item.description)
    ]
    services = [
        {"service": service.id, "name": service.name, "price": service.price}
        for service in marketplace.services
        if _has_words(words, service.id, service.name)
    ]
    return {
        "currency": marketplace.currency,
        "items": items[:MAX_MARKET_RESULTS],
        "more": max(0, len(items) - MAX_MARKET_RESULTS),
        "services": services,
    }


def update_location(
    db: Session,
    campaign_id: int,
    name: str,
    description: str,
    x: float | None = None,
    y: float | None = None,
    off_map: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Change a location's description and its map position. Giving x and y moves it. Giving
    off_map takes it off the map. Without any of these the position stays as it is."""
    state = load_state(db, campaign_id)
    where = _place_named(state, name)
    text = bounded_text(description, "description", MAX_TEXT)
    if not isinstance(off_map, bool):
        raise InvalidInput("off_map must be true or false")
    px, py = _coordinates(x, y)
    if where.parent is not None:
        if px is not None or py is not None:
            raise InvalidInput(f"{where.name} is inside {where.parent}, so it has no map position")
        off_map = False
    if off_map:
        if px is not None or py is not None:
            raise InvalidInput("give x and y to move the place, or off_map to take it off the map")
    elif px is None and py is None and where.position is not None:
        px, py = where.position.x, where.position.y
    _ensure_open_point(state, where.name, px, py, force)
    record(db, campaign_id, LocationUpdated(name=where.name, description=text, x=px, y=py))
    db.commit()
    return {"updated": "location", "name": where.name}


def discover_location(db: Session, campaign_id: int, name: str) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    where = _place_named(state, name)
    if not where.discovered:
        record(db, campaign_id, LocationDiscovered(name=where.name))
        db.commit()
    return {"discovered": "location", "name": where.name}


def delete_location(db: Session, campaign_id: int, name: str) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    where = _place_named(state, name)
    occupants = [c.name for c in state.characters.values() if c.location == where.name]
    if occupants:
        raise InvalidInput(
            f"{where.name} still has {', '.join(occupants)} there; move them elsewhere first"
        )
    inside = _children_of(state, where.name)
    if inside:
        raise InvalidInput(
            f"{where.name} still has {', '.join(inside)} inside it; delete those places first"
        )
    mapped = [m.name for m in state.maps.values() if name_key(m.location) == name_key(where.name)]
    if mapped:
        raise InvalidInput(
            f"{where.name} still has the encounter map {', '.join(mapped)}; delete the map first"
        )
    record(db, campaign_id, LocationRemoved(name=where.name))
    db.commit()
    return {"deleted": "location", "name": where.name}


def update_npc(
    db: Session,
    campaign_id: int,
    spec: CharacterSpec,
    rng: random.Random,
    name: str,
    description: str | None = None,
    personality: str | None = None,
    accent: str | None = None,
    quirks: Sequence[str] | None = None,
    character: Mapping[str, Any] | None = None,
    armor: int | None = None,
    attacks: Sequence[Mapping[str, str]] | None = None,
    *,
    armor_max: int = 0,
    plus: str,
) -> dict[str, Any]:
    """Change the NPC's details. Only the given fields change; quirks, if given, replace the list.
    A story-only NPC can be given its stats once, with character, armor, and attacks."""
    state = load_state(db, campaign_id)
    npc = _npc_named(state, name)
    text_given = any(value is not None for value in (description, personality, accent, quirks))
    stats_given = any(value is not None for value in (character, armor, attacks))
    if not text_given and not stats_given:
        raise InvalidInput(
            "give at least one of description, personality, accent, quirks, or stats"
        )
    statted: NpcStatted | None = None
    if stats_given:
        if not npc.story_only:
            raise InvalidInput(
                f"{npc.name} already has stats; only a story-only NPC can be given them"
            )
        try:
            check_choices(spec, (character or {}).get("fields", {}))
        except CharacterError as error:
            raise InvalidInput(str(error)) from error
        sheet = resolve(spec, character or {}, rng, plus=plus)
        _check_sheet_text(sheet)
        statted = NpcStatted(
            name=npc.name,
            armor=0
            if armor is None
            else whole_number(armor, "armor", 0, armor_max or combat.UNCAPPED_ARMOR),
            attacks=_attacks(attacks or []),
            **_sheet_fields(sheet),
        )
    if text_given:
        record(
            db,
            campaign_id,
            NpcUpdated(
                name=npc.name,
                description=npc.description
                if description is None
                else bounded_text(description, "description", MAX_TEXT),
                personality=npc.personality
                if personality is None
                else bounded_text(personality, "personality", MAX_NAME),
                accent=npc.accent if accent is None else bounded_text(accent, "accent", MAX_NAME),
                quirks=list(npc.quirks) if quirks is None else _quirks(quirks) or [],
            ),
        )
    if statted is not None:
        record(db, campaign_id, statted)
    db.commit()
    return {"updated": "npc", "name": npc.name, "statted": statted is not None}


def delete_npc(db: Session, campaign_id: int, name: str) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    npc = _npc_named(state, name)
    if state.encounter is not None and npc.name in state.encounter.order:
        raise InvalidInput(f"{npc.name} is in the encounter; remove them from the encounter first")
    record(db, campaign_id, NpcRemoved(name=npc.name))
    db.commit()
    return {"deleted": "npc", "name": npc.name}


def move_actor(
    db: Session, campaign_id: int, name: str, location: str | None = None
) -> dict[str, str]:
    """Move a character to a place. With no place, an NPC goes into the wind: it is nowhere
    in particular until a later move brings it back."""
    state = load_state(db, campaign_id)
    actor = _character_named(state, name)
    if location is None:
        if actor.kind != NPC_KIND:
            raise InvalidInput(f"{actor.name} is the player character and cannot go into the wind")
        record(db, campaign_id, ActorMoved(name=actor.name, location=None))
        db.commit()
        return {"moved": actor.name, "location": "in the wind"}
    where = _place_named(state, location)
    record(db, campaign_id, ActorMoved(name=actor.name, location=where.name))
    # Going somewhere in person means the player character knows the place in detail.
    if actor.kind == PC_KIND and not where.discovered:
        record(db, campaign_id, LocationDiscovered(name=where.name))
    # Hirelings travel with the player character who employs them.
    if actor.kind == PC_KIND and not actor.hireling:
        for hireling in state.characters.values():
            if hireling.hireling and hireling.employer == actor.name:
                record(db, campaign_id, ActorMoved(name=hireling.name, location=where.name))
    db.commit()
    return {"moved": actor.name, "location": where.name}


def add_note(
    db: Session, campaign_id: int, text: str, subject: str | None = None
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    text = bounded_text(text, "text", MAX_TEXT)
    subject_name: str | None = None
    label = _optional_text(subject, "subject")
    if label is not None and label.casefold() != CAMPAIGN_WIDE:
        entry = state.named(label)
        if entry is None:
            raise InvalidInput(f"there is no location or character named {label!r}")
        subject_name = entry.name
    note_id = record(db, campaign_id, NoteAdded(text=text, subject=subject_name))
    db.commit()
    return {
        "noted": True,
        "id": note_id,
        "subject": subject_name if subject_name is not None else CAMPAIGN_WIDE,
    }


def update_note(db: Session, campaign_id: int, note_id: int, text: str) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    _note_with_id(state, note_id)
    record(db, campaign_id, NoteUpdated(note_id=note_id, text=bounded_text(text, "text", MAX_TEXT)))
    db.commit()
    return {"updated": "note", "id": note_id}


def delete_note(db: Session, campaign_id: int, note_id: int) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    note = _note_with_id(state, note_id)
    record(db, campaign_id, NoteRemoved(note_id=note_id, text=note.text))
    db.commit()
    return {"deleted": "note", "id": note_id}


def search_notes(state: CampaignState, query: str) -> dict[str, list[dict[str, Any]]]:
    """Notes whose text or subject name contains every word of `query`, newest first."""
    words = _words(query)
    matches: list[dict[str, Any]] = []
    for note in reversed(state.notes):
        if _has_words(words, note.subject, note.text):
            matches.append({"id": note.id, "subject": note.subject, "text": note.text})
    return {"notes": matches}


def all_at_location(state: CampaignState, location: str | None) -> dict[str, Any]:
    """A location as the GM needs it: its description, the NPCs there, whether the player character
    is there, and the notes about the place or the NPCs in it. Without a location, it is the
    player character's location. Statblocks are left out; `get_statblock` returns them."""
    if location is None:
        pc = state.pc()
        if pc is None or pc.location is None:
            raise InvalidInput("the player character is not at a location yet; name a location")
        location = pc.location
    place = _place_named(state, location)
    npcs = _npcs_at(state, place.name)
    pc = state.pc()
    subjects = {place.name, *(npc.name for npc in npcs)}
    return {
        "location": _location_entry(place),
        "player_character_here": pc is not None and pc.location == place.name,
        "npcs": [_npc_summary(npc) for npc in npcs],
        "notes": [
            {"id": note.id, "subject": note.subject, "text": note.text}
            for note in reversed(state.notes)
            if note.subject in subjects
        ],
    }


def search_location(state: CampaignState, query: str) -> dict[str, list[dict[str, Any]]]:
    """Locations whose name or description contains every word of `query`."""
    words = _words(query)
    matches = [
        _location_entry(place)
        for place in state.places.values()
        if _has_words(words, place.name, place.description)
    ]
    return {"locations": matches}


def search_npc(state: CampaignState, query: str) -> dict[str, list[dict[str, Any]]]:
    """NPCs anywhere in the world whose details contain every word of `query`."""
    words = _words(query)
    matches = [
        _npc_summary(npc)
        for npc in state.characters.values()
        if npc.kind == NPC_KIND and _has_words(words, *_npc_words(npc))
    ]
    return {"npcs": matches}


def lookup_index(state: CampaignState) -> dict[str, list[dict[str, Any]]]:
    """Every location and NPC by name, each with a one-sentence brief. The brief is enough to know
    what to look up; the full entry comes from the lookup tools."""
    locations = [
        {"name": place.name, "brief": brief(place.description)} for place in state.places.values()
    ]
    npcs = [
        {"name": npc.name, "location": npc.location, "brief": brief(npc.description)}
        for npc in state.characters.values()
        if npc.kind == NPC_KIND
    ]
    return {"locations": locations, "npcs": npcs}


def get_statblock(
    state: CampaignState,
    name: str,
    spec: CharacterSpec,
    combat: CombatRules | None = None,
    conditions: Mapping[str, ConditionRules] | None = None,
    inventory: InventoryRules | None = None,
) -> dict[str, Any]:
    """A character's stats, template, features, and fields, one line each. Works for the player
    character and for NPCs. With a rule set's `combat`, the armor, attacks, harm, and status follow.
    With its `conditions` and `inventory`, the conditions, injuries, need, and items follow."""
    character = _character_named(state, name)
    kind = "player character" if character.kind == PC_KIND else "npc"
    lines = describe(spec, _sheet_of(character)).splitlines()
    if combat is not None:
        lines += _combat_lines(character, spec, combat)
    lines += _status_lines(character, conditions, inventory)
    return {
        "name": character.name,
        "kind": kind,
        "location": character.location,
        "statblock": lines,
    }


def _combat_lines(character: Character, spec: CharacterSpec, combat: CombatRules) -> list[str]:
    lines = [f"Armor: {character.armor}"]
    if character.large_group:
        lines.append(
            "Large group: a group that attacks with Blast and is hit only by impaired attacks"
        )
    if character.attacks:
        attacks = ", ".join(f"{name} {formula}" for name, formula in character.attacks.items())
        lines.append(f"Attacks: {attacks}")
    if any(character.harm.values()):
        harm = ", ".join(
            f"{spec.stats[stat].name} {amount}" for stat, amount in character.harm.items() if amount
        )
        lines.append(f"Harm: {harm}")
    if character.defeated:
        lines.append(f"Status: {defeat_status(character, combat.defeat)}")
    elif character.fled:
        lines.append("Status: fled")
    elif character.surrendered:
        lines.append("Status: surrendered")
    elif character.critical_due:
        lines.append("Status: broken, awaiting the Warden's decision (resolve_group_break)")
    elif character.save_due is not None:
        lines.append(f"Status: {spec.stats[character.save_due].name} save due")
    return lines


def _required[T](value: T | None, what: str) -> T:
    if value is None:
        raise InvalidInput(f"this rule set does not track {what}")
    return value


def _status_lines(
    character: Character,
    conditions: Mapping[str, ConditionRules] | None,
    inventory: InventoryRules | None,
) -> list[str]:
    """The character's conditions, injuries, need, and what they carry. Each part shows only
    when the rule set has it, or when the character has something to report."""
    lines: list[str] = []
    if conditions is not None:
        held = [rules.label for rules in active_conditions(character, conditions)]
        lines.append(f"Conditions: {', '.join(held) if held else 'none'}")
    if character.injuries:
        lines.append(f"Injuries: {'; '.join(character.injuries)}")
    if character.unmet_since is not None:
        lines.append(f"Deprived: {character.unmet_cause} (since day {character.unmet_since})")
    if inventory is not None:
        used = inventory_service.used_slots(character)
        flag = " (over capacity)" if used > inventory.capacity else ""
        lines.append(f"Inventory: {used} of {inventory.capacity} slots{flag}")
        carried = ", ".join(item.name for item in character.items)
        lines.append(f"Carrying: {carried or 'nothing'}")
    return lines


def speaker_profile(state: CampaignState, name: str, spec: CharacterSpec) -> str:
    """The NPC details a speech step needs. The NPC must be at the player character's location."""
    npc = _character_named(state, name)
    if npc.kind != NPC_KIND and not npc.hireling:
        raise InvalidInput(f"{npc.name} is the player character and does not need speaking for")
    pc = state.pc()
    if pc is None or npc.location != pc.location:
        raise InvalidInput(f"{npc.name} is not at the player character's location")
    quirks = "; ".join(npc.quirks) or "none"
    lines = [f"Speaker: {npc.name}", f"Who they are: {npc.description}"]
    if npc.personality:
        lines.append(f"Personality: {npc.personality}")
    if npc.accent:
        lines.append(f"Accent: {npc.accent}")
    lines += [f"Quirks: {quirks}", f"Location: {npc.location}"]
    sheet = describe(spec, _sheet_of(npc))
    if sheet:
        lines.append(sheet)
    return "\n".join(lines)


def map_view(state: CampaignState) -> MapView:
    """The player's map. Only discovered places appear on it. Each marker is placed on a square
    centered on the origin, sized to fit the farthest discovered place plus a margin."""
    places = sorted(
        (place for place in state.places.values() if place.discovered),
        key=lambda place: name_key(place.name),
    )
    placed = [place for place in places if place.position is not None]
    reach = max(
        [MIN_MAP_RADIUS_KM]
        + [max(abs(p.position.x), abs(p.position.y)) for p in placed if p.position is not None]
    )
    radius = reach * MAP_MARGIN
    pc = state.pc()
    here = pc.location if pc is not None else None
    labels = token_labels(place.name for place in places)
    markers = [
        MapMarker(
            name=place.name,
            label=labels[place.name],
            left=round(50 + place.position.x / radius * 50, 2),
            top=round(50 - place.position.y / radius * 50, 2),
            here=place.name == here,
        )
        for place in placed
        if place.position is not None
    ]
    return MapView(radius_km=round(radius, 2), markers=markers, places=places)


def scene_view(
    state: CampaignState, spec: CharacterSpec, inventory: InventoryRules | None = None
) -> SceneView:
    """The player's view. It shows only what is at the player character's location, including any
    hirelings there. The pack shows the player character's items when the rule set has an inventory."""
    pc = state.pc()
    location = pc.location if pc is not None else None
    found = state.named(location) if location is not None else None
    place = found if isinstance(found, Place) else None
    npcs = _npcs_at(state, place.name) if place is not None else []
    subjects = {CAMPAIGN_WIDE}
    if pc is not None:
        subjects.add(pc.name)
    if place is not None:
        subjects.add(place.name)
    subjects.update(npc.name for npc in npcs)
    notes = [note for note in reversed(state.notes) if note.subject in subjects]
    sheet = describe(spec, _sheet_of(pc)).splitlines() if pc is not None else []
    pack = None
    if pc is not None and inventory is not None:
        pack = PackView(
            items=[
                PackItem(
                    name=item.name,
                    size=item.size,
                    slots=item.slots,
                    uses=item.uses,
                    max_uses=item.max_uses,
                    kind=item.kind,
                    text=item.text,
                )
                for item in pc.items
            ],
            used=inventory_service.used_slots(pc),
            capacity=inventory.capacity,
        )
    hirelings = tuple(
        c
        for c in state.characters.values()
        if c.hireling and place is not None and c.location == place.name
    )
    objects = tuple(visible_objects(state, place.name)) if place is not None else ()
    return SceneView(
        pc=pc,
        location=place,
        npcs=npcs,
        notes=notes,
        sheet=sheet,
        pack=pack,
        hirelings=hirelings,
        objects=objects,
    )


def world_state_text(
    state: CampaignState,
    campaign: CampaignSpec,
    grid: EncounterGrid | None = None,
    combat: CombatRules | None = None,
    conditions: Mapping[str, ConditionRules] | None = None,
    clock_rules: ClockRules | None = None,
    inventory: InventoryRules | None = None,
) -> str:
    """The GM's orientation at the start of a turn: the campaign options, the day, the player
    character and where they are, the NPCs there, and any encounter in progress. The rest is looked
    up with the lookup tools."""
    pc = state.pc()
    if pc is None:
        return ""
    lines = [
        f"Campaign {campaign.fields[field_id].name.lower()}: {value}."
        for field_id, value in state.options.items()
        if field_id in campaign.fields
    ]
    if clock_rules is not None:
        when = f"day {state.clock.day}, {clock_rules.parts[state.clock.part]}"
        if clock_rules.hours_per_part > 1:
            when += f", hour {state.clock.hour + 1} of {clock_rules.hours_per_part}"
        lines.append(f"It is {when}.")
    if pc.dead:
        where = f" the body lies at {pc.location}." if pc.location is not None else ""
        lines.append(f"Player character {pc.name} is dead;{where}".rstrip(".") + ".")
    elif pc.location is None:
        lines.append(
            f"Player character: {pc.name}, not yet placed; place them with move_actor on your first turn."
        )
    else:
        lines.append(f"Player character: {pc.name}, at {pc.location}.")
        names = [
            f"{npc.name} (story only, no stats yet)" if npc.story_only else npc.name
            for npc in _npcs_at(state, pc.location)
        ]
        lines.append(f"NPCs here: {', '.join(names) if names else 'none'}.")
        things = [o.contents for o in visible_objects(state, pc.location)]
        if things:
            lines.append(f"Objects here: {'; '.join(things)}.")
    hirelings = [c for c in state.characters.values() if c.hireling]
    if hirelings:
        lines.append(
            "Hirelings, controlled by the player: "
            + "; ".join(
                f"{h.name}, at {h.location or 'no location'}, hired by {h.employer} "
                f"at {h.wage} a day"
                for h in hirelings
            )
            + "."
        )
    wind = _npcs_in_the_wind(state)
    if wind:
        lines.append(f"NPCs in the wind, at no location: {', '.join(wind)}.")
    lines.extend(_status_lines(pc, conditions, inventory))
    lines.extend(_encounter_lines(state, grid, combat))
    return "\n".join(lines)


def _encounter_lines(
    state: CampaignState, grid: EncounterGrid | None, combat: CombatRules | None
) -> list[str]:
    view = encounters.encounter_view(state, grid, combat)
    if view is None:
        return []
    lines = [
        f"Encounter in progress on the map {view['map']} at {view['location']}, round {view['round']}.",
        f"Map: {view['columns']} by {view['rows']} cells, columns and rows counted from 0 at the top left. {view['description']}",
    ]
    if view["awaiting_initiative"]:
        waiting = ", ".join(view["awaiting_initiative"])
        lines.append(
            f"Initiative is not set yet. {waiting} must roll for initiative with player_initiative."
        )
    if view["acting"] is not None:
        left = (
            f", with {view['moves_left']} of {view['move_cells']} cells of movement left"
            if view["moves_left"] is not None
            else ""
        )
        lines.append(f"Taking their turn now: {view['acting']}{left}.")
    if not view["participants"]:
        lines.append("No participants yet.")
    for who in view["participants"]:
        cell = who["cell"]
        where = f"column {cell[0]}, row {cell[1]}" if cell else "not yet placed"
        kind = "player character" if who["is_pc"] else "NPC"
        status = f", {who['status']}" if who["status"] else ""
        lines.append(
            f"- {who['number']}. {who['name']} ({kind}, {who['side']} side{status}), {where}."
        )
    return lines


def _location_entry(place: Place) -> dict[str, Any]:
    position = None
    if place.position is not None:
        position = {"x": place.position.x, "y": place.position.y}
    return {
        "name": place.name,
        "description": place.description,
        "discovered": place.discovered,
        "position_km": position,
        "parent": place.parent,
    }


def _npcs_at(state: CampaignState, location: str) -> list[Character]:
    return [c for c in state.characters.values() if c.kind == NPC_KIND and c.location == location]


def present_rules_text(state: CampaignState, rules: Mapping[str, str]) -> str:
    """The text of the rules attached to what is present: the NPCs at the player character's
    location, defeated or not, and the items the player character carries. Empty when nothing
    is attached."""
    pc = state.pc()
    if pc is None:
        return ""
    sources: dict[str, list[str]] = {}
    if pc.location is not None:
        for npc in _npcs_at(state, pc.location):
            for rule_id in npc.rules:
                sources.setdefault(rule_id, []).append(npc.name)
    for item in pc.items:
        for rule_id in item.rules:
            sources.setdefault(rule_id, []).append(item.name)
    lines = [
        f"- {rule_id} (from {', '.join(names)}): {rules.get(rule_id, '')}"
        for rule_id, names in sources.items()
    ]
    if not lines:
        return ""
    return "\n".join(
        ["These rules belong to the NPCs and items present now. Follow them as written.", *lines]
    )


def _npcs_in_the_wind(state: CampaignState) -> list[str]:
    return sorted(
        c.name for c in state.characters.values() if c.kind == NPC_KIND and c.location is None
    )


def _npc_summary(npc: Character) -> dict[str, Any]:
    return {
        "name": npc.name,
        "location": npc.location,
        "description": npc.description,
        "personality": npc.personality,
        "accent": npc.accent,
        "quirks": list(npc.quirks),
        "defeated": npc.defeated,
    }


def _npc_words(npc: Character) -> list[str]:
    return [npc.name, npc.location or "", npc.description, npc.personality, npc.accent, *npc.quirks]


def _npc_event(
    tables: Mapping[str, Sequence[TableOption]],
    rng: random.Random,
    spec: CharacterSpec,
    *,
    name: str,
    location: str,
    description: str,
    personality: str | None,
    accent: str | None,
    quirks: Sequence[str] | None,
    character: Mapping[str, Any] | None = None,
    plus: str,
    story_only: bool = False,
    rules: Sequence[str] = (),
) -> NpcAdded:
    """The event that adds an NPC. A trait or character option that is not given is picked at
    random, so the picked values are stored in the event and replays do not roll again. A
    story-only NPC gets no stats at all."""
    trait_personality = personality or rng.choice(tables["personality"]).value
    trait_accent = accent or rng.choice(tables["accent"]).value
    if quirks is None:
        pool = [option.value for option in tables["quirk"]]
        trait_quirks = rng.sample(pool, min(QUIRKS_PER_NPC, len(pool)))
    else:
        trait_quirks = list(quirks)
    sheet_fields: dict[str, Any] = {}
    if not story_only:
        sheet = resolve(spec, character or {}, rng, plus=plus)
        _check_sheet_text(sheet)
        sheet_fields = _sheet_fields(sheet)
    return NpcAdded(
        name=name,
        location=location,
        description=description,
        personality=trait_personality,
        accent=trait_accent,
        quirks=trait_quirks,
        story_only=story_only,
        rules=list(rules),
        **sheet_fields,
    )


def _sheet_fields(sheet: Sheet) -> dict[str, Any]:
    return {
        "stats": dict(sheet.stats),
        "templates": dict(sheet.templates),
        "features": dict(sheet.features),
        "fields": dict(sheet.fields),
    }


def _sheet_of(character: Character) -> Sheet:
    return Sheet(
        stats=character.stats,
        templates=character.templates,
        features=character.features,
        fields=character.fields,
    )


def _check_sheet_text(sheet: Sheet) -> None:
    for value in sheet.fields.values():
        if len(value) > MAX_TEXT:
            raise InvalidInput(f"character text must be at most {MAX_TEXT} characters")


def _ensure_free_name(state: CampaignState, name: str) -> None:
    if state.named(name) is not None:
        raise InvalidInput(f"the name {name!r} is already used by a location or character")


def _place_named(state: CampaignState, name: str) -> Place:
    entry = state.named(name)
    if not isinstance(entry, Place):
        raise InvalidInput(f"there is no location named {name!r}")
    return entry


def _children_of(state: CampaignState, name: str) -> list[str]:
    return sorted(p.name for p in state.places.values() if p.parent == name)


def _parent_for_new_place(
    state: CampaignState, parent: str, x: float | None, y: float | None
) -> str:
    """Check a place to be created inside another. Such a place has no point of its own, and only
    a top-level place can hold others."""
    if x is not None or y is not None:
        raise InvalidInput("a place inside another has no map position; leave out x and y")
    outer = _place_named(state, parent)
    if outer.parent is not None:
        raise InvalidInput(
            f"{outer.name} is itself inside {outer.parent}; place it inside {outer.parent} instead"
        )
    return outer.name


def _ensure_open_point(
    state: CampaignState, name: str, x: float | None, y: float | None, force: bool
) -> None:
    """Refuse a point that another place already has, unless the GM passes force. Places do not
    share a point by default, so their pins never sit on top of each other."""
    if not isinstance(force, bool):
        raise InvalidInput("force must be true or false")
    if x is None or y is None or force:
        return
    for other in state.places.values():
        if other.name == name or other.position is None:
            continue
        if abs(other.position.x - x) < SAME_POINT_KM and abs(other.position.y - y) < SAME_POINT_KM:
            raise InvalidInput(
                f"{other.name!r} is already at that point. Choose another point, or pass "
                "force=true to put both places there."
            )


def _character_named(state: CampaignState, name: str) -> Character:
    entry = state.named(name)
    if not isinstance(entry, Character):
        raise InvalidInput(f"there is no character named {name!r}")
    return entry


def _npc_named(state: CampaignState, name: str) -> Character:
    npc = _character_named(state, name)
    if npc.kind != NPC_KIND:
        raise InvalidInput(f"{npc.name} is the player character, not an NPC")
    return npc


def _note_with_id(state: CampaignState, note_id: int) -> Note:
    note = next((n for n in state.notes if n.id == note_id), None)
    if note is None:
        raise InvalidInput(f"there is no note with id {note_id}")
    return note


MAX_MARKET_RESULTS = 20


def _words(query: str) -> list[str]:
    return bounded_text(query, "query", MAX_TEXT).casefold().split()


def _has_words(words: Sequence[str], *parts: str) -> bool:
    haystack = " ".join(parts).casefold()
    return all(word in haystack for word in words)


def _optional_text(value: object, label: str) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return bounded_text(value, label, MAX_NAME)


def _coordinates(x: object, y: object) -> tuple[float | None, float | None]:
    """Both map coordinates or neither. Neither means the place has no position on the map."""
    if x is None and y is None:
        return None, None
    if x is None or y is None:
        raise InvalidInput("give both x and y, or neither")
    return _kilometers(x, "x"), _kilometers(y, "y")


def _kilometers(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise InvalidInput(f"{label} must be a number of kilometers from the map's center")
    return float(value)


def _quirks(value: Sequence[str] | None) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str) or len(value) > MAX_QUIRKS:
        raise InvalidInput(f"quirks must be a list of up to {MAX_QUIRKS} short texts")
    return [bounded_text(item, "each quirk", MAX_NAME) for item in value]
