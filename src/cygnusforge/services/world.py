"""Places, characters, and notes: the world that a campaign's GM edits.

Writes check the campaign's current state, record events, and commit. Reads work on a state that
is already folded from the log. Problems the model can fix are raised as `InvalidInput`, and their
messages are returned to the model as they are."""

from __future__ import annotations

import math
import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from difflib import get_close_matches
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
    DEFAULT_TRAVEL_MODE,
    ClockRules,
    CombatRules,
    ConditionRules,
    DiceTable,
    EncounterGrid,
    EncounterObject,
    EncounterTemplate,
    EncounterTerrain,
    FoodRules,
    InventoryRules,
    MagicRules,
    Marketplace,
    MonsterTemplate,
    NeedRules,
    RestLength,
    ShopItem,
    Travel,
    Wilderness,
    WorldTemplate,
    WorldTerrain,
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
from cygnusforge.services import wilderness as wilderness_service
from cygnusforge.services.attacks import ATTACK_FLAGS
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
    JournalAppended,
    JournalReplaced,
    LocationAdded,
    LocationDiscovered,
    LocationRemoved,
    LocationSummarized,
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
    Region,
    RegionAdded,
    SeasonSet,
    TravelCharged,
    WorldObject,
    defeat_status,
    load_state,
    name_key,
    record,
    same_item_name,
    token_labels,
)
from cygnusforge.services.geometry import clearance, contains, overlaps, polygon_problem
from cygnusforge.services.objects import visible_objects
from cygnusforge.services.tools import check_arguments

MAX_NAME = 120
MAX_STAT = 999
MAX_TEXT = 2000
# A player-facing summary of a place: a sentence or two, not the place's full description.
MAX_SUMMARY = 300
MAX_QUIRKS = 10
QUIRKS_PER_NPC = 2
# The map always shows at least this many kilometers from its center, and leaves a margin.
MIN_MAP_RADIUS_KM = 1.0
MAP_MARGIN = 1.1
# A region outline may have at most this many corners.
MAX_REGION_CORNERS = 200
# A place given a map point must lie this far inside a region's edge, so the ground around it shows.
REGION_BUFFER_KM = 0.5
# Two places count as sharing a point when their positions agree to this many kilometers.
SAME_POINT_KM = 0.01


@dataclass(frozen=True)
class SceneView:
    """What the player may see: the player character, the place they are in, the NPCs there, the
    notes that concern them, the player character's statblock, and the journal."""

    pc: Character | None
    location: Place | None
    npcs: list[Character]
    notes: list[Note]
    sheet: list[str]
    journal: str = ""
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
    damage: str = ""


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
    icon: str | None = None


@dataclass(frozen=True)
class MapRegion:
    """An area of ground on the map. Its points are percentages of the map's width and height.
    `name` and `text` are None until the region is known, and `text` is what hovering it shows.
    `pattern` is the id of the texture drawn over its colour, when its kind has one."""

    name: str | None
    text: str | None
    color: str
    points: tuple[tuple[float, float], ...]
    pattern: str | None = None


@dataclass(frozen=True)
class MapPattern:
    """A texture drawn over a kind of ground. `id` names it in the map's SVG, `shape` is one of
    TERRAIN_PATTERNS, and `ink` colours its marks."""

    id: str
    shape: str
    ink: str


@dataclass(frozen=True)
class TimeView:
    """The campaign day and the part of that day, shown beside the map."""

    day: int
    part: str
    night: bool


@dataclass(frozen=True)
class MapView:
    """The player's map. Only discovered places appear on it. The map is centered on the player
    character's place, and places with a position are placed on a square sized to fit the farthest
    one. Every discovered place is listed. `where` holds each place's line of location text, and
    `origin` is the percentage position of the true origin, or None when it is off the board.
    """

    radius_km: float
    markers: list[MapMarker]
    places: list[Place]
    where: dict[str, str] = field(default_factory=dict)
    origin: tuple[float, float] | None = None
    regions: list[MapRegion] = field(default_factory=list)
    patterns: list[MapPattern] = field(default_factory=list)


# How a player character moves: overland takes watches, in a settlement takes minutes at a speed,
# and in a dungeon takes no clock time.
OVERLAND = "overland"
IN_SETTLEMENT = "in_settlement"
IN_DUNGEON = "in_dungeon"
MOVE_MODES = (OVERLAND, IN_SETTLEMENT, IN_DUNGEON)


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
    # The rule set's encounter terrain, objects, and map templates by id.
    terrain: Mapping[str, EncounterTerrain] = field(default_factory=dict)
    encounter_objects: Mapping[str, EncounterObject] = field(default_factory=dict)
    encounter_templates: Mapping[str, EncounterTemplate] = field(default_factory=dict)
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
    # The rule set's HP tiers for NPCs without a statblock, by tier name.
    npc_hp: Mapping[str, int] = field(default_factory=dict)
    # The rule set's travel modes and default leg, or None when it does not charge travel time.
    travel: Travel | None = None
    # The rule set's wilderness calendar, weather, and Wilderness Actions, or None when it has none.
    wilderness: Wilderness | None = None
    # The kinds of ground a world map's regions can be, by id.
    world_terrain: Mapping[str, WorldTerrain] = field(default_factory=dict)

    def create_region(
        self,
        name: str,
        terrain: str,
        points: Sequence[Sequence[float]],
        force: bool = False,
        discovered: bool = False,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return create_region(
                db,
                self.campaign_id,
                self.world_terrain,
                name,
                terrain,
                points,
                force=force,
                discovered=discovered,
            )

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

    def _give_catalogue_item(self, db: Session, name: str, item: str) -> bool:
        listed = self._listed(item)
        if listed is None:
            raise InvalidInput(f"this rule set has no item named {item!r} to give")
        rules = _required(self.inventory, "inventory")
        if not inventory_service.fits_listed(
            db, self.campaign_id, rules, self.item_sizes, name, listed
        ):
            return False
        inventory_service.carry_listed(db, self.campaign_id, rules, self.item_sizes, name, listed)
        return True

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

    def wilderness_action(self, action: str, reason: str) -> dict[str, Any]:
        """Take a Wilderness Action for the current watch, then spend that watch. The action rolls
        the day's weather and the events; the last part of the day counts as night."""
        wilderness = _required(self.wilderness, "wilderness")
        clock_rules = _required(self.clock_rules, "time")
        with session_scope(self.engine) as db:
            state = load_state(db, self.campaign_id)
            day = state.clock.day
            night = state.clock.part == len(clock_rules.parts) - 1
            season = wilderness_service.season_for_day(
                wilderness, state.start_season or wilderness.start_season, day
            )
            # Living PCs and hirelings. Player characters come first, so a grant goes to one of them.
            party = sorted(
                (c for c in state.characters.values() if c.kind == PC_KIND and not c.dead),
                key=lambda c: c.hireling,
            )
            result = wilderness_service.wilderness_action(
                db,
                self.campaign_id,
                action,
                reason,
                wilderness,
                self.dice_tables,
                self.rng,
                plus=self.plus,
                day=day,
                season=season,
                at=clock_service.absolute(
                    state.clock, len(clock_rules.parts), clock_rules.hours_per_part
                ),
                party=[c.name for c in party],
                night=night,
                give=self._give_catalogue_item,
            )
        with session_scope(self.engine) as db:
            result["clock"] = clock_service.advance_time(
                db, self.campaign_id, clock_rules, self.need, self.inventory, days=0, parts=1
            )
        return result

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

    def place_names(self) -> list[str]:
        with session_scope(self.engine) as db:
            state = load_state(db, self.campaign_id)
            return sorted(place.name for place in state.places.values())

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

    def discover_location(self, name: str, summary: str | None = None) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return discover_location(db, self.campaign_id, name, summary)

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
                npc_hp=self.npc_hp,
                hp_tier=hp_tier,
                pronouns=pronouns,
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
        pronouns: str | None = None,
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
                pronouns=pronouns,
                plus=self.plus,
            )

    def promote_hireling(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return promote_hireling(db, self.campaign_id, name)

    def dismiss_hireling(self, name: str, cause: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return dismiss_hireling(db, self.campaign_id, name, cause)

    def move_actor(
        self,
        name: str,
        location: str | None = None,
        travel: str = DEFAULT_TRAVEL_MODE,
        mode: str = IN_SETTLEMENT,
        route: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if mode not in MOVE_MODES:
            names = ", ".join(MOVE_MODES)
            raise InvalidInput(f"there is no move mode named {mode!r}; use one of {names}")
        if mode == OVERLAND and location is not None:
            with session_scope(self.engine) as db:
                actor = _character_named(load_state(db, self.campaign_id), name)
            if actor.kind == PC_KIND:
                return self._overland_move(actor.name, location, route)
        with session_scope(self.engine) as db:
            return move_actor(
                db,
                self.campaign_id,
                name,
                location,
                travel=self.travel,
                speed=travel,
                mode=mode,
                clock_rules=self.clock_rules,
                need=self.need,
                inventory=self.inventory,
            )

    def _overland_move(
        self, name: str, location: str, route: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        """An overland move takes watches. Each watch rolls for the party getting lost, takes the
        Travel Wilderness Action (weather, events, night hooks), and advances the clock one part.
        A lost watch makes the next one a recovery that makes no progress. The move places the
        character once the last progress watch is spent."""
        wilderness = _required(self.wilderness, "wilderness")
        clock_rules = _required(self.clock_rules, "time")
        if route is None:
            raise InvalidInput("an overland move needs a route with path, distance, and terrain")
        inventory = _required(self.inventory, "inventory")
        with session_scope(self.engine) as db:
            state = load_state(db, self.campaign_id)
            actor = _character_named(state, name)
            where = _place_named(state, location)
            party = _travelling_party(state, actor)
            start = state.clock
            season = wilderness_service.season_for_day(
                wilderness, state.start_season or wilderness.start_season, start.day
            )
            deprived = any(c.unmet_since is not None for c in party)
            weather = wilderness_service.roll_weather(
                db,
                self.campaign_id,
                wilderness,
                self.dice_tables,
                self.rng,
                plus=self.plus,
                day=start.day,
                season=season,
            )
            plan = wilderness_service.plan_overland(wilderness, route, weather["weather"], deprived)
            if plan.fatigue:
                for member in party:
                    inventory_service.add_penalty(
                        db, self.campaign_id, inventory, member.name, cause="travel"
                    )
        journey: list[dict[str, Any]] = []
        progress = 0
        recovering = False
        while progress < plan.watches:
            with session_scope(self.engine) as db:
                state = load_state(db, self.campaign_id)
                clock = state.clock
                season = wilderness_service.season_for_day(
                    wilderness, state.start_season or wilderness.start_season, clock.day
                )
                names = [m.name for m in _travelling_party(state, _character_named(state, name))]
                lost = wilderness_service.roll_lost(
                    db,
                    self.campaign_id,
                    route["path"],
                    wilderness,
                    self.rng,
                    reason=f"overland travel to {where.name}",
                )
                watch = wilderness_service.wilderness_action(
                    db,
                    self.campaign_id,
                    wilderness_service.TRAVEL_ACTION,
                    f"overland travel to {where.name}",
                    wilderness,
                    self.dice_tables,
                    self.rng,
                    plus=self.plus,
                    day=clock.day,
                    season=season,
                    at=clock_service.absolute(
                        clock, len(clock_rules.parts), clock_rules.hours_per_part
                    ),
                    party=names,
                    night=clock.part == len(clock_rules.parts) - 1,
                    give=self._give_catalogue_item,
                    travel_watch=True,
                )
            with session_scope(self.engine) as db:
                clock_now = clock_service.advance_time(
                    db, self.campaign_id, clock_rules, self.need, self.inventory, days=0, parts=1
                )
            made_progress = not recovering
            if made_progress:
                progress += 1
            journey.append(
                {
                    "lost": lost["lost"],
                    "roll": lost["roll"],
                    "progress": made_progress,
                    "weather": watch["weather"],
                    "events": watch["events"],
                    "clock": clock_now,
                }
            )
            recovering = lost["lost"]
        with session_scope(self.engine) as db:
            result = move_actor(
                db,
                self.campaign_id,
                name,
                location,
                travel=self.travel,
                mode=OVERLAND,
                clock_rules=self.clock_rules,
                need=self.need,
                inventory=self.inventory,
            )
        result.update(
            {
                "watches": plan.watches,
                "terrain": plan.terrain,
                "fatigue": plan.fatigue,
                "lost_watches": sum(1 for w in journey if w["lost"]),
                "journey": journey,
            }
        )
        return result

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
        attacks: Sequence[Mapping[str, Any]] | None = None,
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
        columns: int | None,
        rows: int | None,
        description: str,
        template: str = "",
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.create_map(
                db,
                self.campaign_id,
                name,
                location,
                columns,
                rows,
                description,
                template,
                self.encounter_templates,
            )

    def set_encounter_map_cells(self, map: str, cells: list[Mapping[str, Any]]) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return encounters.set_map_cells(
                db, self.campaign_id, map, cells, self.terrain, self.encounter_objects
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
        attacks: Sequence[Mapping[str, Any]] | None = None,
        force: bool = False,
        detachment: bool | None = None,
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
                detachment=detachment,
            )

    def search_monster(self, query: str) -> dict[str, list[dict[str, Any]]]:
        return search_monster(self.monsters, query)

    def search_marketplace(self, query: str) -> dict[str, Any]:
        return search_marketplace(_required(self.marketplace, "a marketplace"), query)

    def attack(
        self,
        attacker: str,
        attack: str,
        target: str,
        modifier: str = "",
        targets: Sequence[str] | None = None,
    ) -> dict[str, Any]:
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
                targets=targets,
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
            return encounters.place_participant(
                db,
                self.campaign_id,
                name,
                column,
                row,
                self.grid,
                self.terrain,
                self.encounter_objects,
            )

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

    def append_journal(self, text: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return append_journal(db, self.campaign_id, text)

    def replace_in_journal(self, old_text: str, new_text: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return replace_in_journal(db, self.campaign_id, old_text, new_text)

    def search_journal(self, query: str, context: int = 1) -> dict[str, list[dict[str, Any]]]:
        with session_scope(self.engine) as db:
            return search_journal(load_state(db, self.campaign_id), query, context)

    def view_journal(self, start_line: int = 1, end_line: int | None = None) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return view_journal(load_state(db, self.campaign_id), start_line, end_line)

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

    def speaker_at_player(self, name: str) -> bool:
        with session_scope(self.engine) as db:
            return speaker_at_player(load_state(db, self.campaign_id), name)


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
    npc_hp: Mapping[str, int],
    wilderness: Wilderness | None = None,
) -> None:
    """Record a campaign's places and the world's NPCs as events. The player character is made
    later, by `create_player_character`. An NPC that names a statblock takes its stats, and an
    NPC with an HP tier and no statblock takes that tier's HP. A rule set with a wilderness
    calendar also records the season the campaign starts in."""
    if wilderness is not None:
        requested = world.start.season if world.start is not None else None
        season = wilderness_service.start_season_for(wilderness, requested)
        record(db, campaign_id, SeasonSet(season=season))
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
                icon=spot.icon,
                rules=list(spot.rules),
            ),
        )
    for region in world.regions:
        record(
            db,
            campaign_id,
            RegionAdded(
                name=region.name,
                terrain=region.terrain,
                points=[[x, y] for x, y in region.points],
            ),
        )
    for npc in world.npcs:
        base = None if npc.monster is None else monsters[npc.monster]
        if base is not None:
            character: dict[str, Any] | None = {"stats": dict(base.stats)}
        elif npc.hp_tier is not None:
            character = {"stats": {"hp": npc_hp[npc.hp_tier]}}
        else:
            character = None
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
            character=character,
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
    pronouns: str,
    plus: str,
) -> dict[str, Any]:
    """Make the player character from the player's choices. `rolled` are the stats rolled for
    this character; the player may only keep them or swap within a swap group. Starting item
    features are carried as inventory items, spell features as spellbooks, and the armor they
    give is worn. The currency stays a count. The pronouns are how the GM refers to the character."""
    state = load_state(db, campaign_id)
    current = state.pc()
    if current is not None and not current.dead:
        raise InvalidInput("the player character already exists")
    if campaign.fields and not state.options:
        raise InvalidInput("the campaign options must be chosen before the player character")
    name = bounded_text(name, "name", MAX_NAME)
    pronouns = bounded_text(pronouns, "pronouns", MAX_NAME)
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
    record(db, campaign_id, PcAdded(name=name, pronouns=pronouns, **fields))
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
    _ensure_no_same_place(state, name)
    text = bounded_text(description, "description", MAX_TEXT)
    if not isinstance(discovered, bool):
        raise InvalidInput("discovered must be true or false")
    px, py = _coordinates(x, y)
    inside = None
    if parent is not None:
        inside = _parent_for_new_place(state, parent, px, py)
        px, py = None, None
    else:
        _ensure_on_terrain(state, name, px, py)
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


def _ensure_on_terrain(state: CampaignState, name: str, x: float | None, y: float | None) -> None:
    """A place with a map point must sit inside a region with ground around it, so the map shows
    the terrain under the place rather than blank space."""
    if x is None or y is None:
        return
    ground = [region.points for region in state.regions.values()]
    if clearance((x, y), ground) < REGION_BUFFER_KM:
        raise InvalidInput(
            f"{name!r} is not on the map's terrain. Create a region that covers the place and "
            f"extends at least {REGION_BUFFER_KM} km beyond it, using create_region, then add "
            "the location."
        )


def create_region(
    db: Session,
    campaign_id: int,
    kinds: Mapping[str, WorldTerrain],
    name: str,
    terrain: str,
    points: Sequence[Sequence[float]],
    force: bool = False,
    discovered: bool = False,
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    name = bounded_text(name, "name", MAX_NAME)
    if name_key(name) in state.regions:
        raise InvalidInput(f"there is already a region named {name!r}")
    if terrain not in kinds:
        choices = ", ".join(sorted(kinds)) or "none"
        raise InvalidInput(f"{terrain!r} is not a terrain kind. The terrain kinds are: {choices}.")
    corners = _region_corners(points)
    problem = polygon_problem(corners)
    if problem is not None:
        raise InvalidInput(
            f"the region {problem}. Give its corners in order around the edge, in kilometers "
            "from the map's center."
        )
    if not isinstance(force, bool):
        raise InvalidInput("force must be true or false")
    if not isinstance(discovered, bool):
        raise InvalidInput("discovered must be true or false")
    if not force:
        for other in state.regions.values():
            if overlaps(corners, other.points):
                raise InvalidInput(
                    f"it overlaps {other.name!r}. Choose other corners, or pass force=true to "
                    "overlap it."
                )
    record(
        db,
        campaign_id,
        RegionAdded(
            name=name,
            terrain=terrain,
            points=[[x, y] for x, y in corners],
            discovered=discovered,
        ),
    )
    db.commit()
    return {"created": "region", "name": name, "terrain": terrain, "discovered": discovered}


def _region_corners(points: Sequence[Sequence[float]]) -> list[tuple[float, float]]:
    if not isinstance(points, list | tuple) or len(points) > MAX_REGION_CORNERS:
        raise InvalidInput(f"points must be a list of at most {MAX_REGION_CORNERS} [x, y] corners")
    corners: list[tuple[float, float]] = []
    for point in points:
        if (
            not isinstance(point, list | tuple)
            or len(point) != 2
            or any(isinstance(value, bool) or not isinstance(value, int | float) for value in point)
        ):
            raise InvalidInput(
                "each point must be an [x, y] pair of numbers, in kilometers from the map's center"
            )
        corners.append((float(point[0]), float(point[1])))
    return corners


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
    npc_hp: Mapping[str, int],
    hp_tier: str | None = None,
    pronouns: str | None = None,
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
    if hp_tier is not None:
        character = _with_hp_tier(character, hp_tier, npc_hp, statblock=base is not None)
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
        pronouns=_optional_text(pronouns, "pronouns"),
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
        "pronouns": event.pronouns,
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
    pronouns: str | None = None,
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
        pronouns=_optional_text(pronouns, "pronouns"),
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
            pronouns=event.pronouns,
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
    attacks: Sequence[Mapping[str, Any]] | None,
    armor_max: int = 0,
    force: bool = False,
    detachment: bool | None = None,
) -> dict[str, Any]:
    """An NPC built from a statblock. A template gives the starting values, and any value given
    replaces it. Without a template, a name that matches a statblock uses it and takes no values
    of its own, unless force is set. A detachment flag overrides the statblock's group setting;
    without it, the statblock decides."""
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
    attack_map, flag_map = _attack_table(attacks, base)
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
        attack_flags=flag_map,
        creature=base.name if base else "",
        large_group=(detachment if detachment is not None else bool(base and base.large_group)),
        rules=list(base.rules) if base else [],
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
        "attack_flags": flag_map,
        "detachment": event.large_group,
    }


def _attack_table(
    attacks: Sequence[Mapping[str, Any]] | None, base: MonsterTemplate | None
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """The attacks an NPC gets: the given ones, else the statblock's, else none. Returns the dice by
    attack name and the flags of the attacks that have any."""
    if attacks is not None:
        return _attacks(attacks)
    if base is None:
        return {}, {}
    return dict(base.attacks), {name: list(flags) for name, flags in base.attack_flags.items()}


def _attacks(
    attacks: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Each attack is {"name", "dice", "flags"}. Its dice must be a formula such as '1d8' or
    '1d6+1d6', and flags is an optional list of names from ATTACK_FLAGS, each listed once. Returns
    the dice by attack name and the flags of the attacks that have any."""
    checked: dict[str, str] = {}
    flags_by_attack: dict[str, list[str]] = {}
    seen: set[str] = set()
    for attack in attacks:
        label = bounded_text(attack.get("name", ""), "attack name", MAX_NAME)
        dice = attack.get("dice", "")
        flags = attack.get("flags", [])
        if label.casefold() in seen:
            raise InvalidInput(f"attack {label!r} is listed twice")
        if not isinstance(dice, str) or not is_formula(dice):
            raise InvalidInput(f"attack {label!r} needs a dice formula such as '1d8' or '1d6+1d6'")
        if not isinstance(flags, list) or not all(isinstance(flag, str) for flag in flags):
            raise InvalidInput(f"attack {label!r} flags must be a list of names")
        unknown = [flag for flag in flags if flag not in ATTACK_FLAGS]
        if unknown or len(set(flags)) != len(flags):
            options = ", ".join(ATTACK_FLAGS)
            raise InvalidInput(f"attack {label!r} flags must each be listed once, from: {options}")
        seen.add(label.casefold())
        checked[label] = dice
        if flags:
            flags_by_attack[label] = list(flags)
    return checked, flags_by_attack


def _flag_text(flags: Sequence[str]) -> str:
    """The flags of one attack as a suffix for the record, such as " (blast)", or "" if none."""
    return f" ({', '.join(flags)})" if flags else ""


def _statblock_conflict(base: MonsterTemplate, spec: CharacterSpec) -> str:
    """The refusal when a named statblock creature is given values of its own."""
    stats = ", ".join(
        f"{spec.stats[key].name} {value}" for key, value in base.stats.items() if key in spec.stats
    )
    attacks = "".join(
        f"; {label} {dice}{_flag_text(base.attack_flags.get(label, ()))}"
        for label, dice in base.attacks.items()
    )
    return (
        f"{base.name} has a published statblock ({stats}{attacks}). "
        "Leave out the values you would give to use it, or set force to create a custom "
        "creature with this name."
    )


def _with_statblock(event: NpcAdded, base: MonsterTemplate) -> NpcAdded:
    """The NPC event with a statblock's armor, attacks, creature, large-group flag, and rules.
    Its stats are set by the caller, before the event is made, so they are not rolled."""
    return replace(
        event,
        armor=base.armor,
        attacks=dict(base.attacks),
        attack_flags={name: list(flags) for name, flags in base.attack_flags.items()},
        creature=base.name,
        large_group=base.large_group,
        rules=list(dict.fromkeys([*event.rules, *base.rules])),
    )


def _with_hp_tier(
    character: Mapping[str, Any] | None,
    hp_tier: str,
    npc_hp: Mapping[str, int],
    *,
    statblock: bool,
) -> dict[str, Any]:
    """The character with the HP of a tier. A tier is for an NPC with no statblock, and it sets
    HP outright, so it cannot be combined with HP given another way."""
    if hp_tier not in npc_hp:
        choices = ", ".join(sorted(npc_hp)) or "none"
        raise InvalidInput(f"Unknown HP tier {hp_tier!r}; choose one of: {choices}")
    if statblock:
        raise InvalidInput(
            "A creature with a statblock takes its HP from the statblock; leave out hp_tier"
        )
    stats = dict((character or {}).get("stats") or {})
    if "hp" in stats:
        raise InvalidInput("Give either hp_tier or hp, not both")
    return {**(character or {}), "stats": {**stats, "hp": npc_hp[hp_tier]}}


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
            "attack_flags": {name: list(flags) for name, flags in monster.attack_flags.items()},
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


def discover_location(
    db: Session, campaign_id: int, name: str, summary: str | None = None
) -> dict[str, Any]:
    """Discover a place. A given summary is what the player sees of it. Giving one for a place that
    is already discovered replaces its summary. A summary is never required."""
    state = load_state(db, campaign_id)
    where = _place_named(state, name)
    text = _optional_text(summary, "summary", MAX_SUMMARY)
    if not where.discovered:
        record(db, campaign_id, LocationDiscovered(name=where.name, summary=text))
        db.commit()
    elif text is not None and text != where.summary:
        record(db, campaign_id, LocationSummarized(name=where.name, summary=text))
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
    attacks: Sequence[Mapping[str, Any]] | None = None,
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
        attack_map, attack_flags = _attacks(attacks or [])
        statted = NpcStatted(
            name=npc.name,
            armor=0
            if armor is None
            else whole_number(armor, "armor", 0, armor_max or combat.UNCAPPED_ARMOR),
            attacks=attack_map,
            attack_flags=attack_flags,
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
    db: Session,
    campaign_id: int,
    name: str,
    location: str | None = None,
    *,
    travel: Travel | None = None,
    speed: str = DEFAULT_TRAVEL_MODE,
    mode: str = IN_SETTLEMENT,
    clock_rules: ClockRules | None = None,
    need: NeedRules | None = None,
    inventory: InventoryRules | None = None,
) -> dict[str, Any]:
    """Move a character to a place. With no place, an NPC goes into the wind: it is nowhere
    in particular until a later move brings it back. A player character's move in a settlement
    takes time from the travel table at the given speed, when the rule set has one. Moves in a
    dungeon, and overland moves placed after their watches, take no clock time here. The clock
    advances by whole hours; the rest of the minutes carry to the next move."""
    state = load_state(db, campaign_id)
    actor = _character_named(state, name)
    if location is None:
        if actor.kind != NPC_KIND:
            raise InvalidInput(f"{actor.name} is the player character and cannot go into the wind")
        record(db, campaign_id, ActorMoved(name=actor.name, location=None))
        db.commit()
        return {"moved": actor.name, "location": "in the wind"}
    if travel is not None and speed not in travel.modes:
        raise InvalidInput(
            f"there is no travel mode named {speed!r}; use one of {', '.join(sorted(travel.modes))}"
        )
    where = _place_named(state, location)
    minutes = None
    if travel is not None and actor.kind == PC_KIND and mode == IN_SETTLEMENT:
        minutes = _travel_minutes(state, actor, where, travel, speed)
    record(db, campaign_id, ActorMoved(name=actor.name, location=where.name))
    # Going somewhere in person means the player character knows the place in detail.
    if actor.kind == PC_KIND and not where.discovered:
        record(db, campaign_id, LocationDiscovered(name=where.name))
    # Hirelings travel with the player character who employs them.
    if actor.kind == PC_KIND and not actor.hireling:
        for hireling in state.characters.values():
            if hireling.hireling and hireling.employer == actor.name:
                record(db, campaign_id, ActorMoved(name=hireling.name, location=where.name))
    result: dict[str, Any] = {"moved": actor.name, "location": where.name}
    if minutes is not None and clock_rules is not None:
        result.update(
            _charge_travel(db, campaign_id, state, minutes, clock_rules, need, inventory, speed)
        )
    db.commit()
    return result


def _travelling_party(state: CampaignState, actor: Character) -> list[Character]:
    """The character and the hirelings who go with them, in that order."""
    if actor.hireling:
        return [actor]
    hirelings = [c for c in state.characters.values() if c.hireling and c.employer == actor.name]
    return [actor, *hirelings]


def _travel_minutes(
    state: CampaignState, actor: Character, where: Place, travel: Travel, mode: str
) -> int:
    """The whole minutes a move takes: the straight-line distance from where the actor is, at the
    mode's speed. A move with no distance to work out takes the default leg."""
    origin = state.places.get(name_key(actor.location)) if actor.location else None
    if origin is None or origin.position is None or where.position is None:
        return travel.default_leg_minutes
    distance = math.hypot(
        where.position.x - origin.position.x, where.position.y - origin.position.y
    )
    return round(distance / travel.modes[mode].km_per_hour * 60)


def _charge_travel(
    db: Session,
    campaign_id: int,
    state: CampaignState,
    minutes: int,
    clock_rules: ClockRules,
    need: NeedRules | None,
    inventory: InventoryRules | None,
    mode: str,
) -> dict[str, Any]:
    total = state.travel_minutes + minutes
    hours, carried = divmod(total, 60)
    record(db, campaign_id, TravelCharged(minutes=carried))
    result: dict[str, Any] = {"minutes": minutes, "speed": mode}
    if hours:
        start = clock_service.absolute(
            state.clock, len(clock_rules.parts), clock_rules.hours_per_part
        )
        clock = clock_service.advance_to(
            db, campaign_id, clock_rules, start + hours, need, inventory
        )
        result["clock"] = clock
    return result


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


def append_journal(db: Session, campaign_id: int, text: str) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    added = bounded_text(text, "text", MAX_TEXT).strip()
    if not added:
        raise InvalidInput("text must hold what to add to the journal")
    record(db, campaign_id, JournalAppended(text=added))
    db.commit()
    return {"added": "journal", "day": state.clock.day}


def replace_in_journal(
    db: Session, campaign_id: int, old_text: str, new_text: str
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    old = bounded_text(old_text, "old_text", MAX_TEXT)
    if not old.strip():
        raise InvalidInput("old_text must hold the passage to replace")
    new = (
        bounded_text(new_text, "new_text", MAX_TEXT).strip() if str(new_text or "").strip() else ""
    )
    count = state.journal.count(old)
    if count == 0:
        raise InvalidInput(
            "the journal has no passage matching old_text; view or search it for the exact text"
        )
    if count > 1:
        raise InvalidInput(f"old_text matches {count} places; quote more of it so it matches once")
    record(db, campaign_id, JournalReplaced(old_text=old, new_text=new))
    db.commit()
    return {"replaced": "journal", "deleted": new == ""}


def view_journal(
    state: CampaignState, start_line: int = 1, end_line: int | None = None
) -> dict[str, Any]:
    """Lines `start_line` to `end_line` of the journal, numbered from 1, at most JOURNAL_VIEW_LINES."""
    lines = _journal_lines(state)
    if end_line is not None and end_line < start_line:
        raise InvalidInput("end_line must be at least start_line")
    first = max(1, start_line)
    last = min(first if end_line is None else end_line, len(lines))
    last = min(last, first + JOURNAL_VIEW_LINES - 1)
    return {
        "total_lines": len(lines),
        "lines": [
            {"number": number, "text": lines[number - 1]} for number in range(first, last + 1)
        ],
    }


def search_journal(
    state: CampaignState, query: str, context: int = 1
) -> dict[str, list[dict[str, Any]]]:
    """The passages of the journal that best match `query`, in journal order. A passage is a
    matching line with `context` lines on either side, numbered as `from_line` to `to_line`.
    Matching is fuzzy: a query word matches a journal word that contains it or is spelled nearly
    the same."""
    words = re.findall(r"\w+", bounded_text(query, "query", MAX_TEXT).casefold())
    lines = _journal_lines(state)
    context = max(0, min(context, MAX_JOURNAL_CONTEXT))
    scored = [
        (score, index)
        for index, line in enumerate(lines)
        if (score := _fuzzy_score(words, line)) > 0
    ]
    best = sorted(scored, key=lambda pair: (-pair[0], pair[1]))[:MAX_JOURNAL_RESULTS]
    ranges: list[list[int]] = []
    for _score, index in sorted(best, key=lambda pair: pair[1]):
        low, high = max(0, index - context), min(len(lines) - 1, index + context)
        if ranges and low <= ranges[-1][1] + 1:
            ranges[-1][1] = max(ranges[-1][1], high)
        else:
            ranges.append([low, high])
    return {
        "passages": [
            {
                "from_line": low + 1,
                "to_line": high + 1,
                "text": "\n".join(lines[low : high + 1]),
            }
            for low, high in ranges
        ]
    }


def _journal_lines(state: CampaignState) -> list[str]:
    return state.journal.split("\n") if state.journal else []


def _fuzzy_score(words: Sequence[str], paragraph: str) -> int:
    """How many of the query words the paragraph matches."""
    found = re.findall(r"\w+", paragraph.casefold())
    return sum(
        1
        for word in words
        if any(word in candidate for candidate in found)
        or get_close_matches(word, found, n=1, cutoff=FUZZY_CUTOFF)
    )


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
        attacks = ", ".join(
            f"{name} {formula}{_flag_text(character.attack_flags.get(name, ()))}"
            for name, formula in character.attacks.items()
        )
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


def speaker_at_player(state: CampaignState, name: str) -> bool:
    """Whether the named NPC or hireling is at the player character's location."""
    npc = _character_named(state, name)
    pc = state.pc()
    return pc is not None and npc.location == pc.location


def speaker_profile(state: CampaignState, name: str, spec: CharacterSpec) -> str:
    """The NPC details a speech step needs."""
    npc = _character_named(state, name)
    if npc.kind != NPC_KIND and not npc.hireling:
        raise InvalidInput(f"{npc.name} is the player character and does not need speaking for")
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


def _map_center(state: CampaignState) -> tuple[float, float] | None:
    """The position the map centers on: the player character's place, or the nearest place above it
    that has a position. None when there is no player character or no placed place above it."""
    pc = state.pc()
    name = pc.location if pc is not None else None
    seen: set[str] = set()
    while name is not None and name_key(name) not in seen:
        seen.add(name_key(name))
        place = state.named(name)
        if not isinstance(place, Place):
            return None
        if place.position is not None:
            return place.position.x, place.position.y
        name = place.parent
    return None


def _km_text(km: float) -> str:
    return f"{abs(km):.1f}".rstrip("0").rstrip(".")


def _offset_text(dx: float, dy: float) -> str:
    east = "east" if dx >= 0 else "west"
    north = "north" if dy >= 0 else "south"
    return f"{_km_text(dx)} km {east}, {_km_text(dy)} km {north}"


def time_view(state: CampaignState, clock: ClockRules | None) -> TimeView | None:
    """The clock's day and part. The last part of the day is night, as the wilderness rules count it."""
    if clock is None or not clock.parts:
        return None
    index = state.clock.part
    return TimeView(
        day=state.clock.day,
        part=clock.parts[index],
        night=index == len(clock.parts) - 1,
    )


def map_view(state: CampaignState, terrain: Mapping[str, WorldTerrain] | None = None) -> MapView:
    """The player's map. Only discovered places appear on it. Every region is drawn, but only known
    regions are named, and only they set the map's reach. Each marker is placed on a square centered
    on the player character's place, sized to fit the farthest discovered place or known region
    corner plus a margin. `terrain` gives the colour and texture of each region's kind."""
    places = sorted(
        (place for place in state.places.values() if place.discovered),
        key=lambda place: name_key(place.name),
    )
    center = _map_center(state)
    cx, cy = center if center is not None else (0.0, 0.0)
    placed = [(place, place.position) for place in places if place.position is not None]
    known = [region for region in state.regions.values() if _region_known(state, region)]
    corners = [point for region in known for point in region.points]
    reach = max(
        [MIN_MAP_RADIUS_KM]
        + [max(abs(pos.x - cx), abs(pos.y - cy)) for _, pos in placed]
        + [max(abs(x - cx), abs(y - cy)) for x, y in corners]
    )
    radius = reach * MAP_MARGIN
    pc = state.pc()
    here = pc.location if pc is not None else None
    labels = token_labels(place.name for place in places)
    markers = [
        MapMarker(
            name=place.name,
            label=labels[place.name],
            left=round(50 + (pos.x - cx) / radius * 50, 2),
            top=round(50 - (pos.y - cy) / radius * 50, 2),
            here=place.name == here,
            icon=place.icon,
        )
        for place, pos in placed
    ]
    where: dict[str, str] = {}
    for place in places:
        if place.parent:
            where[place.name] = f"Inside {place.parent}"
        elif place.name == here:
            where[place.name] = "You are here"
        elif place.position is not None and center is not None:
            where[place.name] = _offset_text(place.position.x - cx, place.position.y - cy)
    origin_left = round(50 - cx / radius * 50, 2)
    origin_top = round(50 + cy / radius * 50, 2)
    on_board = 0 <= origin_left <= 100 and 0 <= origin_top <= 100
    kinds = terrain or {}
    regions = [
        _map_region(region, kinds, cx, cy, radius, _region_known(state, region))
        for region in sorted(state.regions.values(), key=lambda region: name_key(region.name))
    ]
    patterns = [
        MapPattern(id=_pattern_id(kind.id), shape=kind.pattern, ink=kind.ink)
        for kind in kinds.values()
        if kind.pattern is not None and kind.ink is not None
    ]
    return MapView(
        radius_km=round(radius, 2),
        markers=markers,
        places=places,
        where=where,
        origin=(origin_left, origin_top) if on_board else None,
        regions=regions,
        patterns=patterns,
    )


def _region_known(state: CampaignState, region: Region) -> bool:
    """A region is known once it is discovered, or once a discovered place with a map point lies
    inside it."""
    if region.discovered:
        return True
    return any(
        place.discovered
        and place.position is not None
        and contains((place.position.x, place.position.y), region.points)
        for place in state.places.values()
    )


def _pattern_id(kind: str) -> str:
    return f"terrain-{kind}"


def _map_region(
    region: Region,
    kinds: Mapping[str, WorldTerrain],
    cx: float,
    cy: float,
    radius: float,
    known: bool,
) -> MapRegion:
    kind = kinds.get(region.terrain)
    color = kind.color if kind is not None else "#888888"
    name = text = None
    if known:
        name = region.name
        text = region.name if kind is None else f"{region.name} ({kind.name}). {kind.rule}"
    pattern = _pattern_id(kind.id) if kind is not None and kind.pattern is not None else None
    points = tuple(
        (round(50 + (x - cx) / radius * 50, 2), round(50 - (y - cy) / radius * 50, 2))
        for x, y in region.points
    )
    return MapRegion(name=name, text=text, color=color, points=points, pattern=pattern)


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
                    damage=item.damage,
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
        journal=state.journal,
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
    terrain: Mapping[str, EncounterTerrain] | None = None,
    encounter_objects: Mapping[str, EncounterObject] | None = None,
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
    if pc.pronouns:
        lines.append(f"Pronouns: {pc.name}, {pc.pronouns}.")
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
        for npc in _npcs_at(state, pc.location):
            if npc.pronouns:
                lines.append(f"Pronouns: {npc.name}, {npc.pronouns}.")
        things = [o.contents for o in visible_objects(state, pc.location)]
        if things:
            lines.append(f"Objects here: {'; '.join(things)}.")
        here = _place_named(state, pc.location)
        if here.parent is not None:
            lines.append(f"{here.name} is inside {here.parent}.")
        inside = [_place_named(state, name) for name in _children_of(state, here.name)]
        if inside:
            rooms = ", ".join(
                f"{room.name} ({'discovered' if room.discovered else 'not yet discovered'})"
                for room in inside
            )
            lines.append(f"Places inside {here.name}: {rooms}.")
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
        for h in hirelings:
            if h.pronouns:
                lines.append(f"Pronouns: {h.name}, {h.pronouns}.")
    wind = _npcs_in_the_wind(state)
    if wind:
        lines.append(f"NPCs in the wind, at no location: {', '.join(wind)}.")
    lines.extend(_status_lines(pc, conditions, inventory))
    lines.extend(_encounter_lines(state, grid, combat, terrain, encounter_objects))
    return "\n".join(lines)


def _encounter_lines(
    state: CampaignState,
    grid: EncounterGrid | None,
    combat: CombatRules | None,
    terrain: Mapping[str, EncounterTerrain] | None = None,
    encounter_objects: Mapping[str, EncounterObject] | None = None,
) -> list[str]:
    view = encounters.encounter_view(state, grid, combat, terrain, encounter_objects)
    if view is None:
        return []
    lines = [
        f"Encounter in progress on the map {view['map']} at {view['location']}, round {view['round']}.",
        f"Map: {view['columns']} by {view['rows']} cells, columns and rows counted from 0 at the top left. {view['description']}",
    ]
    if view["background"] is not None:
        lines.append(_terrain_sentence("Background", view["background"]))
    if view["marked_cells"]:
        marked = [_marked_cell_text(cell) for cell in view["marked_cells"]]
        lines.append("Marked cells: " + "; ".join(marked) + ".")
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


def _terrain_sentence(label: str, terrain: dict[str, Any]) -> str:
    difficult = (
        " It is difficult: each cell entered costs two moves." if terrain["difficult"] else ""
    )
    return f"{label} terrain: {terrain['name']}.{difficult}"


def _marked_cell_text(cell: dict[str, Any]) -> str:
    parts = []
    if cell["terrain"]:
        parts.append(f"{cell['terrain']}{' (difficult)' if cell['difficult'] else ''}")
    if cell["object"]:
        parts.append(f"{cell['object']}{' (blocks)' if cell['blocks'] else ''}")
    return f"column {cell['column']}, row {cell['row']}: {', '.join(parts)}"


def _location_entry(place: Place) -> dict[str, Any]:
    position = None
    if place.position is not None:
        position = {"x": place.position.x, "y": place.position.y}
    return {
        "name": place.name,
        "description": place.description,
        "summary": place.summary,
        "discovered": place.discovered,
        "position_km": position,
        "parent": place.parent,
    }


def _npcs_at(state: CampaignState, location: str) -> list[Character]:
    return [c for c in state.characters.values() if c.kind == NPC_KIND and c.location == location]


def present_rules_text(state: CampaignState, rules: Mapping[str, str]) -> str:
    """The text of the rules attached to what is present: the place the player character is in,
    the NPCs there, defeated or not, and the items the player character carries. Empty when
    nothing is attached."""
    pc = state.pc()
    if pc is None:
        return ""
    sources: dict[str, list[str]] = {}
    if pc.location is not None:
        place = state.places.get(name_key(pc.location))
        if place is not None:
            for rule_id in place.rules:
                sources.setdefault(rule_id, []).append(place.name)
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
        [
            "These rules belong to the place, NPCs, and items present now. Follow them as written.",
            *lines,
        ]
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
        "pronouns": npc.pronouns,
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
    pronouns: str | None = None,
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
        pronouns=pronouns,
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


def _ensure_no_same_place(state: CampaignState, name: str) -> None:
    """A new place may not repeat an existing one under a different article, such as "The Farming
    Path" for "Farming Path"; discover_location reveals the existing place instead."""
    bare = _without_article(name)
    for place in state.places.values():
        if _without_article(place.name) == bare:
            raise InvalidInput(
                f"there is already a location called {place.name!r}; "
                "use discover_location to reveal it"
            )


def _without_article(name: str) -> str:
    key = name_key(name)
    return key.removeprefix("the ")


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
MAX_JOURNAL_RESULTS = 5
MAX_JOURNAL_CONTEXT = 3
JOURNAL_VIEW_LINES = 50
FUZZY_CUTOFF = 0.8


def _words(query: str) -> list[str]:
    return bounded_text(query, "query", MAX_TEXT).casefold().split()


def _has_words(words: Sequence[str], *parts: str) -> bool:
    haystack = " ".join(parts).casefold()
    return all(word in haystack for word in words)


def _optional_text(value: object, label: str, limit: int = MAX_NAME) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return bounded_text(value, label, limit)


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
