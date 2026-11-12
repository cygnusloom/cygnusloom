"""World templates and rule sets, loaded once from the bundled data directory."""

from __future__ import annotations

import copy
import importlib
import inspect
import json
import logging
import math
import re
import string
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from itertools import pairwise
from pathlib import Path
from types import ModuleType
from typing import Any

from cygnusloom.content.character import (
    DEFAULT_FEATURE_TYPES,
    CampaignSpec,
    CharacterSpec,
    CounterRef,
    Feature,
    FeatureTypeRules,
    TableOption,
    apply_stat_presets,
    build_campaign,
    build_character,
    character_schema,
    load_feature,
    load_starting_silver,
    load_starting_years,
    load_training_features,
    rule_ids,
)
from cygnusloom.content.definitions import (
    ContentError,
    Definition,
    read_definitions,
    required_text,
)
from cygnusloom.content.icons import (
    FALLBACK_ITEM,
    FALLBACK_MONSTER,
    FALLBACK_PLACE,
    FALLBACK_WEAPON,
    check_icon,
    resolve_icon,
)
from cygnusloom.rules.dice import FORMULA, PLUS_MODES, is_formula
from cygnusloom.services.attacks import ATTACK_FLAGS
from cygnusloom.services.geometry import overlaps, polygon_problem

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
# A checklist item id is the key the GM's finish_state_step call takes a note for, so it must be
# a safe tool argument name.
CHECKLIST_KEY = re.compile(r"[a-z][a-z0-9_-]*\Z")
# What a tool with no runnable handler is told to add. Its id is not a WorldTools method and it
# names no Python function.
TOOL_HANDLER_HINT = "give 'world_method', or 'python_module' with 'function'"
# A dice table row's roll: one number, or an inclusive range such as "3-5".
ROLL_RANGE = re.compile(r"([0-9]+)(?:-([0-9]+))?")
PROMPT_ARGUMENT = "prompt"
# A tool flagged `character_options` gets this argument, built from the rule set's character spec.
CHARACTER_ARGUMENT = "character"
# A tool flagged `npc_hp_tier` gets this optional argument, whose values are the rule set's HP tiers.
NPC_HP_TIER_ARGUMENT = "hp_tier"
# A tool flagged `travel` gets this argument, with the rule set's travel modes as its choices.
TRAVEL_ARGUMENT = "travel"
# A tool property may say `"enum_from"` to take its choices from the rule set's own content: its
# moves, stats, item sizes, or rest lengths. The loader fills in the enum, so a tool never lists
# what the rule set defines. A source with no entries leaves the enum out, which lets core author
# a universal tool whose choices come from each inheriting rule set.
ENUM_FROM_KEY = "enum_from"
# The engine passes its ToolContext to a function that declares this parameter. The model
# never sees it, so it must not appear in a tool's schema.
CONTEXT_ARGUMENT = "ctx"
# An unbound WorldTools method's signature starts with this. The engine calls it on a built
# world, so the parameter is not part of a tool's schema.
SELF_ARGUMENT = "self"
logger = logging.getLogger(__name__)

CORE_RULE_SET = "core"
# Every rule set must define these tables. NPCs get a random entry from each one by default.
REQUIRED_TABLES = ("personality", "accent", "quirk")
# The genres a dice or creative table can be tagged with. A rule set offers the tables its own
# `table_genres` match, and a campaign may narrow the choice further. A table tagged `generic`,
# or with no genres at all, fits every setting. Extend this list and an existing table tag gains
# a new reader without any code change.
TABLE_GENRES = (
    "generic",
    "fantasy",
    "magicschool",
    "science-fiction",
    "horror",
    "modern",
    "historical",
)
GENERIC_GENRE = "generic"


def check_keys(data: Mapping[str, Any], allowed: Iterable[str], label: str) -> None:
    """Reject any key a definition loader does not read.

    `allowed` is every key the loader reads, including `type` and `id`. A misspelled or obsolete
    key fails at load time instead of being ignored. Deliberately open maps, such as a combat's
    `defeat` labels or the rule set's `npc_hp` tiers, are validated by the loader that reads them.
    """
    unknown = sorted(set(data) - set(allowed), key=str)
    if unknown:
        names = ", ".join(repr(key) for key in unknown)
        raise ContentError(f"{label}: unknown key(s) {names}")


def check_object_keys(data: object, allowed: Iterable[str], label: str) -> Mapping[str, Any]:
    """Check that a nested object is a mapping and carries only keys its loader reads."""
    if not isinstance(data, Mapping):
        raise ContentError(f"{label} must be an object")
    check_keys(data, allowed, label)
    return data


def _icon_entries(
    definitions: Iterable[Definition],
    kind: str,
    template: str,
) -> dict[str, tuple[str | None, str | None]]:
    """The raw (icon, looks_like) a set of definitions names, by id, for `looks_like` chains.
    Every field is checked to be text here, before a chain follows it."""
    entries: dict[str, tuple[str | None, str | None]] = {}
    for definition in definitions:
        if definition.type != kind:
            continue
        label = f"{template}: {kind.replace('_', ' ')} {definition.id}"
        icon = _optional_text(definition.data, "icon", label)
        looks_like = _optional_text(definition.data, "looks_like", label)
        entries[definition.id] = (icon, looks_like)
    return entries


def _icon_fields(
    data: Mapping[str, Any],
    entries: Mapping[str, tuple[str | None, str | None]],
    fallback: str | None,
    label: str,
) -> tuple[str | None, str | None]:
    """The resolved icon and the raw `looks_like` a definition carries."""
    icon = _optional_text(data, "icon", label)
    looks_like = _optional_text(data, "looks_like", label)
    resolved = resolve_icon(
        icon,
        looks_like,
        label=label,
        entries=entries,
        fallback=fallback,
    )
    return resolved, looks_like


def read_table_genres(value: Any, label: str) -> tuple[str, ...]:
    """The genre tags in a table's `genres`, checked against TABLE_GENRES. An absent list means
    the table fits every setting. Repeats are refused, so a tag means what it says once."""
    if value is None:
        return ()
    entries = _text_list(value, label)
    unknown = [entry for entry in entries if entry not in TABLE_GENRES]
    if unknown:
        names = ", ".join(repr(entry) for entry in unknown)
        raise ContentError(
            f"{label}: unknown genre(s) {names}; choose from {', '.join(TABLE_GENRES)}"
        )
    if len(set(entries)) != len(entries):
        raise ContentError(f"{label}: a genre may be named only once")
    return tuple(entries)


def _definition_genres(definition: Definition, template: str) -> tuple[str, ...]:
    """A table definition's genre tags, before the table itself is built."""
    if "genres" not in definition.data:
        return ()
    label = f"{template}: {definition.type.replace('_', ' ')} {definition.id}: 'genres'"
    return read_table_genres(definition.data["genres"], label)


def table_genre_offered(tags: Sequence[str], genres: Sequence[str]) -> bool:
    """Whether a genre selection offers a table. A table with no tags, or one tagged `generic`,
    belongs to every setting; otherwise at least one of its tags must be selected."""
    if not tags or GENERIC_GENRE in tags:
        return True
    return bool(set(tags) & set(genres))


def tables_for_genres(
    tables: Mapping[str, DiceTable], genres: Sequence[str]
) -> dict[str, DiceTable]:
    """The tables a genre selection offers, keeping the mapping's order."""
    return {
        table_id: table
        for table_id, table in tables.items()
        if table_genre_offered(table.genres, genres)
    }


def effective_table_genres(stored: str, rule_set: RuleSet) -> tuple[str, ...]:
    """The genres a campaign's tables are filtered by. A campaign's JSON list wins when it set
    one; a blank, unreadable, or now-unknown list falls back to the rule set's default, so a
    stale choice never leaves the GM without the tables the rule set means to offer."""
    text = (stored or "").strip()
    if not text:
        return rule_set.table_genres
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return rule_set.table_genres
    if not isinstance(value, list):
        return rule_set.table_genres
    chosen = tuple(
        dict.fromkeys(entry for entry in value if isinstance(entry, str) and entry in TABLE_GENRES)
    )
    return chosen or rule_set.table_genres


@dataclass(frozen=True)
class LocationTemplate:
    id: str
    name: str
    description: str
    # Known to everyone in the world from the start, so the player's map shows it.
    discovered: bool = False
    x: float | None = None
    y: float | None = None
    # The id of the place this one is inside, such as a building in a village. A place inside
    # another has no map position of its own.
    parent: str | None = None
    # An icon id from the icon registry, shown on the map. Places without one get a default.
    icon: str | None = None
    # What the player sees of the place, written without secrets. None when it is not authored.
    summary: str | None = None
    # The ids of rules attached to this place. Their text reaches the GM while the player is here.
    rules: tuple[str, ...] = ()
    # Authored overland routes from this place, by destination location id. Each route's path,
    # distance, terrain, and vast are checked against the rule set's wilderness travel.
    routes: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    # The raw `looks_like` this place borrows, kept for authors. The icon is resolved at load.
    looks_like: str | None = None
    # The id of the encounter template that is this place's authored local map, or None. It is
    # checked against the rule set's templates when the world's content is built.
    map: str | None = None


@dataclass(frozen=True)
class NpcTemplate:
    """An NPC from the world definition. Traits left as None are generated when the campaign starts."""

    id: str
    name: str
    location: str
    description: str
    personality: str | None
    accent: str | None
    quirks: tuple[str, ...] | None
    # The id of the monster that is this NPC's statblock: one in the world or the rule set. None
    # when the NPC has no statblock. An NPC with its own statblock is a world monster of the same id.
    monster: str | None = None
    # A story-only NPC has no stats: it cannot fight, make a save, or be an attack target until
    # the GM gives it stats with update_npc.
    story_only: bool = False
    # The ids of rules attached to this NPC. Their text reaches the GM while the NPC is here.
    rules: tuple[str, ...] = ()
    # The rule set's NPC HP tier, such as "average", for an NPC with no statblock. None uses the
    # rule set's ordinary character HP roll.
    hp_tier: str | None = None


@dataclass(frozen=True)
class WorldStart:
    """Where a new campaign's player character begins, and the hooks that bring them there. The
    season is optional; None means the rule set's starting season."""

    location: str
    hooks: str
    season: str | None = None


@dataclass(frozen=True)
class WorldTemplate:
    id: str
    name: str
    description: str
    locations: tuple[LocationTemplate, ...]
    npcs: tuple[NpcTemplate, ...]
    # The rule set the world is built for, or None when it may be played with any.
    rule_set: str | None = None
    # A short line shown when choosing the world on the create campaign page.
    blurb: str | None = None
    # True when the world is imported from a third-party source; the UI links to the credits.
    third_party: bool = False
    # The world's monsters, tables, features, and rules, as definitions. They are checked against
    # each rule set in WorldContent, since a monster's stats depend on the rule set.
    definitions: tuple[Definition, ...] = ()
    start: WorldStart | None = None
    # Areas of ground on the world map. They do not overlap, and are checked against the rule set's
    # world terrain in WorldContent.
    regions: tuple[RegionTemplate, ...] = ()

    @property
    def has_setting(self) -> bool:
        """Whether the world carries authored setting material. A world with no places, people,
        regions, or definitions has no setting, so the GM invents one."""
        return bool(self.locations or self.npcs or self.regions or self.definitions)


@dataclass(frozen=True)
class WorldContent:
    """A world's own monsters, tables, features, and rules, checked against one rule set."""

    monsters: Mapping[str, MonsterTemplate]
    tables: Mapping[str, tuple[TableOption, ...]]
    features: Mapping[str, Feature]
    rules: Mapping[str, str]
    # Rules no monster, NPC, location, or feature attaches. They are not in any owner's text.
    unattached_rules: Mapping[str, str] = field(default_factory=dict)
    # Dice tables the world defines itself, plus each world table as a dice table.
    dice_tables: Mapping[str, DiceTable] = field(default_factory=dict)


@dataclass(frozen=True)
class PlayerInput:
    """The player supplies a value for a tool before it runs.

    The player rolls `dice` (a fixed formula such as "1d20") or, when `dice_param` is set, the
    formula the GM gave in that string argument, such as "1d8+1d8". The player may also type
    free-form text instead. A roll's total is passed to the function as keyword argument `param`.
    `details`, if set, is a template filled from the arguments and shown with the prompt, such as
    the modifiers. When `modifier_param` is set, the string argument it names is an attack modifier
    (such as "impaired"); the dice then come from that modifier, not from `dice_param`."""

    param: str | None = None
    dice: str | None = None
    dice_param: str | None = None
    # A string argument naming a move or check. The engine knows that move's or check's dice and
    # asks the player to roll them. The total reaches the function as `param`.
    move_param: str | None = None
    check_param: str | None = None
    # A string argument naming the weapon. The engine knows that weapon's dice and asks the player
    # to roll them. With `weapon_roll_param`, that total is passed to the function.
    weapon_param: str | None = None
    # The function parameter that receives the player's weapon roll. None when the roll is only
    # for the table and the engine rolls damage itself.
    weapon_roll_param: str | None = None
    details: str | None = None
    modifier_param: str | None = None


@dataclass(frozen=True)
class ToolCall:
    id: str
    # The name the provider sees. It defaults to the id.
    name: str
    description: str
    # The JSON schema for the tool's arguments. A tool with no arguments may omit it, which
    # loads as an empty object schema.
    parameters: Mapping[str, Any]
    # The rule set module's function, when the tool names one. The engine passes the arguments
    # straight from the schema. Exactly one of `function` and `world_method` is set.
    function: Callable[..., Any] | None = None
    # The WorldTools method the engine calls, when the tool binds an engine operation instead of
    # naming a rule set function. It defaults to the tool id, so the common case names no method
    # at all. The loader resolves it and checks it against the schema.
    world_method: str | None = None
    # Arguments the tool supplies to its method itself, so the GM never gives them. A value that
    # is an object builds one argument from the schema arguments it names, such as a statblock's
    # stats from flat numbers.
    bind: Mapping[str, Any] = field(default_factory=dict)
    # Schema arguments passed to the method under a different name, mapping argument to parameter.
    rename: Mapping[str, str] = field(default_factory=dict)
    player_input: PlayerInput | None = None
    # A str.format template shown to the player after the tool runs. Fields come from the
    # arguments and the result.
    activity: str | None = None
    # True when the function declares CONTEXT_ARGUMENT and the engine must pass it.
    takes_context: bool = False
    # True for tools that only read the world. The GM lists them with list_lookups.
    lookup: bool = False
    # True for tools only the journal step offers. Every other step leaves them out.
    journal_step: bool = False
    # True for tools the turn review step adds to the usual tools. The state step leaves them
    # out.
    review_step: bool = False
    # Capability paths that must be truthy for the tool to be offered, such as "wilderness".
    requires: tuple[str, ...] = ()
    # Capability paths and the values that enable the tool, such as {"social": ("factions",)}.
    enabled_by: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class TravelMode:
    name: str
    km_per_hour: float
    note: str


@dataclass(frozen=True)
class Travel:
    """How fast a party moves by each travel mode, and what a leg costs when its distance is
    unknown."""

    modes: Mapping[str, TravelMode]
    default_leg_minutes: int
    default_leg_note: str

    @property
    def default_mode(self) -> str:
        """The mode a move uses when none is named: the first one the rule set lists."""
        return next(iter(self.modes))


JOURNEY_MODELS = ("terrain_days", "watches")
JOURNEY_EVENT_ON = ("day", "watch")
JOURNEY_EVENT_KEYS = frozenset({"on", "when", "table", "reason"})
JOURNEY_WHEN_KEYS = frozenset({"terrain", "weather", "road", "path", "chance"})
# The highest 1d6 roll that still fires a journey event configured with a chance.
MAX_JOURNEY_CHANCE = 6


@dataclass(frozen=True)
class JourneyEvent:
    """A dice-table event rolled during a journey when its conditions match. `on` is "day"
    for a terrain-day journey or "watch" for a watch-by-watch one. The `when` filters narrow
    the terrain, weather, road, or path, and `chance` is the highest 1d6 roll that still
    fires the event."""

    on: str
    table: str
    reason: str = ""
    terrain: tuple[str, ...] = ()
    weather: tuple[str, ...] = ()
    road: tuple[bool, ...] = ()
    path: tuple[str, ...] = ()
    chance: int | None = None


@dataclass(frozen=True)
class JourneyRules:
    """An overland journey measured in whole travel days or in watches. WWN's terrain-day
    model: a day is ten hours of marching, terrain and weather scale the pace, and the return
    value lists supplies and foraging. Cairn's watch-by-watch journey keeps its procedure in
    Wilderness. `events` are dice-table events rolled as the journey passes."""

    model: str = "terrain_days"
    day_hours: int = 10
    terrain_speeds: Mapping[str, float] = field(default_factory=dict)
    road_multiplier: float = 2.0
    weather_factors: Mapping[str, float] = field(default_factory=dict)
    forage_difficulty: Mapping[str, int] = field(default_factory=dict)
    food_per_traveler_day: int = 1
    water_per_traveler_day: int = 1
    firewood_per_day: int = 4
    max_days: int = 30
    max_travelers: int = 20
    # Dice-table events rolled once per travel day or watch, in the order they are listed.
    events: tuple[JourneyEvent, ...] = ()


@dataclass(frozen=True)
class DieGrowth:
    """A die whose sides grow with the party: sides = min(max, base + per_member * party size)."""

    base: int
    per_member: int
    max: int


@dataclass(frozen=True)
class WildernessAlternative:
    """An item that can stand in for a consume's item. `requires`, when set, is an item some
    party member must hold for this alternative to be used."""

    item: str
    requires: str | None = None


@dataclass(frozen=True)
class WildernessEffect:
    """One declarative step the engine applies at a lifecycle point. `do` picks the kind."""

    do: str
    item: str | None = None
    per: str | None = None  # "member" or "party", for consume only
    # A consume whose use the rule set tracks, such as a meal. Its hook may record the use.
    track: bool = False
    count: DieGrowth | None = None  # for grant only
    times: int = 1  # for roll_events only
    # Items tried in order when a party consume has no `item`, such as a Lantern's oil.
    alternatives: tuple[WildernessAlternative, ...] = ()
    # The status the party is in when a consume finds nothing to spend, such as "panicking".
    on_shortage: str | None = None
    # The basic supplies a buy effect lets a resupply purchase, such as Rations.
    items: tuple[str, ...] = ()


@dataclass(frozen=True)
class WildernessHooks:
    """The steps run at each lifecycle point: after a Wilderness Action resolves, and on a night watch."""

    resolve: tuple[WildernessEffect, ...] = ()
    night: tuple[WildernessEffect, ...] = ()


WILDERNESS_DO = ("consume", "grant", "clear", "roll_events", "buy")
WILDERNESS_PER = ("member", "party")
# The keys each kind of effect takes, besides `do`.
WILDERNESS_EFFECT_KEYS = {
    "consume": ("item", "per", "track", "alternatives", "on_shortage"),
    "grant": ("item", "count"),
    "clear": ("item",),
    "roll_events": ("times",),
    "buy": ("items",),
}
WILDERNESS_ALTERNATIVE_KEYS = frozenset({"item", "requires"})
ROUTE_KEYS = frozenset({"path", "distance", "terrain", "vast"})
# A world-level route connects two locations. Besides the route values it names both endpoints and
# whether the route reaches the destination and back.
WORLD_ROUTE_KEYS = frozenset({"type", "id", "from", "to", "one_way", *ROUTE_KEYS})
# The most sides a grant's die may have.
MAX_GRANT_SIDES = 12


@dataclass(frozen=True)
class WildernessAction:
    """A Wilderness Action a party member can choose once per watch, such as Travel or Make Camp."""

    id: str
    name: str
    text: str
    hooks: WildernessHooks = WildernessHooks()


@dataclass(frozen=True)
class Wilderness:
    """The wilderness procedure: seasons and weather, the Wilderness Events table, the lost odds
    by path, and the actions. Dice tables it names are in the rule set's dice tables."""

    seasons: tuple[str, ...]
    start_season: str
    season_days: int
    # A run of this many weather results in a row, of catastrophe_weather, becomes catastrophe_result.
    catastrophe_streak: int
    catastrophe_weather: str
    catastrophe_result: str
    # Path id to the highest lost_die roll that means the party is lost. Zero means never lost.
    lost_on: Mapping[str, int]
    actions: Mapping[str, WildernessAction]
    events_table: str
    # Season to the dice table that rolls its weather.
    weather_tables: Mapping[str, str]
    # Run for every Wilderness Action, before the action's own hooks.
    hooks: WildernessHooks
    travel: WildernessTravel | None = None
    # The dice rolled to see whether the party is lost, such as "1d6".
    lost_die: str = "1d6"


@dataclass(frozen=True)
class WeatherPenalty:
    """What a weather result does to overland travel."""

    # True: the GM picks one of fatigue or watches. False: both apply.
    choice: bool = False
    fatigue: int = 0
    watches: int = 0
    terrain_steps: int = 0
    # False refuses overland travel.
    travel: bool = True


@dataclass(frozen=True)
class WildernessTravel:
    """Overland travel: the cost of each path, distance, and terrain, and how weather changes them."""

    path: Mapping[str, int]
    distance: Mapping[str, int]
    # Watch penalty per terrain level, in difficulty order.
    terrain: Mapping[str, int]
    vast_max: int
    held_back_terrain_steps: int
    weather: Mapping[str, WeatherPenalty]


@dataclass(frozen=True)
class EncounterGrid:
    """The scale of an encounter map: how many feet a cell is, and how far a participant may move
    in one turn."""

    cell_feet: int
    move_feet: int

    @property
    def move_cells(self) -> int:
        return self.move_feet // self.cell_feet


@dataclass(frozen=True)
class EncounterTerrain:
    """A kind of ground on an encounter map. Each cell entered costs `move_cost` movement."""

    id: str
    name: str
    color: str
    # The rule effect of this ground, as the rule set words it. Shown when the ground is hovered.
    rule: str
    # Movement spent to enter one cell of this ground. 1 is normal ground.
    move_cost: int = 1
    # A texture from TERRAIN_PATTERNS drawn over the colour, in the ink colour.
    pattern: str | None = None
    ink: str | None = None
    # A free-text grouping for map catalogs, such as stone, wood, or hazard.
    category: str = ""

    @property
    def difficult(self) -> bool:
        """True when this ground costs more than normal movement to enter."""
        return self.move_cost > 1


# The textures a world terrain kind may be drawn with. The map engine draws each one.
# Local maps draw the same ids as CSS patterns instead of SVG.
TERRAIN_PATTERNS = (
    "waves",
    "trees",
    "ridges",
    "reeds",
    "tufts",
    "dunes",
    "crags",
    "ice",
    "flames",
    "canopy",
    "moor",
    "reef",
    "bricks",
    "tiles",
    "wood",
    "carpet",
    "snow",
    "lava",
    "hexes",
    "runes",
)

# The looks a local map may choose. The CSS colours each palette, and each ambient adds an
# overlay, so a map's mood is data rather than a rule-set special case.
MAP_PALETTES = (
    "parchment",
    "stone",
    "forest",
    "sea",
    "ember",
    "frost",
    "shadow",
    "royal",
)
MAP_AMBIENTS = (
    "day",
    "dusk",
    "night",
    "torchlit",
    "foggy",
    "underwater",
    "magical",
)


@dataclass(frozen=True)
class WorldTerrain:
    """A kind of ground on the world map. A region of this ground is drawn in its colour."""

    id: str
    name: str
    color: str
    # The rule effect of this ground, as the rule set words it. Shown when the ground is hovered.
    rule: str
    # An optional texture from TERRAIN_PATTERNS, drawn over the colour in the ink colour.
    pattern: str | None = None
    ink: str | None = None


@dataclass(frozen=True)
class RegionTemplate:
    """An area of one kind of ground on the world map. Its points are outline corners in
    kilometers from the map's center, in order around the edge."""

    id: str
    name: str
    terrain: str
    points: tuple[tuple[float, float], ...]
    # Optional looks and a display label. None follows the terrain kind.
    color: str | None = None
    pattern: str | None = None
    ink: str | None = None
    label: str | None = None


@dataclass(frozen=True)
class EncounterObject:
    """Something standing on an encounter map. A participant cannot enter a blocking cell.

    A solid object is drawn as a block of its colour rather than its icon, so large
    features such as walls do not clash with the smaller objects placed among them.
    """

    id: str
    name: str
    icon: str
    color: str
    blocks: bool = False
    solid: bool = False
    # A short player-facing description, such as what the object is for or how it looks.
    description: str = ""
    # A free-text grouping for map catalogs, such as furniture, nature, or hazard.
    category: str = ""


@dataclass(frozen=True)
class EncounterCell:
    """One position on an encounter template. Terrain and object are ids, or "" for none."""

    column: int
    row: int
    terrain: str = ""
    object: str = ""


@dataclass(frozen=True)
class EncounterTemplate:
    """A ready-made encounter map. Every cell not in `cells` has the background terrain."""

    id: str
    name: str
    columns: int
    rows: int
    background: str
    cells: tuple[EncounterCell, ...]
    # The template's suggested look, copied by create_map unless the caller chooses one.
    palette: str = ""
    ambient: str = ""
    background_color: str = ""
    grid_color: str = ""
    # A short player-facing description of the layout.
    description: str = ""


@dataclass(frozen=True)
class DiceRow:
    """The rolls from `low` to `high`, inclusive, give `result`."""

    low: int
    high: int
    result: str
    text: str = ""


@dataclass(frozen=True)
class DiceTable:
    """A table rolled with dice, such as 2d6 for a reaction. Its rows cover every roll the dice
    can show, from the fewest to the most."""

    id: str
    name: str
    dice: str
    rows: tuple[DiceRow, ...]
    creative: bool = False
    # The kinds of setting the table fits. Empty means every setting, as does `generic`.
    genres: tuple[str, ...] = ()

    def row_for(self, roll: int) -> DiceRow:
        return next(row for row in self.rows if row.low <= roll <= row.high)


@dataclass(frozen=True)
class MonsterAbility:
    """A monster's special ability as data. `kind` says what it is, and the optional fields
    say when it triggers, what save it forces, what condition or damage it causes."""

    kind: str
    name: str
    description: str
    trigger: str = ""
    save: str = ""
    condition: str = ""
    damage: str = ""


MONSTER_ABILITY_KINDS = ("save_or_condition", "action", "immunity", "passive")


@dataclass(frozen=True)
class MonsterTemplate:
    """A statblock a GM can base a monster on. `stats` has every stat of the rule set's character,
    and `attacks` maps each attack's name to its dice formula. `attacks_per_action` is how many
    of those attacks the statblock makes in one action. `attack_flags` gives the flags of the
    attacks that have any, such as blast, and `attack_shock` gives the Shock of the attacks that
    have any, as (shock, shock_ac) pairs. A large group is a group the GM fights as one unit.
    `rules` are the ids of the rules attached to the monster; `rule_text` holds the text of those
    written in the statblock itself."""

    id: str
    name: str
    description: str
    stats: Mapping[str, int]
    armor: int
    attacks: Mapping[str, str]
    attacks_per_action: int = 1
    large_group: bool = False
    rules: tuple[str, ...] = ()
    attack_flags: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    attack_shock: Mapping[str, tuple[int, int]] = field(default_factory=dict)
    rule_text: Mapping[str, str] = field(default_factory=dict)
    # The statblock's special abilities as structured rules instead of prose alone.
    abilities: tuple[MonsterAbility, ...] = ()
    # The creature icon the statblock shows, resolved from `icon` or `looks_like` at load time.
    icon: str = FALLBACK_MONSTER
    # The icon or definition whose looks this creature borrows. Kept for authors and lookups.
    looks_like: str | None = None


DAMAGE_KINDS = ("absorb", "harm", "overflow")
# When a condition can end: by a rest, extended rest, aid from someone else, the passing of time,
# or the GM's say-so. A condition's `ends` lists the ones that end it; `gm` always does.
# The trigger any condition can end by, decided by the GM. A rule set names its other triggers.
ALWAYS_CONDITION_END = "gm"
# The key of a condition's `modifies` that applies to every stat.
ALL_STATS = "*"
# Situational keys a condition may modify besides the rule set's stats: attack rolls and checks.
SITUATIONAL_MODIFIERS = ("attack", "check")
# The `end_condition` id that ends every condition the character holds.
ALL_CONDITIONS = "*"
# `if_higher` compares the roll with the maximum; `if_above_current` compares it with the current score.
MAX_MODES = ("if_higher", "if_above_current", "add", "always")
# The name a `pick_stat` op stores its choice under, for later ops to use as their `stat`.
PICKED = "$picked"
# A move's `stat` of "highest" rolls the best of its `stat_choice`, without the player picking.
HIGHEST = "highest"
DICE_SIDES = re.compile(r"1d[0-9]+")
# The prose a statblock used to carry. The structured `attacks_per_action` field is the source of
# truth now; the loader reads this only to refuse a statblock that still relies on the prose.
ATTACKS_PER_ACTION_TEXT = re.compile(r"\b\d+\s+attacks?\s+per\s+action\b", re.IGNORECASE)


@dataclass(frozen=True)
class DamageStep:
    """One step a hit's damage passes through, in order. `absorb` takes the target's armor off the
    hit. `harm` takes up to the stat's current value. `overflow` takes the rest, and the target owes
    a save on the stat. Damage left after the last step has no effect."""

    kind: str
    stat: str | None


@dataclass(frozen=True)
class DerivedRule:
    """A condition the stats show rather than one stored on the character. It holds while `stat`'s
    current score is `current`, and, when `needs_max`, the stat's maximum is above 0."""

    stat: str
    current: int
    needs_max: bool


@dataclass(frozen=True)
class ConditionRules:
    """A condition an injury or an effect can give a character. `ends` lists the triggers that end it,
    and `on_end` are the effect ops run when it does. A derived condition has `derived` instead.
    `dying` and `fatal` mark a condition that leaves a player character dying, or one that makes a
    failed save fatal; a rule set with combat collects them into its CombatRules."""

    id: str
    label: str
    description: str
    ends: tuple[str, ...]
    on_end: tuple[Mapping[str, Any], ...]
    derived: DerivedRule | None = None
    # Amounts added to a move's total while the condition holds, by stat id or ALL_STATS.
    modifies: Mapping[str, int] = field(default_factory=dict)
    dying: bool = False
    fatal: bool = False


@dataclass(frozen=True)
class CounterRules:
    """A number a character keeps within a range, such as Experience. When a change takes it up to
    its maximum, the `full` effects run, so a counter can trigger an outcome when it fills."""

    id: str
    label: str
    minimum: int
    maximum: int | None
    initial: int
    full: tuple[Mapping[str, Any], ...]
    # What the counter means, for rule-set authors and lookup tools.
    description: str = ""


@dataclass(frozen=True)
class CounterCap:
    """A stat cap that depends on a counter, such as Academia's Year cap: +2 in Years 1-3,
    +3 in Years 4-6, and +4 in Year 7."""

    counter: str
    bands: Mapping[int, int]

    def maximum_for(self, counters: Mapping[str, int]) -> int | None:
        value = counters.get(self.counter)
        if value is None:
            return None
        return self.bands.get(int(value))


@dataclass(frozen=True)
class AdvancementOption:
    """One thing an Advancement can buy. `limit` is how many times a character may take it, or None
    when unlimited. `picks` says the option takes a stat the player names, which its effects reach
    as `$picked`."""

    value: str
    label: str
    text: str
    limit: int | None
    picks: bool
    effects: tuple[Mapping[str, Any], ...]
    # The feature the option lets the character learn, or None when it teaches none.
    learn: LearnRules | None = None
    # The highest value a picked stat may reach, a counter-banded cap, or None for no cap.
    maximum: int | CounterCap | None = None

    def maximum_for(self, counters: Mapping[str, int]) -> int | None:
        if isinstance(self.maximum, CounterCap):
            return self.maximum.maximum_for(counters)
        return self.maximum


@dataclass(frozen=True)
class LearnRules:
    """A feature an option lets a character learn: one of `feature_type`, optionally restricted to
    the tag held by the character field `tag_field`, never one of `exclude`, and never a feature
    whose tag matches one of the character fields in `exclude_tag_fields`. Existing features may
    be learned again only if repeatable."""

    feature_type: str
    tag: str = ""
    tag_field: str = ""
    exclude: tuple[str, ...] = ()
    exclude_tag_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdvancementRules:
    """What a character spends from `counter`, one point each, on one of the options."""

    id: str
    label: str
    counter: str
    options: Mapping[str, AdvancementOption]


@dataclass(frozen=True)
class LevelUpPick:
    """One kind of choice a level-up offers.

    A `skill` pick raises a skill to a new level for the cost in `costs`, at the character level
    in `minimum_levels`. A `stat` pick buys one attribute boost per point spent from `counter`,
    limited by `limit_counter` and `maximum`. A `feature` pick spends one point of `counter` to
    learn one of the features `options` returns. Every pick has an `apply` hook that runs the
    mechanical change through the rule set's own tools.
    """

    id: str
    label: str
    kind: str
    counter: str
    costs: Mapping[int, int] = field(default_factory=dict)
    minimum_levels: Mapping[int, int] = field(default_factory=dict)
    maximum: int | None = None
    limit_counter: str = ""
    # For a stat pick, only stats in this swap group may be raised, such as "attributes".
    stat_group: str = ""
    feature_type: str = ""
    tags: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    # The rule set's functions that list the features a pick offers and apply one.
    options: Callable[..., Any] | None = None
    apply: Callable[..., Any] | None = None


@dataclass(frozen=True)
class LevelUpRules:
    """A level progression a rule set defines in its JSON.

    `apply_level` runs the book's automatic gains for one new level, such as hit points, attack
    bonus, and any new pools of picks. `summary` describes those gains for the page before it is
    submitted. `picks` are the choices the player then spends, each spending its own pool.
    """

    id: str
    label: str
    level_counter: str
    xp_counter: str
    tracks: Mapping[str, tuple[int, ...]]
    rate_option: str
    max_level: int
    apply_level: Callable[..., Any]
    summary: Callable[..., Any] | None
    picks: Mapping[str, LevelUpPick]
    # What the level-up means in the rule set's own words.
    description: str = ""
    # Template kinds a character needs at least one of to gain a level, such as a class.
    template_kinds: tuple[str, ...] = ()


@dataclass(frozen=True)
class LevelGrants:
    """The automatic gains of one new level, projected without changing anything.

    The rule set's `project_level` builds it from its charts, and both the page summary and the
    applied level use that one projection, so the two can never grant different pools. `pools`
    maps a counter to the points it gains; `values` carries the non-counter gains such as hit
    dice and attack bonus.
    """

    level: int
    new_level: int
    xp: int
    threshold: int
    rate: str
    pools: Mapping[str, int]
    values: Mapping[str, Any]
    lines: tuple[str, ...] = ()

    def granted(self, counter: str) -> int:
        """How many points of one pool the level grants."""
        return int(self.pools.get(counter, 0))

    def as_summary(self) -> dict[str, Any]:
        """The JSON-friendly summary the advancement page shows."""
        return {
            "level": self.level,
            "new_level": self.new_level,
            "xp": self.xp,
            "threshold": self.threshold,
            "rate": self.rate,
            "attack_bonus": self.values.get("attack_bonus"),
            "castings": self.values.get("castings"),
            "lines": list(self.lines),
            "grants": dict(self.pools),
        }


@dataclass(frozen=True)
class PeriodRules:
    """A stretch of play, such as a school year, that ends for every character at once. Its
    effects run for each character."""

    id: str
    label: str
    effects: tuple[Mapping[str, Any], ...]
    # A short player-facing description of when the period ends.
    description: str = ""
    # The tallies that start over when the period ends, after their final totals are logged.
    tallies: tuple[str, ...] = ()
    # Reaction reset labels this period clears, such as "week".
    resets: tuple[str, ...] = ()


@dataclass(frozen=True)
class TallyRules:
    """A running score the campaign keeps by entry, such as the points of each House."""

    id: str
    label: str
    # Entry values and their labels, in file order. A dynamic tally starts empty and gains its
    # entries from campaign state, such as the colleges the GM invents.
    entries: Mapping[str, str]
    dynamic: bool = False
    # The most entries a dynamic tally may hold, or None for no limit. Academia's College Cup
    # caps colleges at five; the generic faction service does not own that number.
    max_entries: int | None = None


@dataclass(frozen=True)
class RelationRules:
    """A named link a character can have with others, such as a Friend. `limit` is how many a
    character may have at once, or None for no limit."""

    id: str
    label: str
    limit: int | None


@dataclass(frozen=True)
class MoveChoice:
    """One cost or consequence a player may pick for a move band. Choice effects run only when the
    player picks that choice; the band's own effects always run."""

    id: str
    label: str
    text: str = ""
    effects: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class MoveBand:
    """One outcome of a move. The first band whose `min` the total reaches applies, so bands run from
    the highest `min` down. The last band has no `min` and takes whatever is left. A band with
    `choices` pauses for the player to pick one before the reply continues."""

    label: str
    text: str
    min: int | None
    effects: tuple[Mapping[str, Any], ...]
    choices: tuple[MoveChoice, ...] = ()


@dataclass(frozen=True)
class MoveRules:
    """A move: a roll of `dice`, plus the stat's modifier when it has one, sorted into `bands`."""

    id: str
    label: str
    description: str
    dice: str
    # The stat rolled, or HIGHEST for the best of `stat_choice`, or None when the player picks.
    stat: str | None
    bands: tuple[MoveBand, ...]
    # The stats the player picks from, or that a HIGHEST move takes the best of. Empty otherwise.
    stat_choice: tuple[str, ...] = ()
    # The skill the move is made with, added at its level. Empty when the move has no skill.
    skill: str = ""
    # Minimum skill contribution, or None when a negative level applies without a floor.
    skill_floor: int | None = 0
    # The move is only open once the character holds every stored condition.
    needs_all_conditions: bool = False
    # Free-form tags a reaction can match, such as "spell" or "social".
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class InjuryEntry:
    roll: int
    name: str
    text: str
    effects: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class InjuryTable:
    """The injury table, its triggering stat, and the targets and hits that use it."""

    id: str
    stat: str
    entries: tuple[InjuryEntry, ...]
    trigger: str
    target_kinds: tuple[str, ...]


@dataclass(frozen=True)
class MoraleTrigger:
    """One configured morale check. `tag` is the label recorded in the log; `when` names a
    registered condition such as first_casualty, half_strength, or lone."""

    tag: str
    when: str


MORALE_TRIGGER_WHENS = ("lone", "first_casualty", "half_strength")


@dataclass(frozen=True)
class MoraleRules:
    """When a side of NPCs must make a morale save and what it is made with. `save_stat` is the
    leader's stat the save uses. `triggers` lists the checks the rule set makes, in order, each
    with its own log tag and registered condition. `lone_at` is the stat a lone foe checks at
    when it reaches 0. `retreat_stat` is the stat a character saves against to run from a fight."""

    save_stat: str
    triggers: tuple[MoraleTrigger, ...]
    lone_at: str
    retreat_stat: str


@dataclass(frozen=True)
class SaveRules:
    """How a save is decided. `mode` is "roll_under" (succeeds at or under the score) or
    "roll_over" (succeeds at or over it). `auto_success` and `auto_fail` are rolls that always pass
    or always fail, or None for no such roll. Missing rules are supplied by the combat die."""

    mode: str = "roll_under"
    auto_success: int | None = None
    auto_fail: int | None = None


@dataclass(frozen=True)
class LevelDamageRules:
    """The templates whose counter contributes to damage, rounded up by `divisor`."""

    template_kind: str
    templates: tuple[str, ...]
    counter: str
    divisor: int


@dataclass(frozen=True)
class GroupRules:
    """How large groups fight. `incoming_modifier` is the only modifier that may hit a group.
    `blast_modifier` is the dice a group's Blast attack rolls for each target. When
    `blast_hits_all` is false, a blast hits only the named target. `break_outcomes` are the GM's
    decisions a broken group may take."""

    incoming_modifier: str = "impaired"
    blast_modifier: str = "enhanced"
    blast_hits_all: bool = True
    break_outcomes: tuple[str, ...] = ("defeat", "flee")


@dataclass(frozen=True)
class CheckRules:
    """A check a rule set resolves with dice, such as a skill check. A character adds their level
    in the skill, or `unskilled` when they have none, and the roll must reach a difficulty that
    is kept within `minimum_difficulty` and `maximum_difficulty`. A missing difficulty is
    `default_difficulty`."""

    id: str
    label: str
    dice: str
    unskilled: int = -1
    default_difficulty: int = 8
    minimum_difficulty: int = 2
    maximum_difficulty: int = 30


@dataclass(frozen=True)
class InitiativeRules:
    """How a rule set rolls initiative for whole sides rather than for each character. Each side
    rolls `die` once and adds the best modifier of `bonus_stat` among its members. `tie` names the
    side that acts first when two totals are equal, "party" or "foes". A rule set with `side`
    false keeps its `initiative_mode` instead."""

    die: str = "1d8"
    bonus_stat: str = "dex"
    side: bool = True
    tie: str = "party"


@dataclass(frozen=True)
class AttackRules:
    """How attacks are resolved: damage order, hit mode, modifiers, group fighting, and the
    bonuses or penalties an attack applies."""

    damage: tuple[DamageStep, ...]
    modifiers: Mapping[str, str] = field(default_factory=dict)
    hit_mode: str = "auto"
    multi_attack: str = "highest"
    groups: GroupRules | None = None
    hit_bonus_stat: str = ""
    unskilled_attack_penalty: int = 0
    damage_bonus_stat: str = ""
    attribute_damage: bool = False
    shock_on_miss: bool = False
    minimum_shock_damage: bool = False
    natural_one_misses: bool = False
    natural_max_hits: bool = False
    level_damage: LevelDamageRules | None = None


@dataclass(frozen=True)
class DefenseRules:
    """How armor and defense are calculated."""

    armor_max: int = 0
    base: int = 0
    modifier: str = ""


@dataclass(frozen=True)
class DyingRules:
    """The clocks and conditions that govern defeat and death, and the harm stats outside combat."""

    outside_harm: tuple[str, ...] = ()
    dying_hours: int = 0
    dying_rounds: int = 0
    dying_conditions: tuple[str, ...] = ()
    fatal_conditions: tuple[str, ...] = ()
    aid_items: tuple[str, ...] = ()


@dataclass(frozen=True)
class UnarmedRules:
    """What an unarmed strike rolls, what it is called, and what it may add."""

    damage: str = ""
    names: frozenset[str] = frozenset()
    attributes: tuple[str, ...] = ()
    skill: str = ""


@dataclass(frozen=True)
class CombatRules:
    """How a rule set resolves combat. The attack, defense, dying, and unarmed systems are
    capability objects; the remaining fields describe checks, initiative, saves, defeat, and the
    optional ruleset module."""

    id: str
    check_die: str
    check_sides: int
    initiative_stat: str
    defeat: Mapping[str, str]
    attack: AttackRules
    defense: DefenseRules = field(default_factory=DefenseRules)
    dying: DyingRules = field(default_factory=DyingRules)
    unarmed: UnarmedRules = field(default_factory=UnarmedRules)
    injury_table: InjuryTable | None = None
    # The stat healed when a tool call does not name one.
    healing_stat: str | None = None
    morale: MoraleRules | None = None
    save: SaveRules = field(default_factory=SaveRules)
    initiative_mode: str = "pass_npcs_fail"
    # The stats whose reaching 0 defeats a character.
    defeat_at_zero: tuple[str, ...] = ()
    # The side-based initiative roll, or None when each character rolls instead.
    initiative: InitiativeRules | None = None
    # The rule set's Python functions, when it names a python_module. Each function, when present,
    # overrides the declarative save and initiative rules above.
    module: ModuleType | None = None
    # The defense bonus from spending a Main action on total defense. Zero when the rule set
    # has no such action.
    total_defense_bonus: int = 0
    # The hit modifier for each attack range band, such as {"short": 0, "long": -2}.
    range_penalties: Mapping[str, int] = field(default_factory=dict)
    # Two-weapon fighting's hit penalty and damage bonus. Zero when the rule set has no such
    # action.
    dual_wield_hit_penalty: int = 0
    dual_wield_damage_bonus: int = 0
    # How many of a surprised character's turns are lost. Zero when the rule set has no
    # surprise rule.
    surprise_turns: int = 0

    @property
    def damage(self) -> tuple[DamageStep, ...]:
        return self.attack.damage

    @property
    def modifiers(self) -> Mapping[str, str]:
        return self.attack.modifiers

    @property
    def hit_mode(self) -> str:
        return self.attack.hit_mode

    @property
    def multi_attack(self) -> str:
        return self.attack.multi_attack

    @property
    def groups(self) -> GroupRules | None:
        return self.attack.groups

    @property
    def hit_bonus_stat(self) -> str:
        return self.attack.hit_bonus_stat

    @property
    def unskilled_attack_penalty(self) -> int:
        return self.attack.unskilled_attack_penalty

    @property
    def damage_bonus_stat(self) -> str:
        return self.attack.damage_bonus_stat

    @property
    def attribute_damage(self) -> bool:
        return self.attack.attribute_damage

    @property
    def shock_on_miss(self) -> bool:
        return self.attack.shock_on_miss

    @property
    def minimum_shock_damage(self) -> bool:
        return self.attack.minimum_shock_damage

    @property
    def natural_one_misses(self) -> bool:
        return self.attack.natural_one_misses

    @property
    def natural_max_hits(self) -> bool:
        return self.attack.natural_max_hits

    @property
    def level_damage(self) -> LevelDamageRules | None:
        return self.attack.level_damage

    @property
    def armor_max(self) -> int:
        return self.defense.armor_max

    @property
    def defense_base(self) -> int:
        return self.defense.base

    @property
    def defense_modifier(self) -> str:
        return self.defense.modifier

    @property
    def outside_harm(self) -> tuple[str, ...]:
        return self.dying.outside_harm

    @property
    def dying_hours(self) -> int:
        return self.dying.dying_hours

    @property
    def dying_rounds(self) -> int:
        return self.dying.dying_rounds

    @property
    def dying_conditions(self) -> tuple[str, ...]:
        return self.dying.dying_conditions

    @property
    def fatal_conditions(self) -> tuple[str, ...]:
        return self.dying.fatal_conditions

    @property
    def aid_items(self) -> tuple[str, ...]:
        return self.dying.aid_items

    @property
    def unarmed_damage(self) -> str:
        return self.unarmed.damage

    @property
    def unarmed_names(self) -> frozenset[str]:
        return self.unarmed.names

    @property
    def unarmed_attributes(self) -> tuple[str, ...]:
        return self.unarmed.attributes

    @property
    def unarmed_skill(self) -> str:
        return self.unarmed.skill

    def save_succeeds(self, roll: int, score: int) -> bool:
        """Whether a save with this roll succeeds against a score."""
        if self.module is not None and callable(getattr(self.module, "save_succeeds", None)):
            return bool(self.module.save_succeeds(roll, score))
        if self.save.auto_success is not None and roll == self.save.auto_success:
            return True
        if self.save.auto_fail is not None and roll == self.save.auto_fail:
            return False
        if self.save.mode == "roll_over":
            return roll >= score
        return roll <= score

    def initiative_order(self, entries: Sequence[tuple[str, bool | None]]) -> list[str]:
        """The order of round one from (name, result) pairs: True passed the initiative save, None is
        an NPC, and False failed it. Under "pass_npcs_fail", passers act first, then NPCs, then
        those who failed, and each group keeps its order. Under "roll_order", the entries keep the
        order they came in."""
        if self.module is not None and callable(getattr(self.module, "initiative_order", None)):
            return list(self.module.initiative_order(entries))
        if self.initiative_mode == "roll_order":
            return [name for name, _ in entries]
        passed = [name for name, result in entries if result is True]
        npcs = [name for name, result in entries if result is None]
        failed = [name for name, result in entries if result is False]
        return passed + npcs + failed


@dataclass(frozen=True)
class ClockRules:
    """The parts of a day, in order. A place not visited for `stale_days` days is flagged when a
    new day begins, so the GM can update it."""

    parts: tuple[str, ...]
    stale_days: int
    # The currency feature a hireling's wage is paid in, taken from its employer each day. Empty
    # when the rule set has no wages.
    wage_currency: str = ""
    # How many hours each part of the day holds. Hours count the time a dying character has left.
    hours_per_part: int = 1
    # The length of the reset week. Seven by default; a rule set can name another cycle.
    week_days: int = 7


@dataclass(frozen=True)
class HousekeepingTaskRules:
    """A scene task the GM should settle after `after_days` campaign days. `task` is the sentence
    the GM sees; it may use the placeholders {place}, {names}, {count}, and {days}."""

    after_days: int
    task: str


@dataclass(frozen=True)
class HousekeepingRules:
    """What the GM should tidy in a place that earlier scenes left behind: the bodies of the
    fallen and the debris of a fight. Both are optional; a rule set may define neither."""

    bodies: HousekeepingTaskRules | None = None
    fight_debris: HousekeepingTaskRules | None = None


@dataclass(frozen=True)
class RestLength:
    """A rest the GM can call for. It passes `hours` hours, `parts` parts of a day, or `days`
    days, and `until_morning` runs on to the next morning. A full rest restores what `restores`
    lists and clears the item kinds in `clears`. `ends` lists the condition-ending triggers the
    rest counts as, such as "rest" or "extended_rest". A character the hooks hold back recovers
    nothing."""

    id: str
    hours: int
    parts: int
    days: int
    until_morning: bool
    full: bool
    restores: tuple[str, ...]
    clears: tuple[str, ...]
    # The condition-ending triggers this rest counts as, named by the rest definition.
    ends: tuple[str, ...] = ()
    # Whether the rest has upkeep: the rule set's before_rest hook runs for it.
    upkeep: bool = False
    # For a rest that runs until morning: a part it begins in maps to the part it ends at instead,
    # such as a night rest begun in the morning that stops at the night part.
    stop_at: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class InventoryPool:
    """One named pool of inventory slots, such as a Stowed pack or the items kept to hand.

    A pool has either a fixed `capacity` or a capacity derived from a character's `stat`: the raw
    score times `multiplier`, plus `base`, divided by `divisor`, and rounded `up` or `down`.
    `ready` marks the pool as the one whose items a character can use without spending an action;
    a rule set has at most one readied pool."""

    id: str
    name: str
    ready: bool = False
    # The fixed slot count for a pool that does not derive capacity from a stat.
    capacity: int | None = None
    # The stat a derived pool's capacity comes from. Empty for a fixed pool.
    stat: str = ""
    multiplier: float = 1.0
    divisor: int = 1
    # "down" or "up": how the derived slot count rounds after dividing.
    rounding: str = "down"
    # Slots added to the scaled score before dividing.
    base: int = 0

    def capacity_for(self, score: int) -> int:
        """The slots this pool holds for a character with `score` in its stat. The result is
        never negative."""
        if self.capacity is not None:
            return self.capacity
        scaled = self.base + score * self.multiplier
        if self.rounding == "up":
            slots = math.ceil(scaled / self.divisor)
        else:
            slots = math.floor(scaled / self.divisor)
        return max(0, int(slots))


@dataclass(frozen=True)
class InventoryRules:
    """How many slots a character's inventory has, what the penalty costs, and which stat takes harm
    when the inventory fills. `item_sizes` is in `RuleSet`.

    Named `pools` are the preferred model: each has its own fixed or stat-derived capacity, and
    `size_pools` maps item sizes to them, falling back to `default_pool`. A rule set with no pools
    keeps the single-pool model: it may leave `capacity_stat` empty and use the fixed `capacity`,
    or name a stat and let each character's raw score in it set their capacity, times
    `capacity_multiplier`. `readied_stat`/`readied_divisor` remain for the single-pool model; a
    named readied pool carries the same idea. `full_stat` is empty when a full inventory drains
    nothing."""

    capacity: int
    penalty_name: str
    # How long the penalty lasts, as a phrase, such as "until a full night's rest".
    penalty_lasts: str
    penalty_item: str
    penalty_slots: int
    # The stat that takes harm when the inventory fills. Empty when filling it drains no stat.
    full_stat: str = ""
    # The stat whose raw score is a character's capacity. Empty when `capacity` is used instead.
    capacity_stat: str = ""
    # What a character's score in `capacity_stat` is multiplied by. One means the score itself.
    capacity_multiplier: float = 1.0
    # A second pool for items the character can use without spending an action. Empty when the
    # rule set has no Readied pool; `readied_divisor` divides the stat's score.
    readied_stat: str = ""
    readied_divisor: int = 1
    # Named pools, in definition order. Empty when the rule set uses the single-pool model.
    pools: tuple[InventoryPool, ...] = ()
    # Item size id to the pool id its items occupy by default.
    size_pools: Mapping[str, str] = field(default_factory=dict)
    # The pool every item size without a `size_pools` entry occupies. Empty with no pools.
    default_pool: str = ""


@dataclass(frozen=True)
class Hooks:
    """Rule-set code the engine calls at named points. `module` holds the functions; `functions`
    maps each hook name the definition declares to a function name in that module. A hook the rule
    set omits is never called, and the engine returns the caller's default instead. `options` is
    the plain mapping from the hooks definition, for the functions to read."""

    module: ModuleType
    functions: Mapping[str, str] = field(default_factory=dict)
    options: Mapping[str, Any] = field(default_factory=dict)

    def function_name(self, hook_name: str) -> str | None:
        """The function the definition names for `hook_name`, or None when it declares none."""
        return self.functions.get(hook_name)


@dataclass(frozen=True)
class ShopItem:
    """A catalogue item. `price` is in the rule set's currency. `armor` changes the buyer's armor as
    `armor_mode` says: "add" adds it, "set" replaces the armor with it, and "max" keeps the higher
    of the two. `damage` is the attack die the item gives, as text for the GM."""

    id: str
    name: str
    category: str
    price: int
    size: str
    armor: int
    damage: str
    uses: int
    description: str
    armor_mode: str = "add"
    # Check ids this worn item penalizes by its Encumbrance, such as Sneak or Exert.
    encumbrance_checks: tuple[str, ...] = ()
    # The icon the catalogue entry shows, resolved from `icon` or `looks_like` at load time.
    icon: str = FALLBACK_ITEM
    # The icon or definition whose looks this item borrows. Kept for authors and lookups.
    looks_like: str | None = None


@dataclass(frozen=True)
class ShopService:
    """A service the marketplace sells. `effect` is "lodge", "restore", or "deferred". A lodge
    covers `fits` characters a night. A restore gives back the attributes in `restores`."""

    id: str
    name: str
    price: int
    effect: str
    fits: int = 1
    restores: tuple[str, ...] = ()
    # A short player-facing description of what the service provides.
    description: str = ""


@dataclass(frozen=True)
class Hireling:
    id: str
    name: str
    wage_per_day: int
    # A short player-facing description of the work the hireling does.
    description: str = ""


@dataclass(frozen=True)
class Marketplace:
    # The feature id of the currency, such as gold. Buying takes it from the buyer's features.
    currency: str
    items: tuple[ShopItem, ...]
    services: tuple[ShopService, ...]
    hirelings: tuple[Hireling, ...]


@dataclass(frozen=True)
class ItemKind:
    """The name items of a kind carry, and the size a held item takes. A charged item's size is
    given when it is made, so its kind has none. `icon` names the picture the kind shows, if any."""

    kind: str
    size: str = ""
    icon: str | None = None


@dataclass(frozen=True)
class ItemMagic:
    """Cairn-style item magic: a reusable spellbook, a single-use scroll, and a charged relic."""

    reusable: ItemKind
    single_use: ItemKind
    charged: ItemKind
    cast_penalty: int


@dataclass(frozen=True)
class PreparedMagic:
    """WWN-style prepared magic. A caster prepares spells, spends castings, and commits Effort."""

    castings_stat: str = "castings"
    prepared_mark: str = "prepared"
    effort_mark: str = "effort_committed"


@dataclass(frozen=True)
class MoveMagic:
    """Academia-style move magic: a spell is cast through a named move."""

    move: str = "cast_a_spell"
    spell_field: str = "spell"


@dataclass(frozen=True)
class MagicRules:
    """One of the engine's magic models. `model` selects the adapter; the matching field carries
    that adapter's data. Item magic keeps the historical `reusable`/`single_use`/`charged`
    properties so existing item tools and call sites stay unchanged."""

    model: str = "item"
    item: ItemMagic | None = None
    prepared: PreparedMagic | None = None
    move: MoveMagic | None = None

    def _item(self) -> ItemMagic:
        if self.item is None:
            raise ContentError("this rule set does not use item magic")
        return self.item

    @property
    def reusable(self) -> ItemKind:
        return self._item().reusable

    @property
    def single_use(self) -> ItemKind:
        return self._item().single_use

    @property
    def charged(self) -> ItemKind:
        return self._item().charged

    @property
    def cast_penalty(self) -> int:
        return self._item().cast_penalty

    @property
    def item_kind_icons(self) -> dict[str, str]:
        """The icon each item kind names in the rule set's data, by kind."""
        if self.item is None:
            return {}
        kinds = (self.item.reusable, self.item.single_use, self.item.charged)
        return {k.kind: k.icon for k in kinds if k.icon is not None}


@dataclass(frozen=True)
class PromptFragment:
    """A rule-set sentence group for the GM prompt, such as how armor absorbs a blow. `step` is
    "state" for the state step, "narration" for the narration step, or "all" for both.
    `unwritten_only` marks a fragment only for a world with no authored setting, so the GM
    invents the setting rather than borrowing one."""

    id: str
    text: str
    step: str = "state"
    unwritten_only: bool = False
    # Capability paths that must be truthy for the fragment to be offered.
    requires: tuple[str, ...] = ()
    # Capability paths and the values that enable the fragment.
    enabled_by: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # Prompt ids this fragment replaces, such as a rule set's own setting instructions replacing
    # the core ones.
    supersedes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChecklistItem:
    """One area the state step must check before it narrates. Its id is the argument the
    finish_state_step tool takes a note for."""

    id: str
    text: str
    # The optional tool group that adds this area, or "" when every rule set checks it.
    group: str = ""


@dataclass(frozen=True)
class ChecklistRules:
    """The rule set's turn checklist: the areas every state step must check, in order. A live A/B
    in November 2026 found this checklist, not restoring the "when to use" sentences in the brief
    tool schemas, drove the character-summary and relationship updates."""

    items: tuple[ChecklistItem, ...]


@dataclass(frozen=True)
class RuleContent:
    """The content a rule set carries: rules, tables, monsters, encounter and world terrain,
    prompt fragments, and the other definition-shaped data."""

    rules: Mapping[str, str]
    tables: Mapping[str, tuple[TableOption, ...]] = field(default_factory=dict)
    important: frozenset[str] = frozenset()
    attached_rules: Mapping[str, str] = field(default_factory=dict)
    monsters: Mapping[str, MonsterTemplate] = field(default_factory=dict)
    npc_hp: Mapping[str, int] = field(default_factory=dict)
    npc_hp_stat: str | None = None
    dice_tables: Mapping[str, DiceTable] = field(default_factory=dict)
    prompt_fragments: tuple[PromptFragment, ...] = ()
    # The areas every state step must check, or None when the rule set has no turn checklist.
    checklist: ChecklistRules | None = None
    sentence_cap: int | None = None
    terrain: Mapping[str, EncounterTerrain] = field(default_factory=dict)
    encounter_objects: Mapping[str, EncounterObject] = field(default_factory=dict)
    encounter_templates: Mapping[str, EncounterTemplate] = field(default_factory=dict)
    world_terrain: Mapping[str, WorldTerrain] = field(default_factory=dict)
    optional_templates: tuple[str, ...] = ()
    # What the GM should tidy at a place that earlier scenes left behind, or None when the rule
    # set does not ask for scene housekeeping.
    housekeeping: HousekeepingRules | None = None


@dataclass(frozen=True)
class ProgressionRules:
    """The optional character-progression systems a rule set enables."""

    counters: Mapping[str, CounterRules] = field(default_factory=dict)
    advancements: Mapping[str, AdvancementRules] = field(default_factory=dict)
    level_ups: Mapping[str, LevelUpRules] = field(default_factory=dict)
    periods: Mapping[str, PeriodRules] = field(default_factory=dict)
    tallies: Mapping[str, TallyRules] = field(default_factory=dict)
    relations: Mapping[str, RelationRules] = field(default_factory=dict)
    moves: Mapping[str, MoveRules] = field(default_factory=dict)


@dataclass(frozen=True)
class TimeRules:
    """The clock, rests, travel, and wilderness procedure a rule set enables."""

    clock: ClockRules | None = None
    rests: Mapping[str, RestLength] = field(default_factory=dict)
    interrupted_rest: str = ""
    travel: Travel | None = None
    journey: JourneyRules | None = None
    wilderness: Wilderness | None = None


@dataclass(frozen=True)
class ItemRules:
    """Inventory, item sizes, magic items, and the marketplace."""

    inventory: InventoryRules | None = None
    item_sizes: Mapping[str, int] = field(default_factory=dict)
    magic: MagicRules | None = None
    marketplace: Marketplace | None = None


@dataclass(frozen=True)
class ProjectDefinition:
    """A source-defined long effort: its default target, cost, currency, and required skill."""

    id: str
    label: str
    description: str
    target: int = 1
    cost: int = 0
    currency: str = ""
    skill: str = ""
    skill_level: int = 0


@dataclass(frozen=True)
class FactionAssetDefinition:
    """A source-defined faction asset: what it costs, its hit points, and how it fights."""

    id: str
    label: str
    description: str
    category: str
    rating: int
    cost: int
    hp: int
    magic: str
    attack: str = ""
    counter: str = ""
    qualities: tuple[str, ...] = ()


MAGIC_CAPABILITIES = ("item", "prepared", "move", "none")
TRAVEL_CAPABILITIES = ("terrain", "narrative", "watches", "none")
SOCIAL_CAPABILITIES = (
    "conditions",
    "counters",
    "checks",
    "moves",
    "relations",
    "tallies",
    "factions",
    "projects",
    "periods",
)
# A capability path a `requires` or `enabled_by` entry may name. A dotted path under `social`
# asks for one social system, such as "social.conditions".
CAPABILITY_PATHS = frozenset(
    {
        "magic",
        "travel",
        "wilderness",
        "dungeon",
        "combat",
        "social",
        *(f"social.{name}" for name in SOCIAL_CAPABILITIES),
    }
)


@dataclass(frozen=True)
class Capabilities:
    """What optional parts of the engine a rule set offers. Core tools and prompts name a
    capability path in `requires` or `enabled_by`, and the loader offers them only when the rule
    set supports it. A rule set without a `capabilities` block keeps every inherited definition,
    so older content loads unchanged."""

    # The magic model: item, prepared, move, or none.
    magic: str = "none"
    # How overland travel works: terrain, narrative, watches, or none.
    travel: str = "none"
    wilderness: bool = False
    dungeon: bool = False
    # Whether the rule set resolves combat with initiative, attacks, and encounters.
    combat: bool = False
    # The social systems the rule set defines, from SOCIAL_CAPABILITIES.
    social: tuple[str, ...] = ()

    def supports(self, path: str) -> bool:
        """Whether the capability at `path` is present and not "none"."""
        if path == "magic":
            return self.magic != "none"
        if path == "travel":
            return self.travel != "none"
        if path == "wilderness":
            return self.wilderness
        if path == "dungeon":
            return self.dungeon
        if path == "combat":
            return self.combat
        if path == "social":
            return bool(self.social)
        if path.startswith("social."):
            return path.removeprefix("social.") in self.social
        return False

    def accepts(self, path: str, values: Sequence[object]) -> bool:
        """Whether the capability at `path` is one of `values`."""
        if path == "magic":
            return self.magic in values
        if path == "travel":
            return self.travel in values
        if path == "wilderness":
            return self.wilderness in values
        if path == "dungeon":
            return self.dungeon in values
        if path == "combat":
            return self.combat in values
        if path == "social":
            return any(value in self.social for value in values)
        return False


@dataclass(frozen=True)
class DefaultCampaign:
    """The rule set and world the New Campaign form starts on, from core's head."""

    rule_set: str
    world_template: str


@dataclass(frozen=True)
class RuleSet:
    """One loaded rule set. The shared systems are grouped into capability objects; the read-only
    aliases below keep older consumers working without flattening the model back into a union."""

    id: str
    name: str
    description: str
    content: RuleContent
    tool_calls: Mapping[str, ToolCall]
    character: CharacterSpec
    # What "+" between dice terms means: "sum" adds them, "highest" keeps the highest total.
    plus: str
    # Options shared by every character in a campaign, such as the omen.
    campaign: CampaignSpec
    # The rule set and world the New Campaign form starts on, when core declares them.
    default_campaign: DefaultCampaign | None = None
    # The scale of encounter maps, or None when the rule set has no grid: its encounters have no
    # movement limit.
    grid: EncounterGrid | None = None
    # How combat resolves, or None when the rule set has no combat.
    combat: CombatRules | None = None
    # Conditions an injury or effect can give, by id. Derived conditions are included.
    conditions: Mapping[str, ConditionRules] = field(default_factory=dict)
    # Checks a character may make with dice, by id, such as a skill check.
    checks: Mapping[str, CheckRules] = field(default_factory=dict)
    # Source-defined project kinds, by id, such as a magic item or spell research.
    projects: Mapping[str, ProjectDefinition] = field(default_factory=dict)
    # Source-defined faction assets, by id, such as a unit of Infantry or a ring of Smugglers.
    faction_assets: Mapping[str, FactionAssetDefinition] = field(default_factory=dict)
    progression: ProgressionRules = field(default_factory=ProgressionRules)
    time: TimeRules = field(default_factory=TimeRules)
    items: ItemRules = field(default_factory=ItemRules)
    # The rule set's code hooks, or None when the rule set has none.
    hooks: Hooks | None = None
    # Triggers that may end a condition, from the rule set head.
    condition_ends: tuple[str, ...] = ()
    # A short line shown when choosing the rule set on the create campaign page.
    blurb: str | None = None
    # True when the rule set is imported from a third-party source; the UI links to the credits.
    third_party: bool = False
    # The genres of dice and creative tables this rule set offers, such as ("generic", "fantasy").
    # A core table tagged `generic`, or with no tags, is always offered; any other core table must
    # share a tag with this selection to reach the rule set.
    table_genres: tuple[str, ...] = ()
    # Rule-data values validated by the ruleset modules that declared them.
    rule_data: Mapping[str, object] = field(default_factory=dict)
    # The optional engine parts this rule set offers, declared in its head or inferred from its
    # definitions when the head has no `capabilities` block.
    capabilities: Capabilities = field(default_factory=Capabilities)

    @property
    def rules(self) -> Mapping[str, str]:
        return self.content.rules

    @property
    def tables(self) -> Mapping[str, tuple[TableOption, ...]]:
        return self.content.tables

    @property
    def important(self) -> frozenset[str]:
        return self.content.important

    @property
    def attached_rules(self) -> Mapping[str, str]:
        return self.content.attached_rules

    @property
    def monsters(self) -> Mapping[str, MonsterTemplate]:
        return self.content.monsters

    @property
    def npc_hp(self) -> Mapping[str, int]:
        return self.content.npc_hp

    @property
    def npc_hp_stat(self) -> str | None:
        return self.content.npc_hp_stat

    @property
    def dice_tables(self) -> Mapping[str, DiceTable]:
        return self.content.dice_tables

    @property
    def prompt_fragments(self) -> tuple[PromptFragment, ...]:
        return self.content.prompt_fragments

    @property
    def checklist(self) -> ChecklistRules | None:
        return self.content.checklist

    @property
    def sentence_cap(self) -> int | None:
        return self.content.sentence_cap

    @property
    def terrain(self) -> Mapping[str, EncounterTerrain]:
        return self.content.terrain

    @property
    def encounter_objects(self) -> Mapping[str, EncounterObject]:
        return self.content.encounter_objects

    @property
    def encounter_templates(self) -> Mapping[str, EncounterTemplate]:
        return self.content.encounter_templates

    @property
    def world_terrain(self) -> Mapping[str, WorldTerrain]:
        return self.content.world_terrain

    @property
    def optional_templates(self) -> tuple[str, ...]:
        return self.content.optional_templates

    @property
    def housekeeping(self) -> HousekeepingRules | None:
        return self.content.housekeeping

    @property
    def counters(self) -> Mapping[str, CounterRules]:
        return self.progression.counters

    @property
    def advancements(self) -> Mapping[str, AdvancementRules]:
        return self.progression.advancements

    @property
    def level_ups(self) -> Mapping[str, LevelUpRules]:
        return self.progression.level_ups

    @property
    def periods(self) -> Mapping[str, PeriodRules]:
        return self.progression.periods

    @property
    def tallies(self) -> Mapping[str, TallyRules]:
        return self.progression.tallies

    @property
    def relations(self) -> Mapping[str, RelationRules]:
        return self.progression.relations

    @property
    def moves(self) -> Mapping[str, MoveRules]:
        return self.progression.moves

    @property
    def clock(self) -> ClockRules | None:
        return self.time.clock

    @property
    def rests(self) -> Mapping[str, RestLength]:
        return self.time.rests

    @property
    def interrupted_rest(self) -> str:
        return self.time.interrupted_rest

    @property
    def travel(self) -> Travel | None:
        return self.time.travel

    @property
    def journey(self) -> JourneyRules | None:
        return self.time.journey

    @property
    def feature_types(self) -> Mapping[str, FeatureTypeRules]:
        return self.character.feature_types

    @property
    def wilderness(self) -> Wilderness | None:
        return self.time.wilderness

    @property
    def inventory(self) -> InventoryRules | None:
        return self.items.inventory

    @property
    def item_sizes(self) -> Mapping[str, int]:
        return self.items.item_sizes

    @property
    def magic(self) -> MagicRules | None:
        return self.items.magic

    @property
    def marketplace(self) -> Marketplace | None:
        return self.items.marketplace


@dataclass(frozen=True)
class ContentLibrary:
    world_templates: Mapping[str, WorldTemplate]
    # Selectable rule sets. The core rule set is not listed here: it is merged into each of these.
    rule_sets: Mapping[str, RuleSet]
    core: RuleSet
    # Built on first use for each (world, rule set) pair, since a world's statblocks are checked
    # against the rule set a campaign plays with.
    _world_content: dict[tuple[str, str], WorldContent] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def world_content(self, world: str, rule_set: str) -> WorldContent:
        """A world's own content checked against one rule set. Raises ContentError when a
        monster or NPC statblock does not fit that rule set."""
        key = (world, rule_set)
        if key not in self._world_content:
            self._world_content[key] = _build_world_content(
                self.world_templates[world], self.rule_sets[rule_set]
            )
        return self._world_content[key]

    @property
    def available_table_genres(self) -> tuple[str, ...]:
        """The genres used by at least one table in the loaded rule sets, in canonical order."""
        used = {
            genre
            for rule_set in (self.core, *self.rule_sets.values())
            for table in rule_set.dice_tables.values()
            for genre in table.genres
        }
        return tuple(genre for genre in TABLE_GENRES if genre in used)

    @classmethod
    def load(cls, data_dir: Path = DATA_DIR, *, validate_worlds: bool = True) -> ContentLibrary:
        """Load every world template and rule set. Each subdirectory is one of either.

        The `core` rule set is loaded on its own first, then merged under every other rule
        set. A definition in a rule set replaces the core definition with the same type and id.
        A world that names a rule set is checked against that rule set before it is returned."""
        worlds = {d.name: _load_world(d) for d in _subdirectories(data_dir / "worlds")}
        rule_set_dirs = _subdirectories(data_dir / "rulesets")
        core_dir = next((d for d in rule_set_dirs if d.name == CORE_RULE_SET), None)
        if core_dir is None:
            raise ContentError(f"missing the {CORE_RULE_SET!r} rule set")
        defaults = _load_defaults(data_dir)
        core_own, core_removed = _resolve_definitions(read_definitions(core_dir), {}, CORE_RULE_SET)
        core_capabilities = _infer_capabilities(core_own, permissive=True)
        core_definitions = _merge_defaults(
            core_own,
            defaults,
            CORE_RULE_SET,
            removed=core_removed,
            capabilities=core_capabilities,
            gate=False,
        )
        core = _build_rule_set(core_definitions, CORE_RULE_SET, capabilities=core_capabilities)
        rule_sets = {
            d.name: _load_rule_set(d, core_definitions, defaults)
            for d in rule_set_dirs
            if d.name != CORE_RULE_SET and _has_definitions(d)
        }
        configured = core.default_campaign
        if configured is not None:
            if configured.rule_set not in rule_sets:
                raise ContentError(
                    f"{CORE_RULE_SET}: default_campaign names unknown rule set "
                    f"{configured.rule_set!r}"
                )
            if configured.world_template not in worlds:
                raise ContentError(
                    f"{CORE_RULE_SET}: default_campaign names unknown world "
                    f"{configured.world_template!r}"
                )
            world_rule_set = worlds[configured.world_template].rule_set
            if world_rule_set is not None and world_rule_set != configured.rule_set:
                raise ContentError(
                    f"{CORE_RULE_SET}: default_campaign world "
                    f"{configured.world_template!r} is built for {world_rule_set!r}, not "
                    f"{configured.rule_set!r}"
                )
        for world in worlds.values():
            if world.rule_set is not None and world.rule_set not in rule_sets:
                raise ContentError(f"{world.id}: world names unknown rule set {world.rule_set!r}")
        library = cls(world_templates=worlds, rule_sets=rule_sets, core=core)
        if validate_worlds:
            for world in worlds.values():
                if world.rule_set is not None:
                    library.world_content(world.id, world.rule_set)
        return library

    def validate(self, *, all_pairs: bool = False) -> list[tuple[str, str]]:
        """Check every world against its declared rule set. With `all_pairs`, also try every
        other rule set and report those failures as warnings, so a portable world can still
        be loaded with a rule set it does not fully support. Returns (severity, message)."""
        issues: list[tuple[str, str]] = []
        for world in self.world_templates.values():
            declared = world.rule_set
            if declared is not None:
                try:
                    self.world_content(world.id, declared)
                except ContentError as exc:
                    issues.append(("error", f"{world.id} + {declared}: {exc}"))
            if not all_pairs:
                continue
            for rule_set in self.rule_sets:
                if rule_set == declared:
                    continue
                try:
                    self.world_content(world.id, rule_set)
                except ContentError as exc:
                    issues.append(("warning", f"{world.id} + {rule_set}: {exc}"))
        return issues


def _load_defaults(data_dir: Path) -> list[Definition]:
    """The optional defaults layer under `data/defaults`. A missing or empty directory is a
    no-op, so every older content tree loads exactly as before."""
    directory = data_dir / "defaults"
    if not directory.is_dir() or not _has_definitions(directory):
        return []
    return read_definitions(directory)


def _has_definitions(directory: Path) -> bool:
    """A rule set directory with no JSON yet, such as one holding only its source PDF, is skipped
    with a warning. Once it has JSON, it loads like any other."""
    if any(directory.rglob("*.json")):
        return True
    logger.warning("skipping rule set %s: no JSON files yet", directory.name)
    return False


def _subdirectories(path: Path) -> list[Path]:
    if not path.is_dir():
        raise ContentError(f"missing content directory {path}")
    return sorted(child for child in path.iterdir() if child.is_dir())


def _single(definitions: list[Definition], kind: str, template: str) -> Definition:
    matches = [d for d in definitions if d.type == kind]
    if len(matches) != 1:
        raise ContentError(
            f"{template}: expected exactly one {kind!r} object, found {len(matches)}"
        )
    return matches[0]


def _check_directory_id(template: str, ident: str) -> None:
    if ident != template:
        raise ContentError(f"{template}: id must match the directory name, got {ident!r}")


WORLD_TYPES = ("monster", "table", "dice_table", "creative_table", "feature", "rule")
# Every definition type a world loader consumes. A rule-set-only definition in a world directory
# is a content error instead of being silently dropped.
WORLD_DEFINITION_TYPES = frozenset({"world", "location", "npc", "region", "route", *WORLD_TYPES})
# Every definition type a rule set loader consumes. A definition with another type is a content
# error instead of being silently dropped.
RULE_SET_DEFINITION_TYPES = frozenset(
    {
        "rule_set",
        "python_module",
        "rule",
        "table",
        "stat",
        "skill",
        "character_field",
        "campaign_field",
        "feature",
        "feature_type",
        "template",
        "injury_table",
        "combat",
        "encounter_grid",
        "monster",
        "condition",
        "counter",
        "advancement",
        "level_up",
        "period",
        "tally",
        "relation",
        "move",
        "check",
        "rest",
        "clock",
        "housekeeping",
        "checklist",
        "item_size",
        "inventory",
        "magic",
        "marketplace",
        "shop_item",
        "service",
        "hireling",
        "hooks",
        "prompt",
        "narration",
        "travel",
        "journey",
        "wilderness",
        "encounter_terrain",
        "encounter_object",
        "encounter_template",
        "world_terrain",
        "project",
        "faction_asset",
        "rule_data",
        "tool_call",
        "dice_table",
        "creative_table",
    }
)
STATBLOCK_KEYS = ("stats", "armor", "attacks")


def _load_capabilities(value: object, label: str) -> Capabilities:
    """A rule set's `capabilities` block. The block is authoritative when present: omitted
    booleans are false and an omitted `social` list offers no social systems."""
    if not isinstance(value, Mapping):
        raise ContentError(f"{label}: 'capabilities' must be an object")
    check_keys(
        value,
        {"magic", "travel", "wilderness", "dungeon", "combat", "social"},
        f"{label}: capabilities",
    )
    magic = value.get("magic", "none")
    if magic not in MAGIC_CAPABILITIES:
        raise ContentError(
            f"{label}: capabilities 'magic' must be one of {', '.join(MAGIC_CAPABILITIES)}"
        )
    travel = value.get("travel", "none")
    if travel not in TRAVEL_CAPABILITIES:
        raise ContentError(
            f"{label}: capabilities 'travel' must be one of {', '.join(TRAVEL_CAPABILITIES)}"
        )
    wilderness = value.get("wilderness", False)
    dungeon = value.get("dungeon", False)
    combat = value.get("combat", False)
    for key, setting in (
        ("wilderness", wilderness),
        ("dungeon", dungeon),
        ("combat", combat),
    ):
        if not isinstance(setting, bool):
            raise ContentError(f"{label}: capabilities {key!r} must be true or false")
    raw_social = value.get("social", [])
    if not isinstance(raw_social, list) or not all(
        isinstance(item, str) and item for item in raw_social
    ):
        raise ContentError(f"{label}: capabilities 'social' must be a list of names")
    unknown = sorted(set(raw_social) - set(SOCIAL_CAPABILITIES))
    if unknown:
        raise ContentError(
            f"{label}: capabilities 'social' names unknown systems "
            f"{', '.join(repr(name) for name in unknown)}"
        )
    if len(set(raw_social)) != len(raw_social):
        raise ContentError(f"{label}: capabilities 'social' repeats a system")
    return Capabilities(
        magic=magic,
        travel=travel,
        wilderness=wilderness,
        dungeon=dungeon,
        combat=combat,
        social=tuple(raw_social),
    )


def _infer_capabilities(
    definitions: Iterable[Definition], *, permissive: bool = False
) -> Capabilities:
    """The capabilities a rule set's definitions imply, for a head with no `capabilities` block.

    The inference is only metadata for a rule set that declares no capabilities: inheritance is
    left ungated in that case, so old content loads exactly as it did. `permissive` fills the
    social list with every system, which core needs because its own neutral tools cover systems
    (factions) that core does not itself define."""
    defs = list(definitions)
    magic = "none"
    for definition in defs:
        if definition.type == "magic":
            model = definition.data.get("model", "item")
            magic = model if model in MAGIC_CAPABILITIES else "item"
            break
    travel = "none"
    journey = next((d for d in defs if d.type == "journey"), None)
    if journey is not None:
        model = journey.data.get("model", "terrain_days")
        travel = "watches" if model == "watches" else "terrain"
    elif any(d.type == "travel" for d in defs):
        travel = "terrain"
    elif any(d.type == "wilderness" for d in defs):
        travel = "watches"
    wilderness = any(d.type == "wilderness" for d in defs)
    dungeon = any(d.type == "rule_data" and d.id == "dungeon" for d in defs)
    combat = any(d.type == "combat" for d in defs)
    if permissive:
        social = SOCIAL_CAPABILITIES
        # Core authors the shared combat and travel tools before any rule set contributes
        # those definitions, so its inferred model must allow them.
        combat = True
        if travel == "none":
            travel = "terrain"
    else:
        present = {d.type for d in defs}
        social = tuple(
            name
            for name, kinds in (
                ("conditions", ("condition",)),
                ("counters", ("counter",)),
                ("checks", ("check",)),
                ("moves", ("move",)),
                ("relations", ("relation",)),
                ("tallies", ("tally",)),
                ("factions", ("faction_asset",)),
                ("projects", ("project",)),
                ("periods", ("period",)),
            )
            if present & set(kinds)
        )
    return Capabilities(
        magic=magic,
        travel=travel,
        wilderness=wilderness,
        dungeon=dungeon,
        combat=combat,
        social=tuple(social),
    )


def _declared_capabilities(own: Sequence[Definition], template: str) -> Capabilities | None:
    """The rule set head's capabilities, or None when it has no block. A block is authoritative:
    only a rule set that declares one has its inherited definitions gated."""
    head = _single(list(own), "rule_set", template)
    if "capabilities" not in head.data:
        return None
    return _load_capabilities(head.data["capabilities"], f"{template}: rule_set")


def _inherited_definition_enabled(
    definition: Definition, capabilities: Capabilities, template: str
) -> bool:
    """Whether an inherited core or default definition passes its own gate."""
    label = f"{template}: {definition.type} {definition.id}"
    requires, enabled_by = _read_gate(definition.data, label)
    return _definition_enabled(capabilities, requires, enabled_by)


def _read_gate(
    data: Mapping[str, Any], label: str
) -> tuple[tuple[str, ...], dict[str, tuple[Any, ...]]]:
    """Read a definition's `requires` and `enabled_by`, validating every capability path and
    value against the rule set's capability model."""
    raw_requires = data.get("requires", [])
    if isinstance(raw_requires, str):
        raw_requires = [raw_requires]
    if not isinstance(raw_requires, list) or not all(
        isinstance(item, str) and item for item in raw_requires
    ):
        raise ContentError(f"{label}: 'requires' must be a list of capability names")
    unknown = sorted(set(raw_requires) - CAPABILITY_PATHS)
    if unknown:
        raise ContentError(
            f"{label}: 'requires' names unknown capability paths {', '.join(unknown)}"
        )
    if len(set(raw_requires)) != len(raw_requires):
        raise ContentError(f"{label}: 'requires' repeats a capability")
    raw_enabled = data.get("enabled_by", {})
    if not isinstance(raw_enabled, Mapping):
        raise ContentError(f"{label}: 'enabled_by' must be an object")
    enabled: dict[str, tuple[Any, ...]] = {}
    for path, raw_values in raw_enabled.items():
        if path not in CAPABILITY_PATHS:
            raise ContentError(f"{label}: 'enabled_by' names unknown capability path {path!r}")
        if "." in path:
            raise ContentError(
                f"{label}: 'enabled_by' cannot name {path!r}; use 'requires' for social systems"
            )
        values = [raw_values] if not isinstance(raw_values, list) else raw_values
        if not values:
            raise ContentError(f"{label}: 'enabled_by' {path!r} must not be empty")
        if path == "magic":
            unknown_values = [value for value in values if value not in MAGIC_CAPABILITIES]
        elif path == "travel":
            unknown_values = [value for value in values if value not in TRAVEL_CAPABILITIES]
        elif path == "social":
            unknown_values = [value for value in values if value not in SOCIAL_CAPABILITIES]
        else:
            unknown_values = [value for value in values if not isinstance(value, bool)]
        if unknown_values:
            raise ContentError(
                f"{label}: 'enabled_by' {path!r} has unsupported value(s) "
                f"{', '.join(repr(value) for value in unknown_values)}"
            )
        enabled[path] = tuple(values)
    return tuple(raw_requires), enabled


def _definition_enabled(
    capabilities: Capabilities,
    requires: Sequence[str],
    enabled_by: Mapping[str, Sequence[Any]],
) -> bool:
    """Whether every `requires` path is truthy and every `enabled_by` path holds one of its
    values."""
    if any(not capabilities.supports(path) for path in requires):
        return False
    return all(capabilities.accepts(path, values) for path, values in enabled_by.items())


def _check_definition_gate(
    capabilities: Capabilities | None,
    requires: Sequence[str],
    enabled_by: Mapping[str, Sequence[Any]],
    label: str,
) -> None:
    """Check a definition authored by the rule set itself. Unlike an inherited definition, which
    is dropped when its gate is unmet, an authored definition that asks for an unsupported
    capability is a content error: the rule set would otherwise offer a tool it says it cannot
    use."""
    if capabilities is None:
        return
    if not _definition_enabled(capabilities, requires, enabled_by):
        raise ContentError(
            f"{label}: its requires/enabled_by gate is not met by the rule set's capabilities"
        )


def _assert_consumed(
    definitions: Iterable[Definition], allowed: frozenset[str], template: str
) -> None:
    """Every definition must have a type a loader consumes. Run after a build so a type the
    registry does not know about cannot be silently dropped."""
    unknown = sorted({d.type for d in definitions if d.type not in allowed})
    if unknown:
        raise ContentError(f"{template}: unknown definition type(s) {', '.join(unknown)}")


def _load_world(directory: Path) -> WorldTemplate:
    template = directory.name
    definitions = read_definitions(directory)
    _assert_consumed(definitions, WORLD_DEFINITION_TYPES, template)
    world = _single(definitions, "world", template)
    _check_directory_id(template, world.id)
    label = f"{template}: world"
    check_keys(
        world.data,
        {"type", "id", "name", "description", "rule_set", "blurb", "third_party", "start"},
        label,
    )
    location_icons = _icon_entries(definitions, "location", template)
    locations = tuple(
        _load_location(d, template, icon_entries=location_icons)
        for d in definitions
        if d.type == "location"
    )
    npc_definitions = [d for d in definitions if d.type == "npc"]
    npcs = tuple(_load_npc(d, template) for d in npc_definitions)
    statblocks = [_npc_statblock(d, template) for d in npc_definitions if _has_statblock(d.data)]
    own = [d for d in definitions if d.type in WORLD_TYPES]
    monster_ids = [d.id for d in own if d.type == "monster"]
    for statblock in statblocks:
        if statblock.id in monster_ids:
            raise ContentError(
                f"{template}: npc {statblock.id!r} has a statblock, so no monster may share its id"
            )
    location_ids = {location.id for location in locations}
    for npc in npcs:
        if npc.location not in location_ids:
            raise ContentError(
                f"{template}: npc {npc.id!r} is in unknown location {npc.location!r}"
            )
    _check_parents(locations, template)
    locations = _merge_world_routes(locations, _load_world_routes(definitions, template), template)
    _check_routes(locations, template)
    _check_unique_names([*locations, *npcs], template)
    regions = _load_regions(definitions, template)
    return WorldTemplate(
        id=world.id,
        name=required_text(world.data, "name", label),
        description=required_text(world.data, "description", label),
        rule_set=_optional_text(world.data, "rule_set", label),
        blurb=_optional_text(world.data, "blurb", label),
        third_party=_flag(world.data, "third_party", label),
        locations=locations,
        npcs=npcs,
        definitions=(*own, *statblocks),
        start=_load_world_start(world.data, label, location_ids),
        regions=regions,
    )


def _load_regions(definitions: list[Definition], template: str) -> tuple[RegionTemplate, ...]:
    regions: list[RegionTemplate] = []
    for d in definitions:
        if d.type != "region":
            continue
        label = f"{template}: region {d.id}"
        check_keys(
            d.data,
            {"type", "id", "name", "terrain", "points", "color", "pattern", "ink", "label"},
            label,
        )
        raw = d.data.get("points")
        if not isinstance(raw, list) or not all(
            isinstance(point, list)
            and len(point) == 2
            and all(
                not isinstance(value, bool) and isinstance(value, int | float) for value in point
            )
            for point in raw
        ):
            raise ContentError(
                f"{label}: points must be a list of [x, y] pairs of numbers, in kilometers from "
                "the map's center"
            )
        points = tuple((float(x), float(y)) for x, y in raw)
        problem = polygon_problem(points)
        if problem is not None:
            raise ContentError(f"{label}: {problem}")
        regions.append(
            RegionTemplate(
                id=d.id,
                name=required_text(d.data, "name", label),
                terrain=required_text(d.data, "terrain", label),
                points=points,
                color=_optional_color(d.data, "color", label) or None,
                pattern=_style_choice(d.data, "pattern", TERRAIN_PATTERNS, label) or None,
                ink=_optional_color(d.data, "ink", label) or None,
                label=_optional_text(d.data, "label", label),
            )
        )
    seen: set[str] = set()
    for region in regions:
        key = region.name.casefold()
        if key in seen:
            raise ContentError(f"{template}: duplicate region name {region.name!r}")
        seen.add(key)
    for index, region in enumerate(regions):
        for other in regions[:index]:
            if overlaps(region.points, other.points):
                raise ContentError(
                    f"{template}: region {region.name!r} overlaps region {other.name!r}"
                )
    return tuple(regions)


def _load_world_start(
    data: Mapping[str, Any], label: str, location_ids: set[str]
) -> WorldStart | None:
    """The optional `start` object: a known location and the hooks that bring the party there."""
    if "start" not in data:
        return None
    start_label = f"{label}: start"
    start = check_object_keys(data["start"], {"location", "hooks", "season"}, start_label)
    location = required_text(start, "location", start_label)
    if location not in location_ids:
        raise ContentError(f"{start_label} names unknown location {location!r}")
    season = None
    if "season" in start:
        season = required_text(start, "season", start_label)
    return WorldStart(
        location=location, hooks=required_text(start, "hooks", start_label), season=season
    )


def _has_statblock(data: Mapping[str, Any]) -> bool:
    return any(key in data for key in STATBLOCK_KEYS)


def _npc_statblock(definition: Definition, template: str) -> Definition:
    """An NPC's own statblock, as a monster with the NPC's id. Monster fields come from the NPC."""
    fields = ("name", "description", *STATBLOCK_KEYS, "attacks_per_action")
    data = {key: definition.data[key] for key in fields if key in definition.data}
    return Definition(
        type="monster", id=definition.id, data={"type": "monster", "id": definition.id, **data}
    )


def _build_world_content(template: WorldTemplate, rule_set: RuleSet) -> WorldContent:
    name = template.id
    own = template.definitions
    _check_character_definitions(own, name)
    for definition in own:
        if definition.type == "rule":
            check_keys(
                definition.data,
                {"type", "id", "text"},
                f"{name}: rule {definition.id}",
            )
    monsters = {
        d.id: _load_monster(
            d,
            rule_set.character,
            name,
            pool=_hit_pool(rule_set.combat),
            conditions=rule_set.conditions,
            icon_entries=_icon_entries(own, "monster", name),
        )
        for d in own
        if d.type == "monster"
    }
    for npc in template.npcs:
        if (
            npc.monster is not None
            and npc.monster not in monsters
            and npc.monster not in rule_set.monsters
        ):
            raise ContentError(
                f"{name}: npc {npc.id!r} has no monster {npc.monster!r} in the world or the "
                f"{rule_set.id!r} rule set"
            )
        if npc.hp_tier is not None and npc.hp_tier not in rule_set.npc_hp:
            raise ContentError(
                f"{name}: npc {npc.id!r} has hp_tier {npc.hp_tier!r}, which the "
                f"{rule_set.id!r} rule set does not define"
            )
    feature_icons = _icon_entries(own, "feature", name)
    features = {
        d.id: load_feature(
            d,
            name,
            stats=rule_set.character.stats,
            skills=rule_set.character.skills,
            counters=rule_set.counters,
            icon_entries=feature_icons,
            feature_types=rule_set.character.feature_types,
        )
        for d in own
        if d.type == "feature"
    }
    raw_rules = {
        d.id: required_text(d.data, "text", f"{name}: rule {d.id}") for d in own if d.type == "rule"
    }
    for region in template.regions:
        if region.terrain not in rule_set.world_terrain:
            raise ContentError(
                f"{name}: region {region.name!r} has terrain {region.terrain!r}, which the "
                f"{rule_set.id!r} rule set does not define"
            )
    for spot in template.locations:
        _check_authored_routes(spot, rule_set, name)
        if spot.map is not None and spot.map not in rule_set.encounter_templates:
            raise ContentError(
                f"{name}: location {spot.id!r} names local map template {spot.map!r}, which the "
                f"{rule_set.id!r} rule set does not define"
            )
    written = _attached_rules(monsters.values(), name)
    shared = sorted(written.keys() & raw_rules.keys())
    if shared:
        raise ContentError(f"{name}: rules {shared} are both world rules and statblock rules")
    rules = {**raw_rules, **written}
    known = {**rule_set.rules, **rule_set.attached_rules, **rules}
    attached = [(f"monster {m.id!r}", m.rules) for m in monsters.values()]
    attached += [(f"npc {npc.id!r}", npc.rules) for npc in template.npcs]
    attached += [(f"location {spot.id!r}", spot.rules) for spot in template.locations]
    attached += [(f"feature {f.id!r}", f.rules) for f in features.values()]
    for owner, ids in attached:
        for rule_id in ids:
            if rule_id not in known:
                raise ContentError(
                    f"{name}: {owner} attaches rule {rule_id!r}, which is not in the world or "
                    f"the {rule_set.id!r} rule set"
                )
    attached_ids = {rule_id for _, ids in attached for rule_id in ids}
    unattached_rules = {rid: text for rid, text in raw_rules.items() if rid not in attached_ids}
    tables = {d.id: _load_table(d, name) for d in own if d.type == "table"}
    dice_tables = _load_dice_tables(own, name)
    for table_id, options in tables.items():
        if table_id in dice_tables:
            continue
        dice_tables[table_id] = DiceTable(
            id=table_id,
            name=table_id.replace("-", " ").title(),
            dice=f"1d{len(options)}",
            rows=tuple(
                DiceRow(
                    low=idx,
                    high=idx,
                    result=opt.description
                    if opt.name is None
                    else f"{opt.name}: {opt.description}",
                )
                for idx, opt in enumerate(options, 1)
            ),
        )
    return WorldContent(
        monsters=monsters,
        tables=tables,
        features=features,
        rules=rules,
        unattached_rules=unattached_rules,
        dice_tables=dice_tables,
    )


def _load_dice_tables(definitions: Sequence[Definition], template: str) -> dict[str, DiceTable]:
    """The dice tables among the definitions, by id, including creative tables. An id may be
    defined only once."""
    dice_tables: dict[str, DiceTable] = {}
    for d in definitions:
        if d.type == "dice_table":
            table = _load_dice_table(d, template)
        elif d.type == "creative_table":
            table = _load_creative_table(d, template)
        else:
            continue
        if d.id in dice_tables:
            raise ContentError(f"{template}: duplicate dice table {d.id!r}")
        dice_tables[d.id] = table
    return dice_tables


def _load_creative_table(definition: Definition, template: str) -> DiceTable:
    """A creative table is a list of `entries`, one per roll, rolled as 1dN. The AI GM rolls on
    these when it needs an idea. A rule set changes a core table with `add` and `remove`, which
    `_patch_creative_table` applies before this runs. `source` credits where the entries came
    from and is checked here but not shown to the GM."""
    label = f"{template}: creative table {definition.id}"
    extra = set(definition.data) - {"type", "id", "name", "entries", "source", "genres"}
    if extra:
        raise ContentError(f"{label}: unknown keys {sorted(extra)}")
    if "source" in definition.data:
        _text_list([definition.data["source"]], f"{label}: 'source'")
    name = _table_label(definition, template)
    entries = _creative_entries(definition.data.get("entries"), label)
    genres = read_table_genres(definition.data.get("genres"), f"{label}: 'genres'")
    return DiceTable(
        id=definition.id,
        name=name,
        dice=f"1d{len(entries)}",
        rows=tuple(DiceRow(index, index, text) for index, text in enumerate(entries, 1)),
        creative=True,
        genres=genres,
    )


def _creative_entries(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ContentError(f"{label}: 'entries' must be a non-empty list of text")
    _text_list(value, f"{label}: 'entries'")
    if len(set(value)) != len(value):
        raise ContentError(f"{label}: 'entries' repeats an entry")
    return value


def _text_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ContentError(f"{label} must be a list of non-empty text")
    return value


def _patch_creative_table(
    definition: Definition, core: Mapping[str, Definition], template: str
) -> Definition:
    """A rule set's creative table with `add` and `remove` changes the core table of the same id:
    the core entries, less `remove`, then `add` appended. Every removed entry must exist, and no
    added entry may already be there. A rule set table with `entries` replaces the core table
    outright, as any definition does. Genre tags follow the same rule: the patch's own `genres`
    win, and a patch without them keeps the core table's tags."""
    label = f"{template}: creative table {definition.id}"
    if not {"add", "remove"} & set(definition.data):
        return definition
    extra = set(definition.data) - {"type", "id", "name", "add", "remove", "source", "genres"}
    if extra:
        raise ContentError(f"{label}: unknown keys {sorted(extra)} with 'add' or 'remove'")
    base = core.get(definition.id)
    if base is None:
        raise ContentError(f"{label}: 'add' and 'remove' need a core creative table of this id")
    entries = list(
        _creative_entries(base.data.get("entries"), f"core creative table {definition.id}")
    )
    for entry in _text_list(definition.data.get("remove", []), f"{label}: 'remove'"):
        if entry not in entries:
            raise ContentError(f"{label}: 'remove' names {entry!r}, which the core table lacks")
        entries.remove(entry)
    for entry in _text_list(definition.data.get("add", []), f"{label}: 'add'"):
        if entry in entries:
            raise ContentError(f"{label}: 'add' repeats {entry!r}, which the table already has")
        entries.append(entry)
    data = {
        "type": "creative_table",
        "id": definition.id,
        "name": definition.data.get("name", base.data.get("name", definition.id)),
        "entries": entries,
    }
    if "source" in definition.data or "source" in base.data:
        data["source"] = definition.data.get("source", base.data.get("source"))
    if "genres" in definition.data or "genres" in base.data:
        data["genres"] = definition.data.get("genres", base.data.get("genres"))
    return Definition(type="creative_table", id=definition.id, data=data)


def _check_authored_routes(spot: LocationTemplate, rule_set: RuleSet, template: str) -> None:
    """Each authored route must use a path, distance, and terrain the rule set's travel defines."""
    travel = rule_set.wilderness.travel if rule_set.wilderness is not None else None
    if travel is None:
        return
    for destination, route in spot.routes.items():
        label = f"{template}: location {spot.id} route to {destination!r}"
        if route["path"] not in travel.path:
            raise ContentError(f"{label}: path {route['path']!r} is not in the rule set's travel")
        if route["distance"] not in travel.distance:
            raise ContentError(
                f"{label}: distance {route['distance']!r} is not in the rule set's travel"
            )
        if route["terrain"] not in travel.terrain:
            raise ContentError(
                f"{label}: terrain {route['terrain']!r} is not in the rule set's travel"
            )
        vast = route.get("vast", 0)
        if vast > travel.vast_max:
            raise ContentError(
                f"{label}: vast {vast} is more than the rule set's {travel.vast_max}"
            )


def _load_location(
    definition: Definition,
    template: str,
    *,
    icon_entries: Mapping[str, tuple[str | None, str | None]] | None = None,
) -> LocationTemplate:
    label = f"{template}: location {definition.id}"
    data = definition.data
    check_keys(
        data,
        {
            "type",
            "id",
            "name",
            "description",
            "discovered",
            "x",
            "y",
            "parent",
            "icon",
            "summary",
            "looks_like",
            "rules",
            "routes",
            "map",
        },
        label,
    )
    discovered = data.get("discovered", False)
    if not isinstance(discovered, bool):
        raise ContentError(f"{label}: discovered must be true or false")
    x, y = data.get("x"), data.get("y")
    if (x is None) != (y is None):
        raise ContentError(f"{label}: give both x and y, or neither")
    for key, value in (("x", x), ("y", y)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int | float)):
            raise ContentError(
                f"{label}: {key} must be a number of kilometers from the map's center"
            )
    icon, looks_like = _icon_fields(data, icon_entries or {}, FALLBACK_PLACE, label)
    return LocationTemplate(
        id=definition.id,
        name=required_text(data, "name", label),
        description=required_text(data, "description", label),
        discovered=discovered,
        x=None if x is None else float(x),
        y=None if y is None else float(y),
        parent=_optional_text(data, "parent", label),
        icon=icon,
        summary=_optional_text(data, "summary", label),
        rules=rule_ids(data, label),
        routes=_load_routes(data, label),
        looks_like=looks_like,
        map=_optional_text(data, "map", label),
    )


def _load_routes(data: Mapping[str, Any], label: str) -> dict[str, dict[str, Any]]:
    """The optional 'routes' of a location: destination location id to an authored overland route."""
    if "routes" not in data:
        return {}
    raw = data["routes"]
    if not isinstance(raw, Mapping):
        raise ContentError(f"{label}: 'routes' must map destination location ids to route objects")
    routes: dict[str, dict[str, Any]] = {}
    for destination, route in raw.items():
        route_label = f"{label}: route to {destination!r}"
        if not isinstance(route, Mapping):
            raise ContentError(f"{route_label} must be an object")
        unexpected = set(route) - ROUTE_KEYS
        if unexpected:
            raise ContentError(f"{route_label} has unknown keys {sorted(unexpected)}")
        loaded: dict[str, Any] = {
            key: required_text(route, key, route_label) for key in ("path", "distance", "terrain")
        }
        if "vast" in route:
            loaded["vast"] = _whole(route, "vast", route_label, 0)
        routes[destination] = loaded
    return routes


@dataclass(frozen=True)
class _WorldRoute:
    """A world-level route before it is merged into each location's route map."""

    id: str
    origin: str
    destination: str
    route: dict[str, Any]
    one_way: bool


def _load_world_routes(definitions: list[Definition], template: str) -> tuple[_WorldRoute, ...]:
    """The world's top-level route definitions. Each connects two locations in one place instead
    of restating the same route on both locations."""
    routes: list[_WorldRoute] = []
    for definition in definitions:
        if definition.type != "route":
            continue
        label = f"{template}: route {definition.id}"
        data = definition.data
        check_keys(data, WORLD_ROUTE_KEYS, label)
        origin = required_text(data, "from", label)
        destination = required_text(data, "to", label)
        if origin == destination:
            raise ContentError(f"{label}: cannot lead to itself")
        route = {key: required_text(data, key, label) for key in ("path", "distance", "terrain")}
        if "vast" in data:
            route["vast"] = _whole(data, "vast", label, 0)
        one_way = data.get("one_way", False)
        if not isinstance(one_way, bool):
            raise ContentError(f"{label}: one_way must be true or false")
        routes.append(
            _WorldRoute(
                id=definition.id,
                origin=origin,
                destination=destination,
                route=route,
                one_way=one_way,
            )
        )
    return tuple(routes)


def _merge_world_routes(
    locations: tuple[LocationTemplate, ...],
    routes: tuple[_WorldRoute, ...],
    template: str,
) -> tuple[LocationTemplate, ...]:
    """Add each top-level route to both locations' route maps. A route already authored on a
    location is an error, whether it repeats the same values or conflicts with them, so the two
    places cannot silently disagree about one road."""
    location_ids = {location.id for location in locations}
    merged = {location.id: dict(location.routes) for location in locations}
    authored: set[tuple[str, str]] = set()
    for world_route in routes:
        label = f"{template}: route {world_route.id}"
        for endpoint, key in (
            (world_route.origin, "from"),
            (world_route.destination, "to"),
        ):
            if endpoint not in location_ids:
                raise ContentError(f"{label}: {key} {endpoint!r} is not a location in this world")
        directions = [(world_route.origin, world_route.destination)]
        if not world_route.one_way:
            directions.append((world_route.destination, world_route.origin))
        for origin, destination in directions:
            pair = (origin, destination)
            if pair in authored:
                raise ContentError(f"{label}: duplicate route from {origin!r} to {destination!r}")
            authored.add(pair)
            existing = merged[origin].get(destination)
            if existing is not None:
                if existing == world_route.route:
                    raise ContentError(
                        f"{label}: location {origin!r} already authors the same route to "
                        f"{destination!r}"
                    )
                raise ContentError(
                    f"{label}: location {origin!r} already authors a different route to "
                    f"{destination!r}"
                )
            merged[origin][destination] = dict(world_route.route)
    return tuple(replace(location, routes=merged[location.id]) for location in locations)


def _check_routes(locations: tuple[LocationTemplate, ...], template: str) -> None:
    """An authored route must lead to another location in this world."""
    location_ids = {location.id for location in locations}
    for location in locations:
        for destination in location.routes:
            label = f"{template}: location {location.id} route to {destination!r}"
            if destination == location.id:
                raise ContentError(f"{label}: cannot lead to itself")
            if destination not in location_ids:
                raise ContentError(f"{label}: {destination!r} is not a location in this world")


def _check_parents(locations: tuple[LocationTemplate, ...], template: str) -> None:
    """A place can be inside another place, but only one level deep, and only a place at the top
    level has a map position."""
    by_id = {location.id: location for location in locations}
    for location in locations:
        if location.parent is None:
            continue
        label = f"{template}: location {location.id}"
        if location.parent == location.id:
            raise ContentError(f"{label}: cannot be inside itself")
        outer = by_id.get(location.parent)
        if outer is None:
            raise ContentError(
                f"{label}: parent {location.parent!r} is not a location in this world"
            )
        if outer.parent is not None:
            raise ContentError(
                f"{label}: parent {location.parent!r} is itself inside {outer.parent!r}"
            )
        if location.x is not None:
            raise ContentError(f"{label}: a place inside another place has no map position")


def _load_npc(definition: Definition, template: str) -> NpcTemplate:
    label = f"{template}: npc {definition.id}"
    data = definition.data
    check_keys(
        data,
        {
            "type",
            "id",
            "name",
            "location",
            "description",
            "personality",
            "accent",
            "quirks",
            "monster",
            "story_only",
            "rules",
            "hp_tier",
            "attacks_per_action",
            *STATBLOCK_KEYS,
        },
        label,
    )
    quirks = data.get("quirks")
    if quirks is not None:
        if not isinstance(quirks, list) or not all(
            isinstance(q, str) and q.strip() for q in quirks
        ):
            raise ContentError(f"{label}: 'quirks' must be a list of non-empty text")
        quirks = tuple(quirks)
    if _has_statblock(data) and "monster" in data:
        raise ContentError(f"{label}: give a 'monster' id or a statblock, not both")
    monster = definition.id if _has_statblock(data) else _optional_text(data, "monster", label)
    story_only = _flag(data, "story_only", label)
    if story_only and monster is not None:
        raise ContentError(f"{label}: a story-only NPC has no statblock or monster")
    hp_tier = _optional_text(data, "hp_tier", label)
    if hp_tier is not None and monster is not None:
        raise ContentError(f"{label}: an NPC with a statblock has its own HP, not an 'hp_tier'")
    if hp_tier is not None and story_only:
        raise ContentError(f"{label}: a story-only NPC has no HP to set")
    return NpcTemplate(
        id=definition.id,
        name=required_text(data, "name", label),
        location=required_text(data, "location", label),
        description=required_text(data, "description", label),
        personality=_optional_text(data, "personality", label),
        accent=_optional_text(data, "accent", label),
        quirks=quirks,
        monster=monster,
        story_only=story_only,
        rules=rule_ids(data, label),
        hp_tier=hp_tier,
    )


def _optional_text(data: Mapping[str, Any], key: str, label: str) -> str | None:
    if data.get(key) is None:
        return None
    return required_text(data, key, label)


def _check_unique_names(entries: list[LocationTemplate | NpcTemplate], template: str) -> None:
    """Locations and NPCs share one namespace, so the GM can refer to either by name."""
    seen: set[str] = set()
    for entry in entries:
        key = entry.name.casefold()
        if key in seen:
            raise ContentError(f"{template}: duplicate name {entry.name!r}")
        seen.add(key)


# The keys the character loaders in content/character.py read. Checking them here keeps every
# definition type strict even though those loaders are not part of this module's `_load_*` set.
CHARACTER_DEFINITION_KEYS: Mapping[str, frozenset[str]] = {
    "stat": frozenset(
        {
            "type",
            "id",
            "name",
            "derived",
            "swap_group",
            "modifier",
            "plus_modifier",
            "minimum",
            "values",
            "formula",
        }
    ),
    "skill": frozenset({"type", "id", "name", "description"}),
    "feature": frozenset(
        {
            "type",
            "id",
            "name",
            "feature_type",
            "description",
            "size",
            "uses",
            "armor",
            "damage",
            "rules",
            "tag",
            "rank",
            "skill",
            "attributes",
            "shock",
            "shock_ac",
            "flags",
            "encumbrance_checks",
            "effects",
            "reactions",
            "defense_floor",
            "picks",
            "repeatable",
            "level_hp_bonus",
            "icon",
            "looks_like",
        }
    ),
    "character_field": frozenset(
        {
            "type",
            "id",
            "name",
            "description",
            "table",
            "group_roll",
            "restricted_by",
            "knows",
        }
    ),
    "campaign_field": frozenset(
        {
            "type",
            "id",
            "name",
            "description",
            "table",
            "group_roll",
            "restricted_by",
            "knows",
        }
    ),
    "template": frozenset(
        {
            "type",
            "id",
            "kind",
            "name",
            "description",
            "features",
            "tables",
            "names",
            "stats",
            "counters",
            "choose",
            "skills",
            "skill_picks",
            "feat_picks",
            "compatible_with",
            "template_requirements",
            "choice_key",
            "grant",
            "learning",
        }
    ),
}

# The keys each feature effect op reads. Unknown ops are rejected by content/character.py.
FEATURE_EFFECT_KEYS: Mapping[str, frozenset[str]] = {
    "note": frozenset({"op", "text"}),
    "armor": frozenset({"op", "delta"}),
    "stat": frozenset({"op", "id", "delta", "cap"}),
    "skill": frozenset({"op", "id", "delta"}),
    "counter": frozenset({"op", "id", "delta"}),
}
DEFENSE_FLOOR_KEYS = frozenset(
    {
        "base",
        "counter",
        "divisor",
        "round_up",
        "requires_no_armor",
        "requires_no_shield",
    }
)
CHOOSE_KEYS = frozenset({"count", "feature_type", "tag_field", "skip_tags"})
SKILL_PICK_KEYS = frozenset({"id", "count", "table", "exclude", "label", "max_level"})
FEAT_PICK_KEYS = frozenset(
    {
        "id",
        "count",
        "tag",
        "tags",
        "tags_from",
        "unique",
        "feature_type",
        "max_rank",
        "exclude",
        "advance_repeated_rank",
    }
)
GRANTED_KEYS = frozenset({"feature", "quantity"})
TEMPLATE_GRANT_KEYS = frozenset({"items", "silver", "equip"})


def _check_character_definitions(definitions: Iterable[Definition], template: str) -> None:
    """Reject unknown keys in the definitions content/character.py loads."""
    for definition in definitions:
        allowed = CHARACTER_DEFINITION_KEYS.get(definition.type)
        if allowed is None:
            continue
        label = f"{template}: {definition.type} {definition.id}"
        check_keys(definition.data, allowed, label)
        if definition.type == "feature":
            _check_feature_keys(definition.data, label)
        elif definition.type == "template":
            _check_template_keys(definition.data, label)


def _check_feature_keys(data: Mapping[str, Any], label: str) -> None:
    effects = data.get("effects", [])
    if isinstance(effects, list):
        for index, op in enumerate(effects):
            if not isinstance(op, Mapping):
                continue
            name = op.get("op")
            allowed = FEATURE_EFFECT_KEYS.get(name) if isinstance(name, str) else None
            if allowed is not None:
                check_keys(op, allowed, f"{label}: effect {index}")
    defense_floor = data.get("defense_floor")
    if isinstance(defense_floor, Mapping):
        check_keys(defense_floor, DEFENSE_FLOOR_KEYS, f"{label}: defense_floor")


def _check_template_keys(data: Mapping[str, Any], label: str) -> None:
    grant = data.get("grant")
    if isinstance(grant, Mapping):
        check_keys(grant, TEMPLATE_GRANT_KEYS, f"{label}: grant")
    choose = data.get("choose")
    if isinstance(choose, Mapping):
        check_keys(choose, CHOOSE_KEYS, f"{label}: choose")
    for key, allowed in (("skill_picks", SKILL_PICK_KEYS), ("feat_picks", FEAT_PICK_KEYS)):
        picks = data.get(key, [])
        if not isinstance(picks, list):
            continue
        for index, pick in enumerate(picks):
            if isinstance(pick, Mapping):
                check_keys(pick, allowed, f"{label}: {key} {index}")
    grants = data.get("features", [])
    if not isinstance(grants, list):
        return
    for index, grant in enumerate(grants):
        if isinstance(grant, Mapping):
            check_keys(grant, GRANTED_KEYS, f"{label}: feature grant {index}")


def _derive_background_tables(definitions: list[Definition], template: str) -> list[Definition]:
    """Derive each background's learning table and the background index from its template.

    A background template may list the skills it teaches in `learning`. The loader makes the
    `learning_<id>` table from that list and, when the content does not define one, a
    `background_index` describing every background. An authored table of either id wins over
    the derived one."""
    skill_ids = {d.id for d in definitions if d.type == "skill"}
    backgrounds: list[tuple[Definition, str, tuple[str, ...]]] = []
    for definition in definitions:
        if definition.type != "template" or "learning" not in definition.data:
            continue
        label = f"{template}: template {definition.id}"
        if definition.data.get("kind") != "background":
            raise ContentError(f"{label}: 'learning' is only for background templates")
        options = definition.data["learning"]
        if (
            not isinstance(options, list)
            or not options
            or not all(isinstance(option, str) and option.strip() for option in options)
        ):
            raise ContentError(f"{label}: 'learning' must be a non-empty list of skill ids")
        if len(set(options)) != len(options):
            raise ContentError(f"{label}: 'learning' repeats a skill id")
        for skill_id in options:
            if skill_id not in skill_ids:
                raise ContentError(f"{label}: 'learning' names no skill ({skill_id!r})")
        free = definition.data.get("skills")
        if not isinstance(free, Mapping) or len(free) != 1:
            raise ContentError(
                f"{label}: a background with 'learning' needs exactly one free skill in 'skills'"
            )
        backgrounds.append((definition, next(iter(free)), tuple(options)))
    if not backgrounds:
        return definitions
    existing = {d.id for d in definitions if d.type == "table"}
    learning: dict[str, Definition] = {}
    for definition, _, options in backgrounds:
        table_id = f"learning_{definition.id}"
        if table_id not in existing:
            learning[definition.id] = Definition(
                type="table",
                id=table_id,
                data={"type": "table", "id": table_id, "options": list(options)},
            )
    index = None
    if "background_index" not in existing:
        entries = []
        for definition, free, options in backgrounds:
            name = required_text(definition.data, "name", f"{template}: template {definition.id}")
            quick = ", ".join(option for option in options if option != free)
            entries.append(
                {
                    "name": name,
                    "description": f"Free skill: {free}. Quick skills: {quick}.",
                }
            )
        index = Definition(
            type="table",
            id="background_index",
            data={"type": "table", "id": "background_index", "options": entries},
        )
    if not learning and index is None:
        return definitions
    # Keep the loaded table order of content that authored these tables explicitly: the index
    # came before the first background Growth table, and each learning table followed its own.
    anchors = {f"growth_{definition.id}" for definition, _, _ in backgrounds}
    result: list[Definition] = []
    pending = dict(learning)
    for definition in definitions:
        if index is not None and definition.type == "table" and definition.id in anchors:
            result.append(index)
            index = None
        result.append(definition)
        if definition.type != "table":
            continue
        for background_id, table in list(pending.items()):
            if definition.id == f"growth_{background_id}":
                result.append(table)
                del pending[background_id]
    if index is not None:
        result.append(index)
    result.extend(pending.values())
    return result


def _rule_set_table_genres(definitions: Sequence[Definition], template: str) -> tuple[str, ...]:
    """The genres a rule set offers. Its `rule_set.table_genres` decides when set; otherwise the
    rule set takes `generic` plus every genre its own tables use, so its own tables always fit."""
    head = _single(list(definitions), "rule_set", template)
    if "table_genres" in head.data:
        return read_table_genres(head.data["table_genres"], f"{template}: rule_set: 'table_genres'")
    used = {GENERIC_GENRE}
    for definition in definitions:
        if definition.type in ("dice_table", "creative_table"):
            used.update(_definition_genres(definition, template))
    return tuple(genre for genre in TABLE_GENRES if genre in used)


# Definition keys a rule-set file may use to shape the resolved definition. They never reach a
# loader.
META_KEYS = frozenset({"extends", "patch", "remove", "disabled"})
# The definition types the optional defaults layer understands.
DEFAULT_DEFINITION_TYPES = frozenset({"clock", "rest", "world_terrain", "prompt", "tool_call"})


def _strip_meta(data: Mapping[str, Any], kind: str = "") -> dict[str, Any]:
    """A copy of a definition's data without the meta keys. A creative table keeps its own
    `add`/`remove` fields, which patch its entries rather than removing the definition."""
    meta = META_KEYS - ({"remove"} if kind == "creative_table" else set())
    return {key: value for key, value in data.items() if key not in meta}


def _overlay_data(base: Mapping[str, Any], own: Mapping[str, Any], label: str) -> dict[str, Any]:
    """Overlay one definition on another: a nested object merges key by key, and every other
    value, including a list, replaces the base value."""
    result = {key: copy.deepcopy(value) for key, value in base.items()}
    for key, value in own.items():
        if key in {"type", "id"} or key in META_KEYS:
            continue
        current = result.get(key)
        if isinstance(value, Mapping) and isinstance(current, Mapping):
            result[key] = _overlay_data(current, value, label)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _patched_data(
    base: Mapping[str, Any], patch: Mapping[str, Any], label: str, path: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Apply a `patch` mapping to resolved definition data. A nested object merges key by key;
    a list or scalar replaces the value. Every patched path must already exist, so a typo fails
    at load time instead of being added silently."""
    result = {key: copy.deepcopy(value) for key, value in base.items()}
    for key, value in patch.items():
        where = ".".join((*path, str(key)))
        if key in {"type", "id"}:
            raise ContentError(f"{label}: 'patch' cannot change {key!r}")
        if key in META_KEYS:
            raise ContentError(f"{label}: 'patch' cannot set the meta key {key!r}")
        if key not in result:
            raise ContentError(
                f"{label}: 'patch' names {where!r}, which the target does not define"
            )
        current = result[key]
        if isinstance(value, Mapping):
            if not isinstance(current, Mapping):
                raise ContentError(
                    f"{label}: 'patch' {where!r} is an object, but the target value is not"
                )
            result[key] = _patched_data(current, value, label, (*path, str(key)))
        else:
            result[key] = copy.deepcopy(value)
    return result


def _parse_extends(
    value: object,
    own_by_key: Mapping[tuple[str, str], Definition],
    base_by_key: Mapping[tuple[str, str], Definition],
    label: str,
) -> tuple[bool, tuple[str, str]]:
    """Resolve an `extends` target. The result is `(from_own, key)`: true for another definition
    in the same rule set, false for an inherited or default definition. `"core:table/quirk"` and
    `{"rule_set": "core", "type": "table", "id": "quirk"}` both name core explicitly."""
    rule_set: str | None = None
    if isinstance(value, str):
        source, colon, rest = value.partition(":")
        if colon:
            rule_set, target = source, rest
        else:
            target = value
        kind, slash, ident = target.partition("/")
        if not slash or not kind or not ident:
            raise ContentError(
                f"{label}: 'extends' must be 'core:type/id' or 'type/id', not {value!r}"
            )
        key = (kind, ident)
    elif isinstance(value, Mapping):
        check_keys(value, {"rule_set", "type", "id"}, f"{label}: extends")
        rule_set = _optional_text(value, "rule_set", f"{label}: extends")
        key = (
            required_text(value, "type", f"{label}: extends"),
            required_text(value, "id", f"{label}: extends"),
        )
    else:
        raise ContentError(f"{label}: 'extends' must be a string or an object")
    if rule_set is not None and rule_set != CORE_RULE_SET:
        raise ContentError(f"{label}: 'extends' names unknown rule set {rule_set!r}")
    if rule_set is None and key in own_by_key:
        return True, key
    if key in base_by_key:
        return False, key
    if rule_set is not None:
        raise ContentError(
            f"{label}: 'extends' names {key[0]} {key[1]!r}, which {CORE_RULE_SET} does not define"
        )
    raise ContentError(f"{label}: 'extends' names {key[0]} {key[1]!r}, which no definition defines")


def _resolve_definitions(
    own: list[Definition],
    base_by_key: Mapping[tuple[str, str], Definition],
    template: str,
) -> tuple[list[Definition], frozenset[tuple[str, str]]]:
    """Resolve `extends`, `patch`, `remove`, and `disabled` before core inheritance.

    A `remove` or `disabled` stub drops the inherited definition with the same type and id, so
    inheritance must also forget the key. The returned set holds those keys."""
    own_by_key = {(d.type, d.id): d for d in own}
    resolved: dict[tuple[str, str], Definition] = {}
    removed: set[tuple[str, str]] = set()
    resolving: list[tuple[str, str]] = []

    def resolve(key: tuple[str, str]) -> Definition | None:
        if key in resolved:
            return resolved[key]
        if key in removed:
            return None
        if key in resolving:
            chain = " -> ".join(f"{kind}/{ident}" for kind, ident in (*resolving, key))
            raise ContentError(f"{template}: 'extends' cycle: {chain}")
        definition = own_by_key[key]
        label = f"{template}: {definition.type} {definition.id}"
        data = dict(definition.data)
        disabled = data.pop("disabled", False)
        remove = data.pop("remove", False) if definition.type != "creative_table" else False
        for flag_name, flag in (("disabled", disabled), ("remove", remove)):
            if not isinstance(flag, bool):
                raise ContentError(f"{label}: {flag_name!r} must be true or false")
        if disabled or remove:
            extra = set(data) - {"type", "id"}
            if extra:
                names = ", ".join(repr(name) for name in sorted(extra))
                raise ContentError(
                    f"{label}: a definition marked {flag_name!r} may carry only type and id; "
                    f"it also has {names}"
                )
            if key not in base_by_key:
                raise ContentError(
                    f"{label}: marked {flag_name!r}, but no inherited definition has that "
                    "type and id"
                )
            removed.add(key)
            return None
        extends = data.pop("extends", None)
        patch = data.pop("patch", None)
        if patch is not None and not isinstance(patch, Mapping):
            raise ContentError(f"{label}: 'patch' must be an object")
        resolving.append(key)
        try:
            if extends is not None:
                from_own, base_key = _parse_extends(extends, own_by_key, base_by_key, label)
                base_definition = resolve(base_key) if from_own else base_by_key[base_key]
                if base_definition is None:
                    raise ContentError(
                        f"{label}: 'extends' names {base_key[0]} {base_key[1]!r}, which is "
                        "removed or disabled"
                    )
                data = _overlay_data(
                    _strip_meta(base_definition.data, base_definition.type), data, label
                )
            if patch is not None:
                data = _patched_data(data, patch, label)
        finally:
            resolving.pop()
        data["type"], data["id"] = definition.type, definition.id
        result = Definition(type=definition.type, id=definition.id, data=data)
        resolved[key] = result
        return result

    order: list[Definition] = []
    for definition in own:
        result = resolve((definition.type, definition.id))
        if result is not None:
            order.append(result)
    return order, frozenset(removed)


def _merge_defaults(
    own: list[Definition],
    defaults: Sequence[Definition],
    template: str,
    removed: frozenset[tuple[str, str]] = frozenset(),
    capabilities: Capabilities | None = None,
    gate: bool = False,
) -> list[Definition]:
    """Layer the optional defaults over a rule set's own definitions.

    A clock or rest default only fills in fields of the rule set's single definition of that
    type; a terrain default only supplies the looks of the same terrain id; a prompt or tool
    default is added only when the rule set does not define that id. A missing defaults layer is
    a no-op."""
    merged = list(own)
    index = {(d.type, d.id): position for position, d in enumerate(merged)}

    def replace(position: int, data: Mapping[str, Any]) -> None:
        definition = merged[position]
        merged[position] = Definition(
            type=definition.type,
            id=definition.id,
            data=data,
        )

    # A rest may remove inherited lengths even when the defaults layer is absent.
    for position, definition in enumerate(list(merged)):
        if definition.type != "rest" or "remove_lengths" not in definition.data:
            continue
        label = f"{template}: rest {definition.id}"
        data = dict(definition.data)
        raw_remove = data.pop("remove_lengths")
        if not isinstance(raw_remove, list) or not all(
            isinstance(item, str) and item for item in raw_remove
        ):
            raise ContentError(f"{label}: 'remove_lengths' must be a list of rest length ids")
        lengths = data.get("lengths")
        if not isinstance(lengths, Mapping):
            raise ContentError(f"{label}: 'lengths' must be an object")
        kept = dict(lengths)
        for length_id in raw_remove:
            if length_id not in kept:
                raise ContentError(
                    f"{label}: 'remove_lengths' names unknown rest length {length_id!r}"
                )
            kept.pop(length_id)
        data["lengths"] = kept
        replace(position, data)
    if not defaults:
        return merged

    for default in defaults:
        if default.type not in DEFAULT_DEFINITION_TYPES:
            raise ContentError(
                f"{template}: the defaults layer does not define {default.type!r} definitions"
            )
        meta = set(default.data) & META_KEYS
        if meta:
            names = ", ".join(repr(name) for name in sorted(meta))
            raise ContentError(
                f"{template}: defaults entry {default.type} {default.id!r} may not use {names}"
            )
        key = (default.type, default.id)
        if key in removed:
            continue
        if default.type == "clock":
            positions = [i for i, d in enumerate(merged) if d.type == "clock"]
            if not positions:
                continue
            position = positions[0]
            target = merged[position]
            data = _overlay_data(default.data, target.data, f"{template}: clock {target.id}")
            data["type"], data["id"] = target.type, target.id
            replace(position, data)
        elif default.type == "rest":
            positions = [i for i, d in enumerate(merged) if d.type == "rest"]
            if not positions:
                continue
            position = positions[0]
            target = merged[position]
            default_lengths = default.data.get("lengths", {})
            if not isinstance(default_lengths, Mapping):
                raise ContentError(
                    f"{template}: defaults rest {default.id!r}: 'lengths' must be an object"
                )
            data = dict(target.data)
            lengths = dict(data.get("lengths", {}))
            for length_id, body in default_lengths.items():
                if length_id not in lengths:
                    lengths[length_id] = copy.deepcopy(body)
            data["lengths"] = lengths
            if "interrupted" in default.data and "interrupted" not in data:
                data["interrupted"] = default.data["interrupted"]
            replace(position, data)
        elif default.type == "world_terrain":
            if key not in index:
                continue
            position = index[key]
            target = merged[position]
            data = dict(target.data)
            for field_name in ("name", "color", "pattern", "ink"):
                if field_name not in data and field_name in default.data:
                    data[field_name] = copy.deepcopy(default.data[field_name])
            replace(position, data)
        else:
            if key in index:
                continue
            if gate and capabilities is not None:
                requires, enabled_by = _read_gate(
                    default.data, f"{template}: {default.type} {default.id}"
                )
                if not _definition_enabled(capabilities, requires, enabled_by):
                    continue
            merged.append(
                Definition(
                    type=default.type,
                    id=default.id,
                    data=_strip_meta(default.data, default.type),
                )
            )
            index[key] = len(merged) - 1
    return merged


def _load_default_campaign(data: Mapping[str, Any], label: str) -> DefaultCampaign | None:
    """The optional `default_campaign` block on core's head: the rule set and world the New
    Campaign form starts on."""
    if "default_campaign" not in data:
        return None
    block_label = f"{label}: default_campaign"
    block = check_object_keys(data["default_campaign"], {"rule_set", "world_template"}, block_label)
    return DefaultCampaign(
        rule_set=required_text(block, "rule_set", block_label),
        world_template=required_text(block, "world_template", block_label),
    )


def _load_rule_set(
    directory: Path,
    core_definitions: list[Definition],
    defaults: Sequence[Definition] = (),
) -> RuleSet:
    """A rule set is its own definitions plus every core definition it does not replace. A core
    dice or creative table joins it only when its genres fit the rule set's own selection; the
    core library keeps every table, so the sourcebook stays complete."""
    template = directory.name
    base_by_key: dict[tuple[str, str], Definition] = {}
    for definition in (*core_definitions, *defaults):
        base_by_key[(definition.type, definition.id)] = Definition(
            type=definition.type,
            id=definition.id,
            data=_strip_meta(definition.data, definition.type),
        )
    own, removed = _resolve_definitions(read_definitions(directory), base_by_key, template)
    declared = _declared_capabilities(own, template)
    capabilities = declared or _infer_capabilities([*own, *core_definitions])
    own = _merge_defaults(
        own,
        defaults,
        template,
        removed=removed,
        capabilities=capabilities,
        gate=declared is not None,
    )
    core_creative = {d.id: d for d in core_definitions if d.type == "creative_table"}
    own = [
        _patch_creative_table(d, core_creative, template) if d.type == "creative_table" else d
        for d in own
    ]
    own_keys = {(d.type, d.id) for d in own}
    genres = _rule_set_table_genres(own, template)
    inherited = [
        d
        for d in core_definitions
        if d.type != "rule_set"
        and (d.type, d.id) not in own_keys
        and (d.type, d.id) not in removed
        and (declared is None or _inherited_definition_enabled(d, capabilities, template))
        and (
            d.type not in ("dice_table", "creative_table")
            or table_genre_offered(_definition_genres(d, template), genres)
        )
    ]
    # A rule set that does not declare capabilities keeps inherited definitions ungated. Passing
    # None makes the builder infer a permissive model for validation without dropping tools.
    build_capabilities = capabilities if declared is not None else None
    return _build_rule_set(
        [*own, *inherited], template, table_genres=genres, capabilities=build_capabilities
    )


def _build_rule_set(
    definitions: list[Definition],
    template: str,
    table_genres: tuple[str, ...] | None = None,
    capabilities: Capabilities | None = None,
) -> RuleSet:
    head = _single(definitions, "rule_set", template)
    _check_directory_id(template, head.id)
    if capabilities is None:
        capabilities = _declared_capabilities(definitions, template) or _infer_capabilities(
            definitions, permissive=True
        )
    label = f"{template}: rule_set"
    check_keys(
        head.data,
        {
            "type",
            "id",
            "name",
            "description",
            "blurb",
            "third_party",
            "plus",
            "skill_progression",
            "ordered_feature_tags",
            "optional_templates",
            "starting_silver",
            "starting_years",
            "stat_presets",
            "item_sizes",
            "training",
            "template_features",
            "condition_ends",
            "npc_hp",
            "npc_hp_stat",
            "table_genres",
            "capabilities",
            "default_campaign",
        },
        label,
    )
    if table_genres is None:
        table_genres = _rule_set_table_genres(definitions, template)
    # A compact `starting_years` head block expands into `starting_year` templates before the
    # character spec is built; an explicit template with the same id wins over its entry.
    definitions = [
        *definitions,
        *load_starting_years(
            head.data.get("starting_years"),
            label,
            {definition.id for definition in definitions if definition.type == "template"},
        ),
    ]
    definitions = apply_stat_presets(definitions, head.data.get("stat_presets"), label)
    definitions = _apply_item_sizes(definitions, head.data.get("item_sizes"), label)
    definitions = load_training_features(head.data.get("training"), definitions, label)
    _check_character_definitions(definitions, template)
    definitions = _derive_background_tables(definitions, template)
    for definition in definitions:
        if definition.type == "rule":
            check_keys(
                definition.data,
                {"type", "id", "text", "important"},
                f"{template}: rule {definition.id}",
            )
    modules = {d.id: _import_module(d, template) for d in definitions if d.type == "python_module"}
    rules = {
        d.id: required_text(d.data, "text", f"{template}: rule {d.id}")
        for d in definitions
        if d.type == "rule"
    }
    important = frozenset(
        d.id
        for d in definitions
        if d.type == "rule" and _flag(d.data, "important", f"{template}: rule {d.id}")
    )
    tables = {d.id: _load_table(d, template) for d in definitions if d.type == "table"}
    dice_tables = _load_dice_tables(definitions, template)
    wilderness = _load_wilderness(definitions, dice_tables, template)
    for name in REQUIRED_TABLES:
        if name not in tables:
            raise ContentError(f"{template}: missing table {name!r}")
    table_names = {d.id: _table_label(d, template) for d in definitions if d.type == "table"}
    feature_icons = _icon_entries(definitions, "feature", template)
    feature_types = _load_feature_types(definitions, template)
    character = build_character(
        definitions,
        tables,
        table_names,
        template,
        skill_progression=head.data.get("skill_progression"),
        ordered_feature_tags=head.data.get("ordered_feature_tags", []),
        icon_entries=feature_icons,
        starting_silver=load_starting_silver(head.data.get("starting_silver"), label),
        feature_types=feature_types,
        template_features=head.data.get("template_features"),
    )
    for feature in character.features.values():
        if feature.feature_type not in feature_types:
            raise ContentError(
                f"{template}: feature {feature.id!r} uses unknown feature_type "
                f"{feature.feature_type!r}"
            )
    optional_templates = _names(head.data, "optional_templates", label)
    character = replace(character, optional_kinds=optional_templates)
    campaign = build_campaign(definitions, tables, table_names, template)
    grids = [d for d in definitions if d.type == "encounter_grid"]
    if len(grids) > 1:
        raise ContentError(f"{template}: more than one encounter grid")
    grid = _load_grid(grids[0], template) if grids else None
    combats = [d for d in definitions if d.type == "combat"]
    if len(combats) > 1:
        raise ContentError(f"{template}: more than one combat definition")
    condition_ends = _names(head.data, "condition_ends", template)
    conditions = _load_conditions(definitions, character, condition_ends, template)
    stored_conditions = frozenset(c.id for c in conditions.values() if c.derived is None)
    counters = _load_counters(definitions, character, stored_conditions, template)
    character = replace(
        character,
        counters={
            c.id: CounterRef(
                id=c.id,
                label=c.label,
                initial=c.initial,
                minimum=c.minimum,
                maximum=c.maximum,
            )
            for c in counters.values()
        },
    )
    _check_template_grants(character, template)
    moves = _load_moves(definitions, character, frozenset(counters), template)
    checks = _load_checks(definitions, template)
    _check_feature_reactions(character, moves, checks, template)
    # Rule data is configured before the level-up definitions, because a level-up may resolve its
    # experience tracks and pick costs from it.
    rule_data = _load_rule_data(definitions, modules, character, template)
    advancements = _load_advancements(
        definitions, character, counters, frozenset(conditions), template
    )
    level_ups = _load_level_ups(
        definitions, modules, character, counters, campaign, rule_data, template
    )
    tallies = _load_tallies(definitions, template)
    periods = _load_periods(
        definitions, character, counters, frozenset(conditions), tallies, template
    )
    relations = _load_relations(definitions, template)
    injury_tables = {
        d.id: _load_injury_entries(d, character, conditions, template)
        for d in definitions
        if d.type == "injury_table"
    }
    combat = (
        _load_combat(combats[0], modules, character, conditions, injury_tables, template)
        if combats
        else None
    )
    if combat is None and any(c.dying or c.fatal for c in conditions.values()):
        raise ContentError(f"{template}: a condition is dying or fatal, but there is no combat")
    monsters = {
        d.id: _load_monster(d, character, template, pool=_hit_pool(combat), conditions=conditions)
        for d in definitions
        if d.type == "monster"
    }
    written = _attached_rules(monsters.values(), template)
    shared = sorted(written.keys() & rules.keys())
    if shared:
        raise ContentError(f"{template}: rules {shared} are both in the rule set and a statblock")
    known = {**rules, **written}
    for monster in monsters.values():
        for rule_id in monster.rules:
            if rule_id not in known:
                raise ContentError(
                    f"{template}: monster {monster.id!r} attaches rule {rule_id!r}, which is not "
                    "in the rule set"
                )
    clock = _load_clock(definitions, template)
    housekeeping = _load_housekeeping(definitions, template)
    checklist = _load_checklist(definitions, template)
    rests = _load_rests(definitions, character, template, clock, condition_ends)
    interrupted_rest = _load_interrupted_rest(definitions, rests, template)
    item_sizes = _load_item_sizes(definitions, template)
    inventory = _load_inventory(definitions, character, item_sizes, template)
    _check_item_sizes(character, item_sizes, template)
    magic = _load_magic(definitions, item_sizes, template)
    hooks = _load_rule_hooks(definitions, modules, character, rests, template)
    marketplace = _load_marketplace(
        definitions,
        item_sizes,
        character,
        template,
        icon_entries=_icon_entries(definitions, "shop_item", template),
    )
    prompt_fragments = _load_prompts(definitions, template, capabilities=capabilities)
    sentence_cap = _load_sentence_cap(definitions, template)
    travel = _load_travel(definitions, template)
    journey = _load_journey(definitions, dice_tables, template)
    terrain = _load_encounter_terrain(definitions, template)
    encounter_objects = _load_encounter_objects(definitions, template)
    encounter_templates = _load_encounter_templates(
        definitions, terrain, encounter_objects, template
    )
    world_terrain = _load_world_terrain(definitions, template)
    npc_hp = _npc_hp(head.data, label)
    npc_hp_stat = _optional_text(head.data, "npc_hp_stat", label)
    if npc_hp and npc_hp_stat is None:
        raise ContentError(f"{label}: 'npc_hp' needs 'npc_hp_stat', the stat its tiers set")
    if npc_hp_stat is not None and npc_hp_stat not in character.stats:
        raise ContentError(f"{label}: 'npc_hp_stat' {npc_hp_stat!r} is not a stat of this rule set")
    projects = _load_projects(definitions, checks, template)
    faction_assets = _load_faction_assets(definitions, template)
    enum_sources = {
        "moves": sorted(moves),
        "stats": sorted(character.stats),
        "skills": sorted(character.skills),
        # Item sizes and rest lengths keep the order their data file declares, so a tool's
        # choices match the rule set's own listing rather than an alphabetical rewrite.
        "item_sizes": list(item_sizes),
        "rest_lengths": list(rests),
        "journey_terrains": list(journey.terrain_speeds) if journey else [],
        "journey_weather": list(journey.weather_factors) if journey else [],
        "checks": sorted(checks),
        "counters": sorted(counters),
        "periods": sorted(periods),
        "tallies": sorted(tallies),
        "tally_entries": sorted({e for t in tallies.values() for e in t.entries}),
        "relations": sorted(relations),
        "advancement_options": sorted({o for a in advancements.values() for o in a.options}),
        "features": sorted(character.features),
        "feature_types": sorted(character.feature_types),
        "xp_tracks": sorted({name for rules in level_ups.values() for name in rules.tracks}),
    }
    tool_calls: dict[str, ToolCall] = {}
    tool_names: set[str] = set()
    for d in definitions:
        if d.type != "tool_call":
            continue
        call = _load_tool_call(
            d,
            modules,
            character,
            template,
            npc_hp,
            travel,
            enum_sources,
            capabilities=capabilities,
        )
        if call.name in tool_names:
            raise ContentError(f"{template}: duplicate tool name {call.name!r}")
        tool_names.add(call.name)
        tool_calls[call.id] = call
    _assert_consumed(definitions, RULE_SET_DEFINITION_TYPES, template)
    return RuleSet(
        id=head.id,
        name=required_text(head.data, "name", label),
        description=required_text(head.data, "description", label),
        blurb=_optional_text(head.data, "blurb", label),
        third_party=_flag(head.data, "third_party", label),
        table_genres=table_genres,
        content=RuleContent(
            rules=rules,
            attached_rules=written,
            important=important,
            tables=tables,
            monsters=monsters,
            npc_hp=npc_hp,
            npc_hp_stat=npc_hp_stat,
            dice_tables=dice_tables,
            prompt_fragments=prompt_fragments,
            checklist=checklist,
            sentence_cap=sentence_cap,
            terrain=terrain,
            encounter_objects=encounter_objects,
            encounter_templates=encounter_templates,
            world_terrain=world_terrain,
            optional_templates=optional_templates,
            housekeeping=housekeeping,
        ),
        tool_calls=tool_calls,
        character=character,
        plus=_plus_mode(head.data, label),
        campaign=campaign,
        default_campaign=_load_default_campaign(head.data, label),
        grid=grid,
        combat=combat,
        conditions=conditions,
        checks=checks,
        projects=projects,
        faction_assets=faction_assets,
        progression=ProgressionRules(
            counters=counters,
            advancements=advancements,
            level_ups=level_ups,
            periods=periods,
            tallies=tallies,
            relations=relations,
            moves=moves,
        ),
        time=TimeRules(
            clock=clock,
            rests=rests,
            interrupted_rest=interrupted_rest,
            travel=travel,
            journey=journey,
            wilderness=wilderness,
        ),
        items=ItemRules(
            inventory=inventory,
            item_sizes=item_sizes,
            magic=magic,
            marketplace=marketplace,
        ),
        hooks=hooks,
        condition_ends=condition_ends,
        rule_data=rule_data,
        capabilities=capabilities,
    )


def _load_travel(definitions: list[Definition], template: str) -> Travel | None:
    definition = _single_optional(definitions, "travel", template)
    if definition is None:
        return None
    label = f"{template}: travel {definition.id}"
    check_keys(definition.data, {"type", "id", "modes", "default_leg"}, label)
    modes_data = definition.data.get("modes")
    if not isinstance(modes_data, dict) or not modes_data:
        raise ContentError(f"{label}: 'modes' must map mode names to speeds")
    modes = {}
    for name, mode in modes_data.items():
        mode_label = f"{label}: mode {name!r}"
        if not isinstance(mode, dict):
            raise ContentError(f"{mode_label} must be an object")
        check_keys(mode, {"km_per_hour", "note"}, mode_label)
        speed = mode.get("km_per_hour")
        if not isinstance(speed, (int, float)) or isinstance(speed, bool) or speed <= 0:
            raise ContentError(f"{mode_label}: 'km_per_hour' must be a number above 0")
        modes[name] = TravelMode(
            name=name,
            km_per_hour=float(speed),
            note=required_text(mode, "note", mode_label),
        )
    default = definition.data.get("default_leg")
    default_label = f"{label}: default_leg"
    if not isinstance(default, dict):
        raise ContentError(f"{label}: 'default_leg' must be an object")
    check_keys(default, {"minutes", "note"}, default_label)
    return Travel(
        modes=modes,
        default_leg_minutes=_whole(default, "minutes", default_label, 1),
        default_leg_note=required_text(default, "note", default_label),
    )


def _load_journey(
    definitions: list[Definition], dice_tables: Mapping[str, DiceTable], template: str
) -> JourneyRules | None:
    definition = _single_optional(definitions, "journey", template)
    if definition is None:
        return None
    label = f"{template}: journey {definition.id}"
    check_keys(
        definition.data,
        {
            "type",
            "id",
            "model",
            "day_hours",
            "terrain_speeds",
            "road_multiplier",
            "weather_factors",
            "forage_difficulty",
            "food_per_traveler_day",
            "water_per_traveler_day",
            "firewood_per_day",
            "max_days",
            "max_travelers",
            "events",
        },
        label,
    )
    model = _optional_text(definition.data, "model", label) or "terrain_days"
    if model not in JOURNEY_MODELS:
        raise ContentError(f"{label}: 'model' must be one of {', '.join(JOURNEY_MODELS)}")
    terrain_days = model == "terrain_days"

    def numbers(key: str, *, whole: bool, required: bool) -> dict[str, float]:
        raw = definition.data.get(key, {})
        if not isinstance(raw, Mapping):
            raise ContentError(f"{label}: {key!r} must be an object")
        if required and not raw:
            raise ContentError(f"{label}: {key!r} must be a non-empty object")
        loaded: dict[str, float] = {}
        for name, value in raw.items():
            if not isinstance(name, str) or not name:
                raise ContentError(f"{label}: {key!r} names must be non-empty strings")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ContentError(f"{label}: {key!r} values must be numbers above 0")
            if whole and int(value) != value:
                raise ContentError(f"{label}: {key!r} values must be whole numbers")
            loaded[name] = float(value)
        return loaded

    terrain_speeds = numbers("terrain_speeds", whole=False, required=terrain_days)
    weather_factors = numbers("weather_factors", whole=False, required=terrain_days)
    forage = numbers("forage_difficulty", whole=True, required=terrain_days)
    if terrain_days and set(terrain_speeds) != set(forage):
        raise ContentError(
            f"{label}: 'terrain_speeds' and 'forage_difficulty' must name the same terrains"
        )

    def whole_or(key: str, default: int, minimum: int) -> int:
        if key not in definition.data:
            return default
        return _whole(definition.data, key, label, minimum)

    return JourneyRules(
        model=model,
        day_hours=whole_or("day_hours", 10, 1),
        terrain_speeds=terrain_speeds,
        road_multiplier=float(definition.data.get("road_multiplier", 2.0)),
        weather_factors=weather_factors,
        forage_difficulty={name: int(value) for name, value in forage.items()},
        food_per_traveler_day=whole_or("food_per_traveler_day", 1, 0),
        water_per_traveler_day=whole_or("water_per_traveler_day", 1, 0),
        firewood_per_day=whole_or("firewood_per_day", 4, 0),
        max_days=whole_or("max_days", 30, 1),
        max_travelers=whole_or("max_travelers", 20, 1),
        events=_load_journey_events(definition.data, dice_tables, model, label),
    )


def _load_journey_events(
    data: Mapping[str, Any], dice_tables: Mapping[str, DiceTable], model: str, label: str
) -> tuple[JourneyEvent, ...]:
    """The journey's dice-table events, validated against the rule set's tables and the
    journey's model. A terrain-day journey takes "day" events; a watch journey takes "watch"
    events."""
    raw = data.get("events", [])
    if not isinstance(raw, list):
        raise ContentError(f"{label}: 'events' must be a list of objects")
    expected = "day" if model == "terrain_days" else "watch"
    events = []
    for index, entry in enumerate(raw):
        event_label = f"{label}: events {index}"
        if not isinstance(entry, Mapping):
            raise ContentError(f"{event_label} must be an object")
        check_object_keys(entry, JOURNEY_EVENT_KEYS, event_label)
        on = required_text(entry, "on", event_label)
        if on not in JOURNEY_EVENT_ON:
            raise ContentError(f"{event_label}: 'on' must be one of {', '.join(JOURNEY_EVENT_ON)}")
        if on != expected:
            raise ContentError(
                f"{event_label}: a {model} journey takes '{expected}' events, not {on!r}"
            )
        table = required_text(entry, "table", event_label)
        if table not in dice_tables:
            raise ContentError(f"{event_label}: 'table' names unknown dice table {table!r}")
        when_label = f"{event_label}: when"
        when = check_object_keys(entry.get("when", {}), JOURNEY_WHEN_KEYS, when_label)
        chance = when.get("chance")
        if chance is not None and (
            isinstance(chance, bool)
            or not isinstance(chance, int)
            or not 1 <= chance <= MAX_JOURNEY_CHANCE
        ):
            raise ContentError(
                f"{when_label}: 'chance' must be a whole number from 1 to {MAX_JOURNEY_CHANCE}"
            )
        events.append(
            JourneyEvent(
                on=on,
                table=table,
                reason=_optional_text(entry, "reason", event_label) or "",
                terrain=_names(when, "terrain", when_label),
                weather=_names(when, "weather", when_label),
                road=_flags(when, "road", when_label),
                path=_names(when, "path", when_label),
                chance=chance,
            )
        )
    return tuple(events)


def _flags(data: Mapping[str, Any], key: str, label: str) -> tuple[bool, ...]:
    """A list of true or false values, such as the road states a journey event applies to."""
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, bool) for item in value):
        raise ContentError(f"{label}: {key!r} must be a list of true or false")
    return tuple(value)


WILDERNESS_KEYS = frozenset(
    {
        "type",
        "id",
        "seasons",
        "start_season",
        "season_days",
        "catastrophe_streak",
        "catastrophe_weather",
        "catastrophe_result",
        "lost_on",
        "lost_die",
        "actions",
        "events_table",
        "weather_tables",
        "hooks",
        "travel",
    }
)


def _load_wilderness(
    definitions: list[Definition], dice_tables: Mapping[str, DiceTable], template: str
) -> Wilderness | None:
    definition = _single_optional(definitions, "wilderness", template)
    if definition is None:
        return None
    data = definition.data
    label = f"{template}: wilderness {definition.id}"
    unknown = set(data) - WILDERNESS_KEYS
    if unknown:
        raise ContentError(f"{label} has unknown keys {sorted(unknown)}")
    seasons = _names(data, "seasons", label)
    if not seasons or len(set(seasons)) != len(seasons):
        raise ContentError(f"{label}: 'seasons' must be a non-empty list of distinct names")
    start_season = required_text(data, "start_season", label)
    if start_season not in seasons:
        raise ContentError(f"{label}: 'start_season' {start_season!r} is not one of 'seasons'")
    lost_on_data = data.get("lost_on")
    if not isinstance(lost_on_data, Mapping):
        raise ContentError(f"{label}: 'lost_on' must map path ids to whole numbers from 0 to 6")
    lost_on = {}
    for path, faces in lost_on_data.items():
        if not isinstance(faces, int) or isinstance(faces, bool) or not 0 <= faces <= 6:
            raise ContentError(f"{label}: 'lost_on' {path!r} must be a whole number from 0 to 6")
        lost_on[path] = faces
    lost_die = data.get("lost_die", "1d6")
    if not is_formula(lost_die) or "+" in lost_die:
        raise ContentError(f"{label}: 'lost_die' must be one dice term such as '1d6'")
    actions_data = data.get("actions")
    if not isinstance(actions_data, Mapping) or not actions_data:
        raise ContentError(f"{label}: 'actions' must map action ids to objects")
    actions = {}
    for action_id, action in actions_data.items():
        action_label = f"{label}: action {action_id!r}"
        if not isinstance(action, Mapping):
            raise ContentError(f"{action_label} must be an object")
        unknown = set(action) - {"name", "text", "hooks"}
        if unknown:
            raise ContentError(f"{action_label} has unknown keys {sorted(unknown)}")
        actions[action_id] = WildernessAction(
            id=action_id,
            name=required_text(action, "name", action_label),
            text=required_text(action, "text", action_label),
            hooks=_load_hooks(action.get("hooks"), action_label),
        )
    events_table = required_text(data, "events_table", label)
    if events_table not in dice_tables:
        raise ContentError(f"{label}: 'events_table' names unknown dice table {events_table!r}")
    weather_data = data.get("weather_tables")
    if not isinstance(weather_data, Mapping) or set(weather_data) != set(seasons):
        raise ContentError(f"{label}: 'weather_tables' must have exactly one entry per season")
    weather_tables = {}
    for season, table in weather_data.items():
        if not isinstance(table, str) or table not in dice_tables:
            raise ContentError(
                f"{label}: 'weather_tables' {season!r} names unknown dice table {table!r}"
            )
        weather_tables[season] = table
    return Wilderness(
        seasons=seasons,
        start_season=start_season,
        season_days=_whole(data, "season_days", label, 1),
        catastrophe_streak=_whole(data, "catastrophe_streak", label, 1),
        catastrophe_weather=required_text(data, "catastrophe_weather", label),
        catastrophe_result=required_text(data, "catastrophe_result", label),
        lost_on=lost_on,
        lost_die=lost_die,
        actions=actions,
        events_table=events_table,
        weather_tables=weather_tables,
        hooks=_load_hooks(data.get("hooks"), label),
        travel=_load_wilderness_travel(data.get("travel"), lost_on, label)
        if "travel" in data
        else None,
    )


TRAVEL_KEYS = frozenset(
    {"path", "distance", "terrain", "vast_max", "held_back_terrain_steps", "weather"}
)
WEATHER_PENALTY_KEYS = frozenset({"choice", "fatigue", "watches", "terrain_steps", "travel"})


def _load_wilderness_travel(
    data: object, lost_on: Mapping[str, int], label: str
) -> WildernessTravel:
    if not isinstance(data, Mapping):
        raise ContentError(f"{label}: 'travel' must be an object")
    travel_label = f"{label}: travel"
    unknown = set(data) - TRAVEL_KEYS
    if unknown:
        raise ContentError(f"{travel_label} has unknown keys {sorted(unknown)}")
    path = _whole_map(data, "path", travel_label)
    if set(path) != set(lost_on):
        raise ContentError(f"{travel_label}: 'path' keys must match the keys of 'lost_on'")
    weather_data = data.get("weather", {})
    if not isinstance(weather_data, Mapping):
        raise ContentError(f"{travel_label}: 'weather' must map weather results to objects")
    weather = {}
    for result, penalty in weather_data.items():
        penalty_label = f"{travel_label}: weather {result!r}"
        if not isinstance(penalty, Mapping):
            raise ContentError(f"{penalty_label} must be an object")
        unknown = set(penalty) - WEATHER_PENALTY_KEYS
        if unknown:
            raise ContentError(f"{penalty_label} has unknown keys {sorted(unknown)}")
        weather[result] = WeatherPenalty(
            choice=_flag(penalty, "choice", penalty_label),
            fatigue=_optional_whole(penalty, "fatigue", penalty_label),
            watches=_optional_whole(penalty, "watches", penalty_label),
            terrain_steps=_optional_whole(penalty, "terrain_steps", penalty_label),
            travel=_flag_default(penalty, "travel", penalty_label, True),
        )
    return WildernessTravel(
        path=path,
        distance=_whole_map(data, "distance", travel_label),
        terrain=_whole_map(data, "terrain", travel_label),
        vast_max=_whole(data, "vast_max", travel_label, 0),
        held_back_terrain_steps=_whole(data, "held_back_terrain_steps", travel_label, 0),
        weather=weather,
    )


def _signed_map(data: Mapping[str, Any], key: str, label: str) -> dict[str, int]:
    """A name-to-whole-number map that may be negative, such as range penalties."""
    value = data.get(key, {})
    if not isinstance(value, Mapping):
        raise ContentError(f"{label}: {key!r} must map names to whole numbers")
    result: dict[str, int] = {}
    for name, number in value.items():
        if not isinstance(name, str) or not name.strip():
            raise ContentError(f"{label}: {key!r} needs named entries")
        if not isinstance(number, int) or isinstance(number, bool):
            raise ContentError(f"{label}: {key!r} {name!r} must be a whole number")
        result[name.strip().casefold()] = number
    return result


def _whole_map(data: Mapping[str, Any], key: str, label: str) -> dict[str, int]:
    value = data.get(key)
    if not isinstance(value, Mapping) or not value:
        raise ContentError(f"{label}: {key!r} must map names to whole numbers, at least 0")
    result = {}
    for name, number in value.items():
        if not isinstance(number, int) or isinstance(number, bool) or number < 0:
            raise ContentError(f"{label}: {key!r} {name!r} must be a whole number, at least 0")
        result[name] = number
    return result


def _optional_whole(data: Mapping[str, Any], key: str, label: str) -> int:
    if key not in data:
        return 0
    return _whole(data, key, label, 0)


def _optional_signed_whole(data: Mapping[str, Any], key: str, label: str) -> int:
    """A whole number that may be negative, such as a reputation counter's floor."""
    if key not in data:
        return 0
    return _whole(data, key, label, -999)


def _flag_default(data: Mapping[str, Any], key: str, label: str, default: bool) -> bool:
    if key not in data:
        return default
    return _flag(data, key, label)


def _load_hooks(data: object, label: str) -> WildernessHooks:
    """The lifecycle hooks of a wilderness procedure or action. Missing means none."""
    if data is None:
        return WildernessHooks()
    if not isinstance(data, Mapping) or not set(data) <= {"resolve", "night"}:
        raise ContentError(f"{label}: 'hooks' must be an object with only 'resolve' and 'night'")
    return WildernessHooks(
        resolve=_load_effects(data.get("resolve", []), f"{label}: 'hooks' 'resolve'"),
        night=_load_effects(data.get("night", []), f"{label}: 'hooks' 'night'"),
    )


def _load_effects(data: object, label: str) -> tuple[WildernessEffect, ...]:
    if not isinstance(data, list):
        raise ContentError(f"{label} must be a list of effects")
    return tuple(_load_effect(entry, label) for entry in data)


def _load_effect(entry: object, label: str) -> WildernessEffect:
    if not isinstance(entry, Mapping):
        raise ContentError(f"{label}: each effect must be an object")
    do = required_text(entry, "do", label)
    if do not in WILDERNESS_DO:
        raise ContentError(f"{label}: effect 'do' {do!r} is not one of {', '.join(WILDERNESS_DO)}")
    unexpected = set(entry) - set(WILDERNESS_EFFECT_KEYS[do]) - {"do"}
    if unexpected:
        raise ContentError(f"{label}: {do} does not take {', '.join(sorted(unexpected))}")
    if do == "consume":
        per = required_text(entry, "per", label)
        if per not in WILDERNESS_PER:
            raise ContentError(f"{label}: consume 'per' must be one of {', '.join(WILDERNESS_PER)}")
        track = entry.get("track", False)
        if not isinstance(track, bool):
            raise ContentError(f"{label}: consume 'track' must be true or false")
        if track and per != "member":
            raise ContentError(f"{label}: a consume that is 'track' must be per member")
        return WildernessEffect(
            do=do,
            item=required_text(entry, "item", label),
            per=per,
            track=track,
            alternatives=_load_alternatives(entry, label),
            on_shortage=_optional_nonempty_text(entry, "on_shortage", label),
        )
    if do == "buy":
        return WildernessEffect(do=do, items=_load_buy_items(entry, label))
    if do == "grant":
        return WildernessEffect(
            do=do, item=required_text(entry, "item", label), count=_die_growth(entry, label)
        )
    if do == "clear":
        return WildernessEffect(do=do, item=required_text(entry, "item", label))
    return WildernessEffect(do=do, times=_whole(entry, "times", label, 1))


def _load_alternatives(effect: Mapping[str, Any], label: str) -> tuple[WildernessAlternative, ...]:
    """The optional 'alternatives' of a consume: objects with an 'item' and an optional 'requires'."""
    if "alternatives" not in effect:
        return ()
    raw = effect["alternatives"]
    if not isinstance(raw, list):
        raise ContentError(f"{label}: consume 'alternatives' must be a list of objects")
    alternatives = []
    for index, alt in enumerate(raw):
        alt_label = f"{label}: consume 'alternatives' {index}"
        if not isinstance(alt, Mapping):
            raise ContentError(f"{alt_label} must be an object with 'item' and optional 'requires'")
        unexpected = set(alt) - WILDERNESS_ALTERNATIVE_KEYS
        if unexpected:
            raise ContentError(f"{alt_label} does not take {', '.join(sorted(unexpected))}")
        alternatives.append(
            WildernessAlternative(
                item=required_text(alt, "item", alt_label),
                requires=_optional_nonempty_text(alt, "requires", alt_label),
            )
        )
    return tuple(alternatives)


def _load_buy_items(effect: Mapping[str, Any], label: str) -> tuple[str, ...]:
    """The non-empty list of items a buy effect lets a resupply purchase."""
    items = effect.get("items")
    if not isinstance(items, list) or not items:
        raise ContentError(f"{label}: buy 'items' must be a non-empty list of item names")
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise ContentError(f"{label}: buy 'items' must be non-empty text")
    return tuple(items)


def _optional_nonempty_text(data: Mapping[str, Any], key: str, label: str) -> str | None:
    if key not in data:
        return None
    value = data[key]
    if not isinstance(value, str) or not value.strip():
        raise ContentError(f"{label}: {key!r} must be non-empty text")
    return value


def _die_growth(effect: Mapping[str, Any], label: str) -> DieGrowth:
    """The die a grant rolls, from its 'count' object."""
    count = effect.get("count")
    if not isinstance(count, Mapping):
        raise ContentError(f"{label}: grant 'count' must be an object with base, per_member, max")
    unexpected = set(count) - {"base", "per_member", "max"}
    if unexpected:
        raise ContentError(f"{label}: grant 'count' does not take {', '.join(sorted(unexpected))}")
    base = _whole(count, "base", label, 1)
    growth = DieGrowth(
        base=base,
        per_member=_whole(count, "per_member", label, 0),
        max=_whole(count, "max", label, base),
    )
    if growth.max > MAX_GRANT_SIDES:
        raise ContentError(f"{label}: grant 'count' max must be at most {MAX_GRANT_SIDES}")
    return growth


def _load_combat(
    definition: Definition,
    modules: Mapping[str, ModuleType],
    character: CharacterSpec,
    conditions: Mapping[str, ConditionRules],
    injury_tables: Mapping[str, tuple[InjuryEntry, ...]],
    template: str,
) -> CombatRules:
    label = f"{template}: combat {definition.id}"
    data = definition.data
    check_keys(
        data,
        {
            "type",
            "id",
            "python_module",
            "damage",
            "check_die",
            "initiative_stat",
            "defeat",
            "outside_harm",
            "healing_stat",
            "morale",
            "modifiers",
            "dying_hours",
            "dying_rounds",
            "armor_max",
            "aid_items",
            "unarmed",
            "hit_mode",
            "multi_attack",
            "defense_base",
            "defense_modifier",
            "natural_one_misses",
            "natural_max_hits",
            "total_defense",
            "range_bands",
            "dual_wield_hit_penalty",
            "dual_wield_damage_bonus",
            "surprise_turns",
            "save",
            "initiative_mode",
            "groups",
            "hit_bonus_stat",
            "unskilled_attack_penalty",
            "damage_bonus_stat",
            "attribute_damage",
            "shock_on_miss",
            "minimum_shock_damage",
            "defeat_at_zero",
            "initiative",
            "injuries",
            "level_damage",
        },
        label,
    )
    module: ModuleType | None = None
    if "python_module" in data:
        module_id = required_text(data, "python_module", label)
        if module_id not in modules:
            raise ContentError(f"{label}: unknown python_module {module_id!r}")
        module = modules[module_id]
    damage = _load_damage(data, character, label)
    check_die = required_text(data, "check_die", label)
    if not re.fullmatch(r"1d[0-9]+", check_die) or int(check_die[2:]) < 2:
        raise ContentError(f"{label}: 'check_die' must be a single die such as '1d20'")
    check_sides = int(check_die[2:])
    initiative_stat = required_text(data, "initiative_stat", label)
    if initiative_stat not in character.stats:
        raise ContentError(f"{label}: 'initiative_stat' names no stat ({initiative_stat!r})")
    defeat_data = data.get("defeat")
    if not isinstance(defeat_data, Mapping):
        raise ContentError(f"{label}: 'defeat' must be an object")
    # The labels are an open map: `player_character`, `player_character_dead`, `important_npc`,
    # `npc`, and any future label a rule set invents. Every value must be non-empty text, but the
    # loader must not drop a label it does not recognize.
    defeat: dict[str, str] = {}
    for key in defeat_data:
        if not isinstance(key, str) or not key.strip():
            raise ContentError(f"{label}: 'defeat' labels must be non-empty text")
        defeat[key] = required_text(defeat_data, key, f"{label}: defeat")
    for key in ("player_character", "npc"):
        if key not in defeat:
            # Use the standard text error so the message stays consistent with other loaders.
            required_text(defeat_data, key, f"{label}: defeat")
    injury_table = _load_injury_table_ref(data, character, injury_tables, label)
    outside_harm = _load_outside_harm(data, character, label)
    unarmed_damage, unarmed_names, unarmed_skill = _load_unarmed(data, character, label)
    unarmed_attributes = _names(data.get("unarmed", {}), "attributes", f"{label}: unarmed")
    for stat_id in unarmed_attributes:
        if stat_id not in character.stats:
            raise ContentError(f"{label}: unarmed attribute {stat_id!r} names no stat")
    for key in ("hit_bonus_stat", "damage_bonus_stat", "defense_modifier", "healing_stat"):
        value = _optional_text(data, key, label)
        if value is not None and value not in character.stats:
            raise ContentError(f"{label}: {key!r} names no stat ({value!r})")
    for stat_id in _names(data, "defeat_at_zero", label):
        if stat_id not in character.stats:
            raise ContentError(f"{label}: 'defeat_at_zero' names no stat ({stat_id!r})")
    level_damage = _load_level_damage(data, character, label)
    return CombatRules(
        id=definition.id,
        check_die=check_die,
        check_sides=check_sides,
        initiative_stat=initiative_stat,
        defeat=defeat,
        attack=AttackRules(
            damage=damage,
            modifiers=_load_modifiers(data, label),
            hit_mode=_choice(data, "hit_mode", ("auto", "roll_vs_defense"), "auto", label),
            multi_attack=_choice(data, "multi_attack", ("highest", "each"), "highest", label),
            groups=_load_groups(data, label),
            hit_bonus_stat=_optional_text(data, "hit_bonus_stat", label) or "",
            unskilled_attack_penalty=_optional_whole(data, "unskilled_attack_penalty", label) or 0,
            damage_bonus_stat=_optional_text(data, "damage_bonus_stat", label) or "",
            attribute_damage=_flag_default(data, "attribute_damage", label, False),
            shock_on_miss=_flag_default(data, "shock_on_miss", label, False),
            minimum_shock_damage=_flag_default(data, "minimum_shock_damage", label, False),
            natural_one_misses=_flag_default(data, "natural_one_misses", label, False),
            natural_max_hits=_flag_default(data, "natural_max_hits", label, False),
            level_damage=level_damage,
        ),
        defense=DefenseRules(
            armor_max=_whole(data, "armor_max", label, 0) if "armor_max" in data else 0,
            base=_optional_whole(data, "defense_base", label) or 0,
            modifier=_optional_text(data, "defense_modifier", label) or "",
        ),
        dying=DyingRules(
            outside_harm=outside_harm,
            dying_hours=_whole(data, "dying_hours", label, 0) if "dying_hours" in data else 0,
            dying_rounds=(_whole(data, "dying_rounds", label, 0) if "dying_rounds" in data else 0),
            dying_conditions=tuple(c.id for c in conditions.values() if c.dying),
            fatal_conditions=tuple(c.id for c in conditions.values() if c.fatal),
            aid_items=_names(data, "aid_items", label),
        ),
        unarmed=UnarmedRules(
            damage=unarmed_damage,
            names=unarmed_names,
            attributes=unarmed_attributes,
            skill=unarmed_skill,
        ),
        injury_table=injury_table,
        healing_stat=_optional_text(data, "healing_stat", label),
        morale=_load_morale(data, character, label),
        save=_load_save(data, check_sides, label),
        initiative_mode=_choice(
            data, "initiative_mode", ("pass_npcs_fail", "roll_order"), "pass_npcs_fail", label
        ),
        defeat_at_zero=_names(data, "defeat_at_zero", label),
        initiative=_load_initiative(data, character, label),
        module=module,
        total_defense_bonus=_optional_whole(data, "total_defense", label) or 0,
        range_penalties=_signed_map(data, "range_bands", label),
        dual_wield_hit_penalty=(
            _whole(data, "dual_wield_hit_penalty", label, -99)
            if "dual_wield_hit_penalty" in data
            else 0
        ),
        dual_wield_damage_bonus=(
            _whole(data, "dual_wield_damage_bonus", label, -99)
            if "dual_wield_damage_bonus" in data
            else 0
        ),
        surprise_turns=_optional_whole(data, "surprise_turns", label) or 0,
    )


INITIATIVE_KEYS = frozenset({"die", "bonus_stat", "side", "tie"})


def _load_initiative(
    data: Mapping[str, Any], character: CharacterSpec, label: str
) -> InitiativeRules | None:
    """The optional side-based initiative block. Absent means each character rolls as
    `initiative_mode` says."""
    block = data.get("initiative")
    if block is None:
        return None
    if not isinstance(block, Mapping):
        raise ContentError(f"{label}: 'initiative' must be an object")
    initiative_label = f"{label}: initiative"
    unknown = set(block) - INITIATIVE_KEYS
    if unknown:
        raise ContentError(f"{initiative_label} has unknown keys {sorted(unknown)}")
    die = block.get("die", "1d8")
    if not isinstance(die, str) or not re.fullmatch(r"1d[0-9]+", die) or int(die[2:]) < 2:
        raise ContentError(f"{initiative_label}: 'die' must be a single die such as '1d8'")
    bonus_stat = block.get("bonus_stat", "dex")
    if not isinstance(bonus_stat, str):
        raise ContentError(f"{initiative_label}: 'bonus_stat' must be a stat id")
    if bonus_stat and bonus_stat not in character.stats:
        raise ContentError(f"{initiative_label}: 'bonus_stat' names no stat ({bonus_stat!r})")
    side = block.get("side", True)
    if not isinstance(side, bool):
        raise ContentError(f"{initiative_label}: 'side' must be true or false")
    tie = block.get("tie", "party")
    if tie not in ("party", "foes"):
        raise ContentError(f"{initiative_label}: 'tie' must be party or foes")
    return InitiativeRules(die=die, bonus_stat=bonus_stat, side=side, tie=tie)


def _choice(
    data: Mapping[str, Any], key: str, choices: tuple[str, ...], default: str, label: str
) -> str:
    value = data.get(key, default)
    if value not in choices:
        raise ContentError(f"{label}: {key!r} must be one of {', '.join(choices)}")
    return value


def _load_save(data: Mapping[str, Any], check_sides: int, label: str) -> SaveRules:
    """The declarative save rule. Optional; its rolls must be on the check die."""
    block = data.get("save")
    if block is None:
        return SaveRules(auto_success=1, auto_fail=check_sides)
    save_label = f"{label}: save"
    block = check_object_keys(block, {"mode", "auto_success", "auto_fail"}, save_label)
    mode = _choice(block, "mode", ("roll_under", "roll_over"), "roll_under", save_label)
    rolls: dict[str, int | None] = {}
    for key in ("auto_success", "auto_fail"):
        value = block.get(key)
        if value is None:
            rolls[key] = None
        elif isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= check_sides:
            raise ContentError(f"{label}: save {key!r} must be a roll on the check die")
        else:
            rolls[key] = value
    return SaveRules(mode=mode, auto_success=rolls["auto_success"], auto_fail=rolls["auto_fail"])


def _load_groups(data: Mapping[str, Any], label: str) -> GroupRules | None:
    """The large-group rules. Optional; absent means the rule set has no groups."""
    block = data.get("groups")
    if block is None:
        return None
    groups_label = f"{label}: groups"
    block = check_object_keys(
        block,
        {"incoming_modifier", "blast_modifier", "blast_hits_all", "break_outcomes"},
        groups_label,
    )
    blast_hits_all = block.get("blast_hits_all", True)
    if not isinstance(blast_hits_all, bool):
        raise ContentError(f"{groups_label}: 'blast_hits_all' must be true or false")
    outcomes = block.get("break_outcomes", ["defeat", "flee"])
    if (
        not isinstance(outcomes, list)
        or not outcomes
        or not all(isinstance(outcome, str) and outcome for outcome in outcomes)
    ):
        raise ContentError(f"{groups_label}: 'break_outcomes' must be a non-empty list of names")
    return GroupRules(
        incoming_modifier=required_text(block, "incoming_modifier", groups_label),
        blast_modifier=required_text(block, "blast_modifier", groups_label),
        blast_hits_all=blast_hits_all,
        break_outcomes=tuple(outcomes),
    )


def _load_morale(
    data: Mapping[str, Any], character: CharacterSpec, label: str
) -> MoraleRules | None:
    morale_data = data.get("morale")
    if morale_data is None:
        return None
    morale_label = f"{label}: morale"
    morale_data = check_object_keys(
        morale_data,
        {"save_stat", "triggers", "lone_at", "retreat_stat"},
        morale_label,
    )
    stats = {
        key: required_text(morale_data, key, morale_label)
        for key in ("save_stat", "lone_at", "retreat_stat")
    }
    for key, stat in stats.items():
        if stat not in character.stats:
            raise ContentError(f"{morale_label}: {key!r} names no stat ({stat!r})")
    raw_triggers = morale_data.get("triggers")
    if not isinstance(raw_triggers, list) or not raw_triggers:
        raise ContentError(f"{morale_label}: 'triggers' must be a non-empty list")
    triggers: list[MoraleTrigger] = []
    for index, raw in enumerate(raw_triggers):
        item_label = f"{morale_label}: triggers[{index}]"
        if not isinstance(raw, Mapping):
            raise ContentError(f"{item_label}: must be an object")
        item = check_object_keys(raw, {"tag", "when"}, item_label)
        tag = required_text(item, "tag", item_label)
        when = required_text(item, "when", item_label)
        if when not in MORALE_TRIGGER_WHENS:
            raise ContentError(
                f"{item_label}: 'when' must be one of {', '.join(MORALE_TRIGGER_WHENS)}"
            )
        triggers.append(MoraleTrigger(tag=tag, when=when))
    return MoraleRules(
        save_stat=stats["save_stat"],
        triggers=tuple(triggers),
        lone_at=stats["lone_at"],
        retreat_stat=stats["retreat_stat"],
    )


def _load_injury_table_ref(
    data: Mapping[str, Any],
    character: CharacterSpec,
    injury_tables: Mapping[str, tuple[InjuryEntry, ...]],
    label: str,
) -> InjuryTable | None:
    """Load the injury table, trigger, and character kinds that can receive an injury."""
    if "injuries" not in data:
        return None
    ref = data["injuries"]
    if not isinstance(ref, Mapping):
        raise ContentError(f"{label}: 'injuries' must be an object")
    injury_label = f"{label}: injuries"
    allowed = {"table", "stat", "trigger", "target_kinds"}
    unknown = set(ref) - allowed
    if unknown:
        raise ContentError(
            f"{injury_label}: unexpected field(s): {', '.join(sorted(map(str, unknown)))}"
        )
    table_id = required_text(ref, "table", injury_label)
    if table_id not in injury_tables:
        raise ContentError(f"{label}: 'injuries' names no injury table ({table_id!r})")
    stat = required_text(ref, "stat", injury_label)
    if stat not in character.stats:
        raise ContentError(f"{label}: 'injuries' names no stat ({stat!r})")
    trigger = _choice(ref, "trigger", ("depleted", "damage"), "", injury_label)
    target_kinds = _names(ref, "target_kinds", injury_label)
    if not target_kinds:
        raise ContentError(f"{injury_label}: 'target_kinds' must be a non-empty list")
    if len(target_kinds) != len(set(target_kinds)):
        raise ContentError(f"{injury_label}: 'target_kinds' must not list a kind twice")
    for kind in target_kinds:
        if kind not in ("pc", "npc"):
            raise ContentError(f"{injury_label}: 'target_kinds' names no character kind ({kind!r})")
    return InjuryTable(
        id=table_id,
        stat=stat,
        entries=injury_tables[table_id],
        trigger=trigger,
        target_kinds=target_kinds,
    )


def _load_outside_harm(
    data: Mapping[str, Any], character: CharacterSpec, label: str
) -> tuple[str, ...]:
    stats = data.get("outside_harm", [])
    if not isinstance(stats, list) or not all(isinstance(stat, str) for stat in stats):
        raise ContentError(f"{label}: 'outside_harm' must be a list of stat ids")
    for stat in stats:
        if stat not in character.stats:
            raise ContentError(f"{label}: 'outside_harm' names no stat ({stat!r})")
    if len(stats) != len(set(stats)):
        raise ContentError(f"{label}: 'outside_harm' lists a stat twice")
    return tuple(stats)


def _load_unarmed(
    data: Mapping[str, Any], character: CharacterSpec, label: str
) -> tuple[str, frozenset[str], str]:
    """The dice an unarmed attack rolls and the names that mean unarmed. Optional; names are
    compared without regard to case. Its optional skill must belong to the rule set."""
    block = data.get("unarmed")
    if block is None:
        return "", frozenset(), ""
    block = check_object_keys(
        block, {"damage", "names", "attributes", "skill"}, f"{label}: unarmed"
    )
    damage = block.get("damage")
    if not isinstance(damage, str) or not is_formula(damage):
        raise ContentError(f"{label}: 'unarmed.damage' must be a dice formula such as '1d4'")
    names = _names(block, "names", f"{label}: unarmed")
    if not names:
        raise ContentError(f"{label}: 'unarmed.names' needs at least one name")
    skill = _optional_text(block, "skill", f"{label}: unarmed") or ""
    if skill and skill not in character.skills:
        raise ContentError(f"{label}: 'unarmed.skill' names no skill ({skill!r})")
    return damage, frozenset(name.strip().casefold() for name in names), skill


def _load_level_damage(
    data: Mapping[str, Any], character: CharacterSpec, label: str
) -> LevelDamageRules | None:
    """The optional source of template level damage. Every part is required when configured."""
    block = data.get("level_damage")
    if block is None:
        return None
    if not isinstance(block, Mapping):
        raise ContentError(f"{label}: 'level_damage' must be an object")
    level_label = f"{label}: level_damage"
    allowed = {"template_kind", "templates", "counter", "divisor"}
    unknown = set(block) - allowed
    if unknown:
        raise ContentError(
            f"{level_label}: unexpected field(s): {', '.join(sorted(map(str, unknown)))}"
        )
    template_kind = required_text(block, "template_kind", level_label)
    template_ids = _names(block, "templates", level_label)
    if not template_ids:
        raise ContentError(f"{level_label}: 'templates' must be a non-empty list")
    counter = required_text(block, "counter", level_label)
    divisor = _whole(block, "divisor", level_label, 1)
    if len(template_ids) != len(set(template_ids)):
        raise ContentError(f"{level_label}: 'templates' must not list an id twice")
    kind_ids = {
        template.id for template in character.templates.values() if template.kind == template_kind
    }
    if not kind_ids:
        raise ContentError(
            f"{level_label}: 'template_kind' names no template kind ({template_kind!r})"
        )
    if counter not in character.counters:
        raise ContentError(f"{level_label}: 'counter' names no counter ({counter!r})")
    for template_id in template_ids:
        if template_id not in kind_ids:
            raise ContentError(
                f"{level_label}: 'templates' names no {template_kind} template ({template_id!r})"
            )
    return LevelDamageRules(
        template_kind=template_kind,
        templates=template_ids,
        counter=counter,
        divisor=divisor,
    )


def _load_modifiers(data: Mapping[str, Any], label: str) -> dict[str, str]:
    """The attack modifiers and the dice each rolls, such as {"impaired": "1d4"}. Optional. Names
    are compared without regard to case; "normal" is reserved for the attack's own dice."""
    entries = data.get("modifiers", {})
    if not isinstance(entries, Mapping):
        raise ContentError(f"{label}: 'modifiers' must be an object of name to dice formula")
    modifiers: dict[str, str] = {}
    for name, formula in entries.items():
        key = name.strip().casefold() if isinstance(name, str) else ""
        if not key:
            raise ContentError(f"{label}: a modifier needs a name")
        if key == "normal":
            raise ContentError(f"{label}: 'normal' is reserved and cannot be a modifier")
        if key in modifiers:
            raise ContentError(f"{label}: modifier {name!r} is listed twice")
        if not isinstance(formula, str) or not is_formula(formula):
            raise ContentError(f"{label}: modifier {name!r} must be a dice formula such as '1d4'")
        modifiers[key] = formula
    return modifiers


def _load_damage(
    data: Mapping[str, Any], character: CharacterSpec, label: str
) -> tuple[DamageStep, ...]:
    """The damage steps, in order: at most one `absorb` first, then `harm` steps, then at most one
    `overflow` last. Each stat must be a character stat, and no stat may be used twice."""
    entries = data.get("damage")
    if not isinstance(entries, list) or not entries:
        raise ContentError(f"{label}: 'damage' must be a non-empty list of steps")
    steps = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ContentError(f"{label}: each damage step must be an object")
        check_keys(entry, {"kind", "stat"}, label)
        kind = required_text(entry, "kind", label)
        if kind not in DAMAGE_KINDS:
            raise ContentError(f"{label}: damage step kind {kind!r} is not one of {DAMAGE_KINDS}")
        if kind == "absorb":
            if "stat" in entry:
                raise ContentError(f"{label}: an 'absorb' damage step takes no stat")
            stat = None
        else:
            stat = required_text(entry, "stat", label)
            if stat not in character.stats:
                raise ContentError(f"{label}: damage step names no stat ({stat!r})")
        steps.append(DamageStep(kind=kind, stat=stat))
    kinds = [step.kind for step in steps]
    if kinds.count("absorb") > 1 or kinds.count("overflow") > 1:
        raise ContentError(f"{label}: 'absorb' and 'overflow' may each appear at most once")
    if "absorb" in kinds and kinds[0] != "absorb":
        raise ContentError(f"{label}: 'absorb' must be the first damage step")
    if "overflow" in kinds and kinds[-1] != "overflow":
        raise ContentError(f"{label}: 'overflow' must be the last damage step")
    if "harm" not in kinds and "overflow" not in kinds:
        raise ContentError(f"{label}: 'damage' needs a 'harm' or 'overflow' step")
    stats = [step.stat for step in steps if step.stat is not None]
    if len(stats) != len(set(stats)):
        raise ContentError(f"{label}: each stat may be used by one damage step only")
    return tuple(steps)


def _hit_pool(combat: CombatRules | None) -> str | None:
    """The stat a hit's harm takes from, which is where a monster's hit points live."""
    if combat is None:
        return None
    return next((step.stat for step in combat.damage if step.kind == "harm"), None)


def _load_monster(
    definition: Definition,
    character: CharacterSpec,
    template: str,
    *,
    pool: str | None,
    conditions: Mapping[str, ConditionRules] | None = None,
    icon_entries: Mapping[str, tuple[str | None, str | None]] | None = None,
) -> MonsterTemplate:
    label = f"{template}: monster {definition.id}"
    data = definition.data
    check_keys(
        data,
        {
            "type",
            "id",
            "name",
            "description",
            "icon",
            "looks_like",
            *STATBLOCK_KEYS,
            "attacks_per_action",
            "large_group",
            "rules",
            "abilities",
        },
        label,
    )
    description = required_text(data, "description", label)
    attacks_per_action = data.get("attacks_per_action", 1)
    if (
        isinstance(attacks_per_action, bool)
        or not isinstance(attacks_per_action, int)
        or attacks_per_action < 1
    ):
        raise ContentError(f"{label}: 'attacks_per_action' must be a whole number, at least 1")
    if "attacks_per_action" not in data:
        match = ATTACKS_PER_ACTION_TEXT.search(description)
        if match is not None:
            raise ContentError(
                f"{label}: the description says {match.group(0)!r}; give the count as the "
                "structured 'attacks_per_action' field instead of relying on the prose"
            )
    stats = data.get("stats")
    allowed = set(character.stats)
    required = {stat_id for stat_id, stat in character.stats.items() if stat.derived is None}
    if not isinstance(stats, dict) or not required <= set(stats) or not set(stats) <= allowed:
        names = ", ".join(sorted(required))
        raise ContentError(f"{label}: 'stats' must give a whole number for each of: {names}")
    for key, value in stats.items():
        _check_whole(value, f"{label}: stat {key!r}")
    if pool is not None and stats[pool] < 1:
        raise ContentError(f"{label}: stat {pool!r} must be at least 1")
    armor = data.get("armor", 0)
    _check_whole(armor, f"{label}: 'armor'")
    attacks, attack_flags = _statblock_attacks(data.get("attacks", {}), label)
    attack_shock = _statblock_shock(data.get("attacks", {}), attacks, label)
    large_group = data.get("large_group", False)
    if not isinstance(large_group, bool):
        raise ContentError(f"{label}: 'large_group' must be true or false")
    rules, rule_text = _statblock_rules(data, label)
    abilities = _statblock_abilities(data.get("abilities", []), character, conditions or {}, label)
    icon, looks_like = _icon_fields(data, icon_entries or {}, FALLBACK_MONSTER, label)
    return MonsterTemplate(
        id=definition.id,
        name=required_text(data, "name", label),
        description=description,
        stats=dict(stats),
        armor=armor,
        attacks=attacks,
        attacks_per_action=attacks_per_action,
        large_group=large_group,
        rules=rules,
        attack_flags=attack_flags,
        attack_shock=attack_shock,
        rule_text=rule_text,
        abilities=abilities,
        icon=icon or FALLBACK_MONSTER,
        looks_like=looks_like,
    )


def _statblock_abilities(
    raw: object,
    character: CharacterSpec,
    conditions: Mapping[str, ConditionRules],
    label: str,
) -> tuple[MonsterAbility, ...]:
    """A statblock's special abilities as structured rules."""
    if not isinstance(raw, list):
        raise ContentError(f"{label}: 'abilities' must be a list")
    abilities: list[MonsterAbility] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise ContentError(f"{label}: each ability must be an object")
        check_keys(
            entry,
            {"kind", "name", "description", "trigger", "save", "condition", "damage"},
            f"{label}: ability",
        )
        kind = required_text(entry, "kind", f"{label}: ability")
        if kind not in MONSTER_ABILITY_KINDS:
            options = ", ".join(MONSTER_ABILITY_KINDS)
            raise ContentError(f"{label}: ability kind {kind!r} must be one of {options}")
        name = required_text(entry, "name", f"{label}: ability")
        description = required_text(entry, "description", f"{label}: ability")
        trigger = _optional_text(entry, "trigger", f"{label}: ability") or ""
        save = _optional_text(entry, "save", f"{label}: ability") or ""
        condition = _optional_text(entry, "condition", f"{label}: ability") or ""
        damage = _optional_text(entry, "damage", f"{label}: ability") or ""
        if save and save not in character.stats:
            raise ContentError(f"{label}: ability {name!r} names no save stat {save!r}")
        if condition and condition not in conditions:
            raise ContentError(f"{label}: ability {name!r} names no condition {condition!r}")
        if damage and not is_formula(damage):
            raise ContentError(f"{label}: ability {name!r} damage must be a dice formula")
        abilities.append(
            MonsterAbility(
                kind=kind,
                name=name,
                description=description,
                trigger=trigger,
                save=save,
                condition=condition,
                damage=damage,
            )
        )
    return tuple(abilities)


def _statblock_attacks(
    raw: object, label: str
) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    """A statblock's attacks. Each is its dice, either as plain text such as "1d8", or as an object
    with the dice and a list of flags, such as {"dice": "1d8", "flags": ["blast"]}. Returns the dice
    by attack name and the flags of the attacks that have any."""
    if not isinstance(raw, dict):
        raise ContentError(f"{label}: 'attacks' must be an object of attack names to dice")
    attacks: dict[str, str] = {}
    flags_by_attack: dict[str, tuple[str, ...]] = {}
    for attack_name, entry in raw.items():
        if not isinstance(attack_name, str) or not attack_name.strip():
            raise ContentError(f"{label}: attack names must be non-empty text")
        if isinstance(entry, str):
            formula, flags = entry, []
        elif (
            isinstance(entry, dict)
            and "dice" in entry
            and set(entry)
            <= {
                "dice",
                "flags",
                "shock",
                "shock_ac",
            }
        ):
            formula, flags = entry["dice"], entry.get("flags", [])
        else:
            raise ContentError(
                f"{label}: attack {attack_name!r} must be dice such as '1d8' or an object with "
                "'dice' and 'flags'"
            )
        if not is_formula(formula):
            raise ContentError(
                f"{label}: attack {attack_name!r} must be a dice formula such as '1d8'"
            )
        if not isinstance(flags, list) or not all(isinstance(f, str) for f in flags):
            raise ContentError(f"{label}: attack {attack_name!r} 'flags' must be a list of names")
        unknown = [f for f in flags if f not in ATTACK_FLAGS]
        if unknown or len(set(flags)) != len(flags):
            options = ", ".join(ATTACK_FLAGS)
            raise ContentError(
                f"{label}: attack {attack_name!r} has flags {flags}; each flag must be listed "
                f"once, from: {options}"
            )
        attacks[attack_name] = formula
        if flags:
            flags_by_attack[attack_name] = tuple(flags)
    return attacks, flags_by_attack


def _statblock_shock(
    raw: object, attacks: Mapping[str, str], label: str
) -> dict[str, tuple[int, int]]:
    """The Shock of a statblock's attacks: the damage done on a miss and the highest armor class
    it reaches. Only attacks written as objects can have it."""
    if not isinstance(raw, dict):
        return {}
    shock: dict[str, tuple[int, int]] = {}
    for attack_name, entry in raw.items():
        if not isinstance(entry, dict) or ("shock" not in entry and "shock_ac" not in entry):
            continue
        value, reaches = entry.get("shock", 0), entry.get("shock_ac", 0)
        for key, amount in (("shock", value), ("shock_ac", reaches)):
            if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
                raise ContentError(
                    f"{label}: attack {attack_name!r} {key!r} must be a whole number, 0 or more"
                )
        if value and not reaches:
            raise ContentError(
                f"{label}: attack {attack_name!r} 'shock' needs the 'shock_ac' it reaches"
            )
        shock[attack_name] = (value, reaches)
    return shock


def _statblock_rules(data: Mapping[str, Any], label: str) -> tuple[tuple[str, ...], dict[str, str]]:
    """A statblock's rules. Each is either the id of a rule in the rule set, or an object with an
    'id' and 'text' written into the statblock. Returns every id, and the text of the written ones."""
    entries = data.get("rules", [])
    if not isinstance(entries, list):
        raise ContentError(f"{label}: 'rules' must be a list")
    ids: list[str] = []
    texts: dict[str, str] = {}
    for entry in entries:
        if isinstance(entry, str) and entry.strip():
            ids.append(entry)
        elif (
            isinstance(entry, dict)
            and set(entry) == {"id", "text"}
            and isinstance(entry["id"], str)
            and entry["id"].strip()
            and isinstance(entry["text"], str)
            and entry["text"].strip()
        ):
            ids.append(entry["id"])
            texts[entry["id"]] = entry["text"]
        else:
            raise ContentError(
                f"{label}: each rule must be a rule id, or an object with an 'id' and 'text'"
            )
    if len(set(ids)) != len(ids):
        raise ContentError(f"{label}: 'rules' names a rule more than once")
    return tuple(ids), texts


def _attached_rules(monsters: Iterable[MonsterTemplate], label: str) -> dict[str, str]:
    """The text of every rule written into a statblock, by id. A rule written into two statblocks
    must have the same text in both."""
    texts: dict[str, str] = {}
    for monster in monsters:
        for rule_id, text in monster.rule_text.items():
            if texts.setdefault(rule_id, text) != text:
                raise ContentError(
                    f"{label}: rule {rule_id!r} is written differently in two statblocks"
                )
    return texts


def _check_whole(value: object, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ContentError(f"{label} must be a whole number, at least 0")


def _load_grid(definition: Definition, template: str) -> EncounterGrid:
    label = f"{template}: encounter grid {definition.id}"
    check_keys(definition.data, {"type", "id", "cell_feet", "move_feet"}, label)
    cell_feet = definition.data.get("cell_feet")
    move_feet = definition.data.get("move_feet")
    for key, value in (("cell_feet", cell_feet), ("move_feet", move_feet)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ContentError(f"{label}: {key!r} must be a whole number of feet, at least 1")
    if move_feet < cell_feet:
        raise ContentError(f"{label}: 'move_feet' must be at least one cell")
    return EncounterGrid(cell_feet=cell_feet, move_feet=move_feet)


MAX_TEMPLATE_SIZE = 60


_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


def _color(data: Mapping[str, Any], label: str, key: str = "color") -> str:
    color = required_text(data, key, label)
    if not _HEX_COLOR.fullmatch(color):
        raise ContentError(f"{label}: {key} must be a hex colour like #3a3f47, not {color!r}")
    return color


def _terrain_pattern(data: Mapping[str, Any], label: str) -> tuple[str | None, str | None]:
    if "pattern" not in data:
        return None, None
    pattern = required_text(data, "pattern", label)
    if pattern not in TERRAIN_PATTERNS:
        allowed = ", ".join(TERRAIN_PATTERNS)
        raise ContentError(f"{label}: pattern must be one of {allowed}, not {pattern!r}")
    return pattern, _color(data, label, "ink")


def _encounter_texture(data: Mapping[str, Any], label: str) -> tuple[str | None, str | None]:
    """A local terrain's optional texture. A pattern needs an ink colour; ink without a
    pattern would never be drawn, so it is refused rather than ignored."""
    if "ink" in data and "pattern" not in data:
        raise ContentError(f"{label}: 'ink' needs a 'pattern'")
    return _terrain_pattern(data, label) if "pattern" in data else (None, None)


def _optional_color(data: Mapping[str, Any], key: str, label: str) -> str:
    """A hex colour, or "" when the key is absent or empty."""
    if key not in data or data[key] == "":
        return ""
    return _color(data, label, key)


def _style_choice(data: Mapping[str, Any], key: str, allowed: Sequence[str], label: str) -> str:
    """A palette or ambient id from `allowed`, or "" when none is given."""
    value = data.get(key, "")
    if value == "":
        return ""
    if not isinstance(value, str) or value not in allowed:
        raise ContentError(f"{label}: {key!r} must be one of {', '.join(allowed)}, not {value!r}")
    return value


def _load_encounter_terrain(
    definitions: list[Definition], template: str
) -> dict[str, EncounterTerrain]:
    terrain: dict[str, EncounterTerrain] = {}
    for d in definitions:
        if d.type != "encounter_terrain":
            continue
        if d.id in terrain:
            raise ContentError(f"{template}: duplicate encounter terrain {d.id!r}")
        label = f"{template}: encounter terrain {d.id}"
        check_keys(
            d.data,
            {
                "type",
                "id",
                "name",
                "color",
                "rule",
                "move_cost",
                "difficult",
                "pattern",
                "ink",
                "category",
            },
            label,
        )
        pattern, ink = _encounter_texture(d.data, label)
        terrain[d.id] = EncounterTerrain(
            id=d.id,
            name=required_text(d.data, "name", label),
            color=_color(d.data, label),
            rule=required_text(d.data, "rule", label),
            move_cost=_terrain_move_cost(d.data, label),
            pattern=pattern,
            ink=ink,
            category=_optional_category(d.data, label),
        )
    return terrain


def _optional_category(data: Mapping[str, Any], label: str) -> str:
    if "category" not in data:
        return ""
    return required_text(data, "category", label)


def _terrain_move_cost(data: Mapping[str, Any], label: str) -> int:
    """The movement one cell of ground costs. A "difficult": true flag is the older way to say
    2, and is used only when "move_cost" is left out."""
    if "move_cost" in data:
        return _whole(data, "move_cost", label, 1)
    return 2 if _flag(data, "difficult", label) else 1


def _load_world_terrain(definitions: list[Definition], template: str) -> dict[str, WorldTerrain]:
    terrain: dict[str, WorldTerrain] = {}
    for d in definitions:
        if d.type != "world_terrain":
            continue
        if d.id in terrain:
            raise ContentError(f"{template}: duplicate world terrain {d.id!r}")
        label = f"{template}: world terrain {d.id}"
        check_keys(
            d.data,
            {"type", "id", "name", "color", "rule", "pattern", "ink"},
            label,
        )
        pattern, ink = _terrain_pattern(d.data, label)
        terrain[d.id] = WorldTerrain(
            id=d.id,
            name=required_text(d.data, "name", label),
            color=_color(d.data, label),
            rule=required_text(d.data, "rule", label),
            pattern=pattern,
            ink=ink,
        )
    return terrain


def _load_encounter_objects(
    definitions: list[Definition], template: str
) -> dict[str, EncounterObject]:
    objects: dict[str, EncounterObject] = {}
    for d in definitions:
        if d.type != "encounter_object":
            continue
        if d.id in objects:
            raise ContentError(f"{template}: duplicate encounter object {d.id!r}")
        label = f"{template}: encounter object {d.id}"
        check_keys(
            d.data,
            {"type", "id", "name", "icon", "color", "blocks", "solid", "category", "description"},
            label,
        )
        objects[d.id] = EncounterObject(
            id=d.id,
            name=required_text(d.data, "name", label),
            icon=check_icon(required_text(d.data, "icon", label), label),
            color=_color(d.data, label),
            blocks=_flag(d.data, "blocks", label),
            solid=_flag(d.data, "solid", label),
            description=_optional_text(d.data, "description", label) or "",
            category=_optional_category(d.data, label),
        )
    return objects


def _load_encounter_templates(
    definitions: list[Definition],
    terrain: Mapping[str, EncounterTerrain],
    objects: Mapping[str, EncounterObject],
    template: str,
) -> dict[str, EncounterTemplate]:
    templates: dict[str, EncounterTemplate] = {}
    for d in definitions:
        if d.type != "encounter_template":
            continue
        if d.id in templates:
            raise ContentError(f"{template}: duplicate encounter template {d.id!r}")
        label = f"{template}: encounter template {d.id}"
        check_keys(
            d.data,
            {
                "type",
                "id",
                "name",
                "columns",
                "rows",
                "background",
                "fills",
                "cells",
                "palette",
                "ambient",
                "background_color",
                "grid_color",
                "description",
            },
            label,
        )
        columns = _map_size(d.data, "columns", label)
        rows = _map_size(d.data, "rows", label)
        templates[d.id] = EncounterTemplate(
            id=d.id,
            name=required_text(d.data, "name", label),
            columns=columns,
            rows=rows,
            background=_defined(
                d.data.get("background"), terrain, "background", "terrain", label, empty_ok=True
            ),
            cells=_paint(d.data, label, columns, rows, terrain, objects),
            palette=_style_choice(d.data, "palette", MAP_PALETTES, label),
            ambient=_style_choice(d.data, "ambient", MAP_AMBIENTS, label),
            background_color=_optional_color(d.data, "background_color", label),
            grid_color=_optional_color(d.data, "grid_color", label),
            description=_optional_text(d.data, "description", label) or "",
        )
    return templates


def _map_size(data: Mapping[str, Any], key: str, label: str) -> int:
    value = data.get(key)
    if not _is_int(value) or not 1 <= value <= MAX_TEMPLATE_SIZE:
        raise ContentError(f"{label}: {key!r} must be a whole number from 1 to {MAX_TEMPLATE_SIZE}")
    return value


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _defined(
    value: Any,
    known: Mapping[str, Any],
    key: str,
    kind: str,
    label: str,
    empty_ok: bool = False,
) -> str:
    """An id that must name a defined `kind`. An empty id means none, where `empty_ok` allows it."""
    if empty_ok and value == "":
        return ""
    if not isinstance(value, str) or value not in known:
        raise ContentError(f"{label}: {key!r} must name a defined {kind}, not {value!r}")
    return value


def _entries(data: Mapping[str, Any], key: str, label: str) -> list[Mapping[str, Any]]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, Mapping) for v in value):
        raise ContentError(f"{label}: {key!r} must be a list of objects")
    return value


def _position(column: Any, row: Any, columns: int, rows: int, label: str) -> tuple[int, int]:
    if not (_is_int(column) and _is_int(row)):
        raise ContentError(f"{label}: a position must be a [column, row] pair of whole numbers")
    if not (0 <= column < columns and 0 <= row < rows):
        raise ContentError(
            f"{label}: position [{column}, {row}] is outside the {columns}x{rows} grid"
        )
    return column, row


def _corner(
    entry: Mapping[str, Any], key: str, columns: int, rows: int, label: str
) -> tuple[int, int]:
    value = entry.get(key)
    if not isinstance(value, list) or len(value) != 2:
        raise ContentError(f"{label}: {key!r} must be a [column, row] pair")
    return _position(value[0], value[1], columns, rows, label)


def _paint(
    data: Mapping[str, Any],
    label: str,
    columns: int,
    rows: int,
    terrain: Mapping[str, EncounterTerrain],
    objects: Mapping[str, EncounterObject],
) -> tuple[EncounterCell, ...]:
    """Expand a template's fills and cells into the cells that hold terrain or an object.

    Each cell starts with the background and no object. A fill or cell changes only the keys it
    gives, and later entries overwrite earlier ones.
    """
    background = _defined(
        data.get("background"), terrain, "background", "terrain", label, empty_ok=True
    )
    state = {
        (column, row): {"terrain": background, "object": ""}
        for column in range(columns)
        for row in range(rows)
    }
    for fill in _entries(data, "fills", label):
        fill_label = f"{label}: fill"
        check_keys(fill, {"from", "to", "terrain", "object"}, fill_label)
        first = _corner(fill, "from", columns, rows, fill_label)
        last = _corner(fill, "to", columns, rows, fill_label)
        values = _changes(fill, terrain, objects, fill_label)
        for column in range(min(first[0], last[0]), max(first[0], last[0]) + 1):
            for row in range(min(first[1], last[1]), max(first[1], last[1]) + 1):
                state[column, row].update(values)
    for cell in _entries(data, "cells", label):
        cell_label = f"{label}: cell"
        check_keys(cell, {"column", "row", "terrain", "object"}, cell_label)
        position = _position(cell.get("column"), cell.get("row"), columns, rows, cell_label)
        state[position].update(_changes(cell, terrain, objects, cell_label))
    painted = []
    for row in range(rows):
        for column in range(columns):
            values = state[column, row]
            if values["terrain"] or values["object"]:
                painted.append(
                    EncounterCell(
                        column=column,
                        row=row,
                        terrain=values["terrain"],
                        object=values["object"],
                    )
                )
    return tuple(painted)


def _changes(
    entry: Mapping[str, Any],
    terrain: Mapping[str, EncounterTerrain],
    objects: Mapping[str, EncounterObject],
    label: str,
) -> dict[str, str]:
    """The terrain and object an entry sets. It must set at least one."""
    changes: dict[str, str] = {}
    if "terrain" in entry:
        changes["terrain"] = _defined(
            entry["terrain"], terrain, "terrain", "terrain", label, empty_ok=True
        )
    if "object" in entry:
        changes["object"] = _defined(
            entry["object"], objects, "object", "encounter object", label, empty_ok=True
        )
    if not changes:
        raise ContentError(f"{label}: must set 'terrain' or 'object'")
    return changes


def _single_optional(definitions: list[Definition], kind: str, template: str) -> Definition | None:
    found = [d for d in definitions if d.type == kind]
    if len(found) > 1:
        raise ContentError(f"{template}: more than one {kind} definition")
    return found[0] if found else None


def _load_checklist(definitions: list[Definition], template: str) -> ChecklistRules | None:
    """The rule set's single `checklist` definition: the areas its state step must check, in
    order. An item with a `group` joins the checklist only when the rule set offers a tool in
    that optional group. The group names come from the engine's tool policy, so the import is
    lazy to keep this module's import order clean."""
    definition = _single_optional(definitions, "checklist", template)
    if definition is None:
        return None
    label = f"{template}: checklist {definition.id}"
    check_keys(definition.data, {"type", "id", "items"}, label)
    raw_items = definition.data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ContentError(f"{label}: 'items' must be a non-empty list")
    from cygnusloom.services.tools import TOOL_GROUPS

    items: list[ChecklistItem] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_items):
        item_label = f"{label}: items[{index}]"
        if not isinstance(raw, Mapping):
            raise ContentError(f"{item_label}: must be an object")
        item = check_object_keys(raw, {"id", "text", "group"}, item_label)
        ident = required_text(item, "id", item_label)
        if not CHECKLIST_KEY.fullmatch(ident):
            raise ContentError(
                f"{item_label}: 'id' must be lowercase letters, digits, '_' or '-', starting "
                "with a letter"
            )
        if ident in seen:
            raise ContentError(f"{item_label}: duplicate id {ident!r}")
        seen.add(ident)
        group = _optional_text(item, "group", item_label) or ""
        if group and group not in TOOL_GROUPS:
            groups = ", ".join(sorted(TOOL_GROUPS))
            raise ContentError(f"{item_label}: 'group' must be one of {groups}")
        items.append(
            ChecklistItem(id=ident, text=required_text(item, "text", item_label), group=group)
        )
    return ChecklistRules(items=tuple(items))


def _load_prompts(
    definitions: list[Definition],
    template: str,
    *,
    capabilities: Capabilities | None = None,
) -> tuple[PromptFragment, ...]:
    """The `prompt` definitions, in file order. Each is a sentence group the GM prompt adds. A
    fragment may `supersede` another by id, so a rule set's own setting instructions can replace
    core's. Superseded fragments are left out of the final list."""
    prompt_definitions = [d for d in definitions if d.type == "prompt"]
    prompt_ids: set[str] = set()
    for d in prompt_definitions:
        if d.id in prompt_ids:
            raise ContentError(f"{template}: duplicate prompt {d.id!r}")
        prompt_ids.add(d.id)
    supersedes_by_id: dict[str, tuple[str, ...]] = {}
    superseded: set[str] = set()
    for d in prompt_definitions:
        raw = d.data.get("supersedes", [])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw):
            raise ContentError(
                f"{template}: prompt {d.id!r}: 'supersedes' must be a list of prompt ids"
            )
        unknown = [item for item in raw if item not in prompt_ids]
        if unknown:
            raise ContentError(
                f"{template}: prompt {d.id!r} supersedes unknown prompt(s) "
                f"{', '.join(repr(item) for item in unknown)}"
            )
        supersedes_by_id[d.id] = tuple(raw)
        superseded.update(raw)
    fragments = []
    for d in prompt_definitions:
        if d.id in superseded:
            continue
        label = f"{template}: prompt {d.id!r}"
        check_keys(
            d.data,
            {
                "type",
                "id",
                "text",
                "step",
                "unwritten_only",
                "requires",
                "enabled_by",
                "supersedes",
            },
            label,
        )
        step = d.data.get("step", "state")
        if step not in ("state", "narration", "all"):
            raise ContentError(f"{label}: 'step' must be state, narration, or all")
        requires, enabled_by = _read_gate(d.data, label)
        _check_definition_gate(capabilities, requires, enabled_by, label)
        fragments.append(
            PromptFragment(
                id=d.id,
                text=required_text(d.data, "text", label),
                step=step,
                unwritten_only=_flag(d.data, "unwritten_only", label),
                requires=requires,
                enabled_by=enabled_by,
                supersedes=supersedes_by_id.get(d.id, ()),
            )
        )
    return tuple(fragments)


def _load_sentence_cap(definitions: list[Definition], template: str) -> int | None:
    """The speech sentence cap from the `narration` definition, or None when there is none."""
    head = _single_optional(definitions, "narration", template)
    if head is None:
        return None
    label = f"{template}: narration"
    check_keys(head.data, {"type", "id", "sentence_cap"}, label)
    return _whole(head.data, "sentence_cap", label, minimum=1)


def _whole(data: Mapping[str, Any], key: str, label: str, minimum: int) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ContentError(f"{label}: {key!r} must be a whole number, at least {minimum}")
    return value


def _number(data: Mapping[str, Any], key: str, label: str, minimum: float) -> float:
    value = data.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < minimum:
        raise ContentError(f"{label}: {key!r} must be a number, at least {minimum}")
    return float(value)


def _names(data: Mapping[str, Any], key: str, label: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ContentError(f"{label}: {key!r} must be a list of non-empty strings")
    return tuple(value)


HOUSEKEEPING_PLACEHOLDERS = frozenset({"place", "names", "count", "days"})


def _load_housekeeping(definitions: list[Definition], template: str) -> HousekeepingRules | None:
    """The rule set's scene housekeeping tasks. Each task may name only the placeholders the
    engine can fill, so a typo is a content error rather than text the GM sees."""
    definition = _single_optional(definitions, "housekeeping", template)
    if definition is None:
        return None
    label = f"{template}: housekeeping {definition.id}"
    check_keys(definition.data, {"type", "id", "bodies", "fight_debris"}, label)

    def task_rules(key: str) -> HousekeepingTaskRules | None:
        data = definition.data.get(key)
        if data is None:
            return None
        sublabel = f"{label}: {key}"
        if not isinstance(data, Mapping):
            raise ContentError(f"{sublabel}: must be an object")
        check_keys(data, {"after_days", "task"}, sublabel)
        after_days = _whole(data, "after_days", sublabel, 0)
        text = required_text(data, "task", sublabel)
        placeholders = set(re.findall(r"\{([^{}]+)\}", text))
        unknown = placeholders - HOUSEKEEPING_PLACEHOLDERS
        if unknown:
            names = ", ".join(sorted(repr(item) for item in unknown))
            allowed = ", ".join(sorted(HOUSEKEEPING_PLACEHOLDERS))
            raise ContentError(f"{sublabel}: unknown placeholder(s) {names}; use {allowed}")
        return HousekeepingTaskRules(after_days=after_days, task=text)

    bodies = task_rules("bodies")
    fight_debris = task_rules("fight_debris")
    if bodies is None and fight_debris is None:
        raise ContentError(f"{label}: give 'bodies', 'fight_debris', or both")
    return HousekeepingRules(bodies=bodies, fight_debris=fight_debris)


def _load_clock(definitions: list[Definition], template: str) -> ClockRules | None:
    definition = _single_optional(definitions, "clock", template)
    if definition is None:
        return None
    label = f"{template}: clock {definition.id}"
    check_keys(
        definition.data,
        {"type", "id", "parts", "stale_days", "wage_currency", "hours_per_part", "week_days"},
        label,
    )
    parts = _names(definition.data, "parts", label)
    if not parts or len(set(parts)) != len(parts):
        raise ContentError(f"{label}: 'parts' must be a non-empty list of distinct names")
    stale_days = _whole(definition.data, "stale_days", label, 1)
    wage_currency = definition.data.get("wage_currency", "")
    if not isinstance(wage_currency, str):
        raise ContentError(f"{label}: 'wage_currency' must be text")
    hours_per_part = (
        _whole(definition.data, "hours_per_part", label, 1)
        if "hours_per_part" in definition.data
        else 1
    )
    week_days = (
        _whole(definition.data, "week_days", label, 1) if "week_days" in definition.data else 7
    )
    return ClockRules(
        parts=parts,
        stale_days=stale_days,
        wage_currency=wage_currency,
        hours_per_part=hours_per_part,
        week_days=week_days,
    )


def _load_rests(
    definitions: list[Definition],
    character: CharacterSpec,
    template: str,
    clock: ClockRules | None,
    condition_ends: Sequence[str] = (),
) -> dict[str, RestLength]:
    """The rest lengths of the rule set's single rest definition, by id. Each needs `full` and at
    least one of `parts`, `days`, or `until: "morning"`. `ends` names the condition-ending
    triggers the rest counts as, and must come from the rule set's `condition_ends`. `stop_at`
    maps a part of the day to the part a rest begun in it ends at instead, and needs `until` and
    a clock."""
    definition = _single_optional(definitions, "rest", template)
    if definition is None:
        return {}
    label = f"{template}: rest {definition.id}"
    check_keys(definition.data, {"type", "id", "lengths", "interrupted"}, label)
    lengths = definition.data.get("lengths")
    if not isinstance(lengths, Mapping) or not lengths:
        raise ContentError(f"{label}: 'lengths' must be a non-empty object")
    loaded: dict[str, RestLength] = {}
    for length_id, data in lengths.items():
        length_label = f"{label}: length {length_id}"
        if not isinstance(data, Mapping):
            raise ContentError(f"{length_label} must be an object")
        check_keys(
            data,
            {
                "hours",
                "parts",
                "days",
                "until",
                "stop_at",
                "full",
                "restores",
                "clears",
                "ends",
                "upkeep",
            },
            length_label,
        )
        parts = _whole(data, "parts", length_label, 0) if "parts" in data else 0
        days = _whole(data, "days", length_label, 0) if "days" in data else 0
        hours = _whole(data, "hours", length_label, 0) if "hours" in data else 0
        until = data.get("until")
        if until is not None and until != "morning":
            raise ContentError(f"{length_label}: 'until' must be \"morning\"")
        if not (hours or parts or days or until):
            raise ContentError(f"{length_label}: needs 'hours', 'parts', 'days', or 'until'")
        stop_at = _stop_at(data, length_label, clock, bool(until))
        full = data.get("full")
        if not isinstance(full, bool):
            raise ContentError(f"{length_label}: 'full' must be true or false")
        restores = _names(data, "restores", length_label)
        for stat in restores:
            if stat not in character.stats:
                raise ContentError(f"{length_label}: 'restores' names no stat ({stat!r})")
        ends = _names(data, "ends", length_label)
        if condition_ends:
            unknown = [end for end in ends if end not in condition_ends]
            if unknown:
                allowed = ", ".join(condition_ends)
                raise ContentError(
                    f"{length_label}: 'ends' must list only the rule set's condition triggers: "
                    f"{allowed}"
                )
        upkeep = data.get("upkeep", False)
        if not isinstance(upkeep, bool):
            raise ContentError(f"{length_label}: 'upkeep' must be true or false")
        loaded[length_id] = RestLength(
            id=length_id,
            hours=hours,
            parts=parts,
            days=days,
            until_morning=until == "morning",
            full=full,
            restores=restores,
            clears=_names(data, "clears", length_label),
            ends=ends,
            upkeep=upkeep,
            stop_at=stop_at,
        )
    return loaded


def _stop_at(
    data: Mapping[str, Any], label: str, clock: ClockRules | None, until: bool
) -> dict[str, str]:
    """The `stop_at` map of a rest length. Each key and value is a part of the day, and it needs a
    rest that runs `until` morning."""
    if "stop_at" not in data:
        return {}
    stops = data["stop_at"]
    if not until:
        raise ContentError(f"{label}: 'stop_at' needs 'until': \"morning\"")
    if clock is None or not isinstance(stops, Mapping):
        raise ContentError(f"{label}: 'stop_at' must be an object of parts of the day")
    for start, stop in stops.items():
        if start not in clock.parts or stop not in clock.parts:
            raise ContentError(f"{label}: 'stop_at' names no part of the day ({start!r}: {stop!r})")
    return {str(start): str(stop) for start, stop in stops.items()}


def _load_interrupted_rest(
    definitions: list[Definition], rests: Mapping[str, RestLength], template: str
) -> str:
    """The rest length an interrupted rest counts as. It must be a non-full rest length."""
    definition = _single_optional(definitions, "rest", template)
    if definition is None:
        return ""
    label = f"{template}: rest {definition.id}"
    name = definition.data.get("interrupted")
    if name not in rests:
        raise ContentError(f"{label}: 'interrupted' must name a rest length")
    if rests[name].full:
        raise ContentError(f"{label}: 'interrupted' must name a rest length that is not full")
    return name


def _load_inventory(
    definitions: list[Definition],
    character: CharacterSpec,
    item_sizes: Mapping[str, int],
    template: str,
) -> InventoryRules | None:
    definition = _single_optional(definitions, "inventory", template)
    if definition is None:
        return None
    label = f"{template}: inventory {definition.id}"
    check_keys(
        definition.data,
        {
            "type",
            "id",
            "capacity",
            "penalty_name",
            "penalty_lasts",
            "penalty_item",
            "penalty_slots",
            "full_stat",
            "capacity_stat",
            "capacity_multiplier",
            "readied_stat",
            "readied_divisor",
            "pools",
            "size_pools",
            "default_pool",
        },
        label,
    )
    full_stat = _optional_text(definition.data, "full_stat", label) or ""
    if full_stat and full_stat not in character.stats:
        raise ContentError(f"{label}: 'full_stat' names no stat ({full_stat!r})")
    capacity_stat = _optional_text(definition.data, "capacity_stat", label) or ""
    if capacity_stat and capacity_stat not in character.stats:
        raise ContentError(f"{label}: 'capacity_stat' names no stat ({capacity_stat!r})")
    pools = _load_inventory_pools(definition.data, character, label)
    size_pools = _load_size_pools(definition.data, item_sizes, pools, label)
    default_pool = _optional_nonempty_text(definition.data, "default_pool", label) or ""
    pool_ids = {pool.id for pool in pools}
    if pools and not default_pool:
        raise ContentError(f"{label}: 'pools' needs a 'default_pool' naming a pool")
    if default_pool and not pools:
        raise ContentError(f"{label}: 'default_pool' needs 'pools'")
    if default_pool and default_pool not in pool_ids:
        raise ContentError(f"{label}: 'default_pool' names no pool ({default_pool!r})")
    readied = [pool.id for pool in pools if pool.ready]
    if len(readied) > 1:
        names = ", ".join(repr(pool_id) for pool_id in readied)
        raise ContentError(f"{label}: at most one pool may be readied, but {names} are")
    # A single-pool rule set still names its capacity; named pools carry their own.
    capacity = 0
    if "capacity" in definition.data or not pools:
        capacity = _whole(definition.data, "capacity", label, 1)
    return InventoryRules(
        capacity=capacity,
        penalty_name=required_text(definition.data, "penalty_name", label),
        penalty_lasts=required_text(definition.data, "penalty_lasts", label),
        penalty_item=required_text(definition.data, "penalty_item", label),
        penalty_slots=_whole(definition.data, "penalty_slots", label, 0),
        full_stat=full_stat,
        capacity_stat=capacity_stat,
        capacity_multiplier=_number(definition.data, "capacity_multiplier", label, 1.0)
        if "capacity_multiplier" in definition.data
        else 1.0,
        readied_stat=_optional_nonempty_text(definition.data, "readied_stat", label) or "",
        readied_divisor=_whole(definition.data, "readied_divisor", label, 1)
        if "readied_divisor" in definition.data
        else 1,
        pools=pools,
        size_pools=size_pools,
        default_pool=default_pool,
    )


INVENTORY_POOL_KEYS = frozenset(
    {"id", "name", "ready", "capacity", "stat", "multiplier", "divisor", "rounding", "base"}
)
# The derived capacity arithmetic: the character's raw score times `multiplier`, plus `base`,
# divided by `divisor`, and rounded up or down.
POOL_ROUNDINGS = ("down", "up")


def _load_inventory_pools(
    data: Mapping[str, Any], character: CharacterSpec, label: str
) -> tuple[InventoryPool, ...]:
    """The named pools. Each has a fixed `capacity` or a `stat` whose score derives it, and at
    most one pool is `ready`."""
    raw = data.get("pools", [])
    if not isinstance(raw, list):
        raise ContentError(f"{label}: 'pools' must be a list of pool objects")
    pools: list[InventoryPool] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        pool = check_object_keys(entry, INVENTORY_POOL_KEYS, f"{label}: pool {index}")
        ident = required_text(pool, "id", f"{label}: pool {index}")
        pool_label = f"{label}: pool {ident!r}"
        if ident in seen:
            raise ContentError(f"{label}: duplicate pool id {ident!r}")
        seen.add(ident)
        name = required_text(pool, "name", pool_label)
        if "capacity" in pool:
            for key in ("stat", "multiplier", "divisor", "rounding", "base"):
                if key in pool:
                    raise ContentError(
                        f"{pool_label}: a fixed 'capacity' takes no {key!r}; derive it from a stat"
                    )
            pools.append(
                InventoryPool(
                    id=ident,
                    name=name,
                    ready=_flag(pool, "ready", pool_label),
                    capacity=_whole(pool, "capacity", pool_label, 0),
                )
            )
            continue
        stat = required_text(pool, "stat", pool_label)
        if stat not in character.stats:
            raise ContentError(f"{pool_label}: 'stat' names no stat ({stat!r})")
        multiplier = _number(pool, "multiplier", pool_label, 0.0) if "multiplier" in pool else 1.0
        if multiplier <= 0:
            raise ContentError(f"{pool_label}: 'multiplier' must be greater than zero")
        rounding = pool.get("rounding", "down")
        if rounding not in POOL_ROUNDINGS:
            raise ContentError(
                f"{pool_label}: 'rounding' must be one of {', '.join(POOL_ROUNDINGS)}"
            )
        pools.append(
            InventoryPool(
                id=ident,
                name=name,
                ready=_flag(pool, "ready", pool_label),
                stat=stat,
                multiplier=multiplier,
                divisor=_whole(pool, "divisor", pool_label, 1) if "divisor" in pool else 1,
                rounding=rounding,
                base=_whole(pool, "base", pool_label, 0) if "base" in pool else 0,
            )
        )
    return tuple(pools)


def _load_size_pools(
    data: Mapping[str, Any],
    item_sizes: Mapping[str, int],
    pools: Sequence[InventoryPool],
    label: str,
) -> dict[str, str]:
    """The pool each item size occupies, checked against the rule set's item sizes and pools."""
    raw = data.get("size_pools", {})
    if not isinstance(raw, Mapping):
        raise ContentError(f"{label}: 'size_pools' must map item size ids to pool ids")
    if raw and not pools:
        raise ContentError(f"{label}: 'size_pools' needs 'pools'")
    pool_ids = {pool.id for pool in pools}
    size_pools: dict[str, str] = {}
    for size, pool_id in raw.items():
        entry_label = f"{label}: size pool {size!r}"
        if size not in item_sizes:
            raise ContentError(f"{entry_label}: names no item size ({size!r})")
        if not isinstance(pool_id, str) or not pool_id.strip():
            raise ContentError(f"{entry_label}: must name a pool")
        if pool_id not in pool_ids:
            raise ContentError(f"{entry_label}: names no pool ({pool_id!r})")
        size_pools[str(size)] = pool_id
    return size_pools


def _apply_item_sizes(definitions: list[Definition], value: object, label: str) -> list[Definition]:
    """Expand the rule-set head's `item_sizes` map into `item_size` definitions. An explicit
    definition of the same id wins."""
    if value is None:
        return definitions
    if not isinstance(value, Mapping) or not value:
        raise ContentError(f"{label}: 'item_sizes' must map ids to slot counts")
    existing = {d.id for d in definitions if d.type == "item_size"}
    generated: list[Definition] = []
    for name, slots in value.items():
        size_label = f"{label}: item size {name!r}"
        if not isinstance(name, str) or not name.strip():
            raise ContentError(f"{label}: 'item_sizes' ids must be non-empty text")
        if not isinstance(slots, int) or isinstance(slots, bool) or slots < 0:
            raise ContentError(f"{size_label}: 'slots' must be a whole number, zero or more")
        if name in existing:
            continue
        generated.append(
            Definition("item_size", name, {"type": "item_size", "id": name, "slots": slots})
        )
    return [*generated, *definitions]


def _load_item_sizes(definitions: list[Definition], template: str) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for d in definitions:
        if d.type != "item_size":
            continue
        label = f"{template}: item size {d.id}"
        check_keys(d.data, {"type", "id", "slots"}, label)
        sizes[d.id] = _whole(d.data, "slots", label, 0)
    return sizes


def _load_magic(
    definitions: list[Definition], item_sizes: Mapping[str, int], template: str
) -> MagicRules | None:
    definition = _single_optional(definitions, "magic", template)
    if definition is None:
        return None
    label = f"{template}: magic {definition.id}"
    check_keys(
        definition.data,
        {
            "type",
            "id",
            "model",
            "item",
            "prepared",
            "move",
            "reusable",
            "single_use",
            "charged",
            "cast_penalty",
        },
        label,
    )
    model = definition.data.get("model", "item")
    if model not in ("item", "prepared", "move"):
        raise ContentError(f"{label}: 'model' must be item, prepared, or move")
    if model == "item":
        if "item" in definition.data:
            item_data = check_object_keys(
                definition.data["item"],
                {"reusable", "single_use", "charged", "cast_penalty"},
                f"{label}: item",
            )
        else:
            item_data = definition.data
        reusable = _load_item_kind(item_data, "reusable", label, item_sizes, sized=True)
        single_use = _load_item_kind(item_data, "single_use", label, item_sizes, sized=True)
        charged = _load_item_kind(item_data, "charged", label, item_sizes, sized=False)
        if len({reusable.kind, single_use.kind, charged.kind}) != 3:
            raise ContentError(f"{label}: magic item roles must use distinct kinds")
        return MagicRules(
            model="item",
            item=ItemMagic(
                reusable=reusable,
                single_use=single_use,
                charged=charged,
                cast_penalty=_whole(item_data, "cast_penalty", label, 0),
            ),
        )
    if model == "prepared":
        prepared_data = check_object_keys(
            definition.data.get("prepared", {}),
            {"castings_stat", "prepared_mark", "effort_mark"},
            f"{label}: prepared",
        )
        return MagicRules(
            model="prepared",
            prepared=PreparedMagic(
                castings_stat=_optional_text(prepared_data, "castings_stat", label) or "castings",
                prepared_mark=_optional_text(prepared_data, "prepared_mark", label) or "prepared",
                effort_mark=_optional_text(prepared_data, "effort_mark", label)
                or "effort_committed",
            ),
        )
    move_data = check_object_keys(
        definition.data.get("move", {}), {"move", "spell_field"}, f"{label}: move"
    )
    return MagicRules(
        model="move",
        move=MoveMagic(
            move=_optional_text(move_data, "move", label) or "cast_a_spell",
            spell_field=_optional_text(move_data, "spell_field", label) or "spell",
        ),
    )


def _load_item_kind(
    data: Mapping[str, Any],
    role: str,
    label: str,
    item_sizes: Mapping[str, int],
    *,
    sized: bool,
) -> ItemKind:
    """One magic item role: an object with a 'kind', and a 'size' naming an item size when `sized`."""
    entry = data.get(role)
    if not isinstance(entry, Mapping):
        raise ContentError(f"{label}: {role!r} must be an object with a 'kind'")
    role_label = f"{label} {role}"
    allowed = {"kind", "size", "icon"} if sized else {"kind", "icon"}
    check_keys(entry, allowed, role_label)
    kind = required_text(entry, "kind", role_label)
    icon = entry.get("icon")
    if icon is not None and (not isinstance(icon, str) or not icon.strip()):
        raise ContentError(f"{role_label}: 'icon' must be the name of an icon")
    if not sized:
        return ItemKind(kind, icon=icon)
    size = required_text(entry, "size", role_label)
    if size not in item_sizes:
        raise ContentError(f"{role_label}: 'size' names no item size ({size!r})")
    return ItemKind(kind, size, icon)


def _load_rule_data(
    definitions: list[Definition],
    modules: Mapping[str, ModuleType],
    character: CharacterSpec,
    template: str,
) -> dict[str, object]:
    """Run each rule-data definition's module configurator and retain its validated value."""
    configured: dict[str, object] = {}
    for definition in definitions:
        if definition.type != "rule_data":
            continue
        label = f"{template}: rule_data {definition.id}"
        data = definition.data
        if not isinstance(data, Mapping):
            raise ContentError(f"{label}: definition data must be an object")
        # A rule_data definition is an explicit extension point: after the loader's own keys,
        # the module's configurator owns the rest of the schema and rejects unknown settings.
        # Both bundled configurators validate their complete key set.
        module_id = required_text(data, "python_module", label)
        module = modules.get(module_id)
        if module is None:
            raise ContentError(f"{label}: unknown python_module {module_id!r}")
        configure_name = required_text(data, "configure", label)
        configurator = getattr(module, configure_name, None)
        if not callable(configurator):
            raise ContentError(
                f"{label}: configurator {configure_name!r} "
                f"is not callable in python_module {module_id!r}"
            )
        try:
            result = configurator(data, character, label)
        except ContentError:
            raise
        except (ValueError, TypeError, RuntimeError) as exc:
            raise ContentError(
                f"{label}: configurator {configure_name!r} "
                f"in python_module {module_id!r} failed: {exc}"
            ) from exc
        if result is None:
            raise ContentError(
                f"{label}: configurator {configure_name!r} "
                f"in python_module {module_id!r} returned None"
            )
        if definition.id in configured:
            raise ContentError(f"{template}: duplicate rule_data id {definition.id!r}")
        configured[definition.id] = result
    return configured


# The keys in a hooks definition that describe the definition rather than name a hook point.
# Every other key is a hook name, so a rule set can add a hook point without a core edit.
HOOK_RESERVED_KEYS = frozenset({"type", "id", "python_module", "options", "configure"})
# The standard hook points and the number of arguments each receives, not counting the
# HookContext every hook gets first. A rule set may still declare another hook name: it is
# called the same way, and the loader only warns that the name is unknown.
HOOK_ARITIES: Mapping[str, int] = {
    "new_day": 1,
    "before_rest": 3,
    "after_rest": 5,
    "held_back": 1,
    "after_consume": 5,
    "lodge": 1,
    "status": 1,
}


def _check_hook_arity(hook_name: str, function: Callable[..., Any], label: str) -> None:
    """Check a standard hook's function against the arguments the engine passes. An unknown
    hook name is allowed (it is a rule set's own extension point) and only warned about."""
    expected = HOOK_ARITIES.get(hook_name)
    if expected is None:
        logger.warning("%s: unknown hook name %r; it is still called", label, hook_name)
        return
    parameters = inspect.signature(function).parameters.values()
    if any(parameter.kind is parameter.VAR_POSITIONAL for parameter in parameters):
        return
    positional = [
        parameter
        for parameter in parameters
        if parameter.name != "hc"
        and parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
    ]
    if len(positional) != expected:
        raise ContentError(
            f"{label}: hook {hook_name!r} takes {expected} argument(s) after hc, but "
            f"{function.__name__!r} takes {len(positional)}"
        )


def _load_rule_hooks(
    definitions: list[Definition],
    modules: Mapping[str, ModuleType],
    character: CharacterSpec,
    rests: Mapping[str, RestLength],
    template: str,
) -> Hooks | None:
    """Load hook functions by name and their options. Every non-reserved key in the definition is
    a hook name, and its value must name a function in the rule set's module. An optional
    `configure` callable can validate and normalize the options. A rest with upkeep needs the
    before_rest hook."""
    definition = _single_optional(definitions, "hooks", template)
    if definition is None:
        if any(length.upkeep for length in rests.values()):
            raise ContentError(
                f"{template}: a rest has upkeep, so the rule set needs a 'hooks' definition"
            )
        return None
    label = f"{template}: hooks {definition.id}"
    data = definition.data
    module_id = required_text(data, "python_module", label)
    if module_id not in modules:
        raise ContentError(f"{label}: unknown python_module {module_id!r}")
    module = modules[module_id]
    functions: dict[str, str] = {}
    for hook_name in data:
        if hook_name in HOOK_RESERVED_KEYS:
            continue
        function_name = _optional_nonempty_text(data, hook_name, label)
        assert function_name is not None
        function = getattr(module, function_name, None)
        if not callable(function):
            raise ContentError(f"{label}: {function_name!r} is not a function in {module_id!r}")
        _check_hook_arity(hook_name, function, label)
        functions[hook_name] = function_name
    if any(length.upkeep for length in rests.values()) and "before_rest" not in functions:
        raise ContentError(
            f"{label}: a rest has upkeep, so the hooks need a 'before_rest' function"
        )
    options = data.get("options", {})
    if not isinstance(options, Mapping):
        raise ContentError(f"{label}: 'options' must be an object")
    options = dict(options)
    configure_name = _optional_nonempty_text(data, "configure", label)
    if configure_name is not None:
        configurator = getattr(module, configure_name, None)
        if not callable(configurator):
            raise ContentError(
                f"{label}: hook configurator {configure_name!r} is not callable in {module_id!r}"
            )
        try:
            configured_options = configurator(options, character, label)
        except ContentError:
            raise
        except (ValueError, TypeError, RuntimeError) as exc:
            raise ContentError(
                f"{label}: hook configurator {configure_name!r} failed: {exc}"
            ) from exc
        if not isinstance(configured_options, Mapping):
            raise ContentError(
                f"{label}: hook configurator {configure_name!r} must return a mapping"
            )
        if any(not isinstance(key, str) for key in configured_options):
            raise ContentError(
                f"{label}: hook configurator {configure_name!r} returned a mapping "
                "with a non-string key"
            )
        options = dict(configured_options)
    return Hooks(module=module, functions=functions, options=options)


def _check_item_sizes(character: CharacterSpec, item_sizes: Mapping[str, int], template: str):
    """Every item feature must name an item size, so the engine can carry it."""
    for feature in character.features.values():
        if feature.feature_type == "item" and feature.size not in item_sizes:
            raise ContentError(
                f"{template}: feature {feature.id!r} names no item size ({feature.size!r})"
            )


def _shop_item_feature(
    data: Mapping[str, Any], character: CharacterSpec, label: str
) -> Feature | None:
    """The feature a shop item copies its identity and stats from. A shop item names one with
    `feature`; any field it authors itself still wins, so it may override a copied name or stat."""
    feature_id = data.get("feature")
    if feature_id is None:
        return None
    if not isinstance(feature_id, str) or not feature_id.strip():
        raise ContentError(f"{label}: 'feature' must be the id of a feature")
    feature = character.features.get(feature_id)
    if feature is None:
        raise ContentError(f"{label}: 'feature' names no feature ({feature_id!r})")
    return feature


def _shop_item_value(
    data: Mapping[str, Any], feature: Feature | None, key: str, default: Any
) -> Any:
    """A shop item's own value for a key, else the feature it copies, else the default."""
    if key in data:
        return data[key]
    return getattr(feature, key) if feature is not None else default


def _load_marketplace(
    definitions: list[Definition],
    item_sizes: Mapping[str, int],
    character: CharacterSpec,
    template: str,
    *,
    icon_entries: Mapping[str, tuple[str | None, str | None]] | None = None,
) -> Marketplace | None:
    """The marketplace: `shop_item`, `service`, and `hireling` definitions. Items may be petty,
    normal, or bulky, as the rule set's item sizes say. A shop item naming a `feature` copies
    that feature's name, size, armor, damage, uses, description, icon, and encumbrance checks
    unless it authors its own. The category, price, and armor mode are always the shop's own."""
    shop = [d for d in definitions if d.type == "shop_item"]
    services = [d for d in definitions if d.type == "service"]
    hirelings = [d for d in definitions if d.type == "hireling"]
    head = _single_optional(definitions, "marketplace", template)
    if head is None:
        if shop or services or hirelings:
            raise ContentError(f"{template}: the marketplace needs a 'marketplace' definition")
        return None
    market_label = f"{template}: marketplace"
    check_keys(head.data, {"type", "id", "currency"}, market_label)
    currency = required_text(head.data, "currency", market_label)
    if currency not in character.features:
        raise ContentError(f"{template}: marketplace 'currency' names no feature ({currency!r})")
    items = []
    for d in shop:
        label = f"{template}: shop item {d.id}"
        check_keys(
            d.data,
            {
                "type",
                "id",
                "feature",
                "name",
                "category",
                "price",
                "size",
                "armor",
                "armor_mode",
                "damage",
                "uses",
                "description",
                "encumbrance_checks",
                "icon",
                "looks_like",
            },
            label,
        )
        feature = _shop_item_feature(d.data, character, label)
        size = _shop_item_value(d.data, feature, "size", "")
        if not isinstance(size, str) or not size.strip():
            size = required_text(d.data, "size", label)
        if size not in item_sizes:
            raise ContentError(f"{label}: 'size' names no item size ({size!r})")
        name = _shop_item_value(d.data, feature, "name", "")
        if not isinstance(name, str) or not name.strip():
            name = required_text(d.data, "name", label)
        damage = str(_shop_item_value(d.data, feature, "damage", ""))
        if "icon" in d.data or "looks_like" in d.data or feature is None:
            icon, looks_like = _icon_fields(
                d.data,
                icon_entries or {},
                FALLBACK_WEAPON if damage else FALLBACK_ITEM,
                label,
            )
        else:
            icon = feature.icon or FALLBACK_ITEM
            looks_like = feature.looks_like
        items.append(
            ShopItem(
                id=d.id,
                name=name,
                category=required_text(d.data, "category", label),
                price=_whole(d.data, "price", label, 0),
                size=size,
                armor=(
                    _whole(d.data, "armor", label, 0)
                    if "armor" in d.data or feature is None
                    else _shop_item_value(d.data, feature, "armor", 0)
                ),
                armor_mode=_choice(
                    d.data, "armor_mode", ("add", "set", "max", "shield"), "add", label
                ),
                damage=damage,
                uses=(
                    _whole(d.data, "uses", label, 0)
                    if "uses" in d.data or feature is None
                    else _shop_item_value(d.data, feature, "uses", 0)
                ),
                description=str(_shop_item_value(d.data, feature, "description", "")),
                encumbrance_checks=(
                    _names(d.data, "encumbrance_checks", label)
                    if "encumbrance_checks" in d.data
                    else _shop_item_value(d.data, feature, "encumbrance_checks", ())
                ),
                icon=icon or FALLBACK_ITEM,
                looks_like=looks_like,
            )
        )
    loaded_services = []
    for d in services:
        label = f"{template}: service {d.id}"
        check_keys(
            d.data,
            {"type", "id", "name", "price", "effect", "fits", "restores", "description"},
            label,
        )
        effect = required_text(d.data, "effect", label)
        if effect not in ("lodge", "restore", "deferred"):
            raise ContentError(f"{label}: 'effect' must be lodge, restore, or deferred")
        restores = _names(d.data, "restores", label)
        for stat in restores:
            if stat not in character.stats:
                raise ContentError(f"{label}: 'restores' names no stat ({stat!r})")
        if effect == "restore" and not restores:
            raise ContentError(f"{label}: a restore service needs 'restores'")
        fits = _whole(d.data, "fits", label, 1) if "fits" in d.data else 1
        loaded_services.append(
            ShopService(
                id=d.id,
                name=required_text(d.data, "name", label),
                price=_whole(d.data, "price", label, 0),
                effect=effect,
                fits=fits,
                restores=restores,
                description=_optional_text(d.data, "description", label) or "",
            )
        )
    hired = []
    for d in hirelings:
        label = f"{template}: hireling {d.id}"
        check_keys(d.data, {"type", "id", "name", "wage_per_day", "description"}, label)
        hired.append(
            Hireling(
                id=d.id,
                name=required_text(d.data, "name", label),
                wage_per_day=_whole(d.data, "wage_per_day", label, 0),
                description=_optional_text(d.data, "description", label) or "",
            )
        )
    return Marketplace(
        currency=currency,
        items=tuple(items),
        services=tuple(loaded_services),
        hirelings=tuple(hired),
    )


def _load_projects(
    definitions: list[Definition], checks: Mapping[str, CheckRules], label: str
) -> dict[str, ProjectDefinition]:
    """Source-defined project kinds by id."""
    projects: dict[str, ProjectDefinition] = {}
    for d in definitions:
        if d.type != "project":
            continue
        project_label = f"{label}: project {d.id}"
        check_keys(
            d.data,
            {
                "type",
                "id",
                "label",
                "description",
                "target",
                "cost",
                "currency",
                "skill",
                "skill_level",
            },
            project_label,
        )
        projects[d.id] = ProjectDefinition(
            id=d.id,
            label=required_text(d.data, "label", project_label),
            description=required_text(d.data, "description", project_label),
            target=_whole(d.data, "target", project_label, 1) if "target" in d.data else 1,
            cost=_whole(d.data, "cost", project_label, 0) if "cost" in d.data else 0,
            currency=_optional_text(d.data, "currency", project_label) or "",
            skill=_optional_text(d.data, "skill", project_label) or "",
            skill_level=(
                _whole(d.data, "skill_level", project_label, 1) if "skill_level" in d.data else 0
            ),
        )
    return projects


def _load_faction_assets(
    definitions: list[Definition], template: str
) -> dict[str, FactionAssetDefinition]:
    """Source-defined faction assets by id. A category or Magic rating outside the book's own
    values fails at load time, so a typo cannot become a silently unusable asset."""
    loaded: dict[str, FactionAssetDefinition] = {}
    categories = ("cunning", "force", "wealth")
    magic_ratings = ("None", "Low", "Medium", "High")
    for d in definitions:
        if d.type != "faction_asset":
            continue
        label = f"{template}: faction asset {d.id}"
        if d.id in loaded:
            raise ContentError(f"{template}: duplicate faction asset {d.id!r}")
        check_keys(
            d.data,
            {
                "type",
                "id",
                "label",
                "description",
                "category",
                "rating",
                "cost",
                "hp",
                "magic",
                "attack",
                "counter",
                "qualities",
            },
            label,
        )
        category = required_text(d.data, "category", label)
        if category not in categories:
            raise ContentError(f"{label}: 'category' must be one of {', '.join(categories)}")
        magic = required_text(d.data, "magic", label)
        if magic not in magic_ratings:
            raise ContentError(f"{label}: 'magic' must be one of {', '.join(magic_ratings)}")
        rating = _whole(d.data, "rating", label, 1)
        if rating > 8:
            raise ContentError(f"{label}: 'rating' must be at most 8")
        attack = d.data.get("attack", "")
        counter = d.data.get("counter", "")
        if not isinstance(attack, str) or not isinstance(counter, str):
            raise ContentError(f"{label}: 'attack' and 'counter' must be text")
        loaded[d.id] = FactionAssetDefinition(
            id=d.id,
            label=required_text(d.data, "label", label),
            description=required_text(d.data, "description", label),
            category=category,
            rating=rating,
            cost=_whole(d.data, "cost", label, 0),
            hp=_whole(d.data, "hp", label, 1),
            magic=magic,
            attack=attack,
            counter=counter,
            qualities=_names(d.data, "qualities", label),
        )
    return loaded


def _load_conditions(
    definitions: list[Definition],
    character: CharacterSpec,
    condition_ends: tuple[str, ...],
    template: str,
) -> dict[str, ConditionRules]:
    """Conditions by id. A stored condition is one a character can have; a derived one shows up
    from the stats and has no entry on the character."""
    stored = frozenset(
        d.id for d in definitions if d.type == "condition" and d.data.get("derived") is None
    )
    conditions: dict[str, ConditionRules] = {}
    for d in definitions:
        if d.type != "condition":
            continue
        label = f"{template}: condition {d.id}"
        if d.id in conditions:
            raise ContentError(f"{template}: duplicate condition {d.id!r}")
        check_keys(
            d.data,
            {
                "type",
                "id",
                "label",
                "description",
                "ends",
                "on_end",
                "derived",
                "modifies",
                "dying",
                "fatal",
            },
            label,
        )
        ends = d.data.get("ends", [])
        allowed = (*condition_ends, ALWAYS_CONDITION_END)
        if not isinstance(ends, list) or any(end not in allowed for end in ends):
            raise ContentError(f"{label}: 'ends' must list only {allowed}")
        on_end, _ = _check_effects(d.data.get("on_end", []), label, character, stored, False)
        derived = _load_derived(d.data.get("derived"), character, label)
        if derived is not None and (ends or on_end):
            raise ContentError(f"{label}: a derived condition cannot have 'ends' or 'on_end'")
        conditions[d.id] = ConditionRules(
            id=d.id,
            label=required_text(d.data, "label", label),
            description=required_text(d.data, "description", label),
            ends=tuple(ends),
            on_end=on_end,
            derived=derived,
            modifies=_load_modifies(d.data.get("modifies", {}), character, label),
            dying=_flag_default(d.data, "dying", label, False),
            fatal=_flag_default(d.data, "fatal", label, False),
        )
    return conditions


def _load_modifies(value: object, character: CharacterSpec, label: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ContentError(f"{label}: 'modifies' must be an object")
    for stat, amount in value.items():
        if stat != ALL_STATS and stat not in character.stats and stat not in SITUATIONAL_MODIFIERS:
            raise ContentError(f"{label}: 'modifies' names no stat ({stat!r})")
        if not isinstance(amount, int) or isinstance(amount, bool):
            raise ContentError(f"{label}: 'modifies' {stat!r} must be a whole number")
    return dict(value)


def _check_template_grants(character: CharacterSpec, template: str) -> None:
    """A template's stat bonuses and counter starts must name stats and counters the rule set
    defines."""
    for item in character.templates.values():
        label = f"{template}: template {item.id}"
        for stat in item.stats:
            if stat not in character.stats:
                raise ContentError(f"{label}: unknown stat {stat!r} in 'stats'")
        for counter in item.counters:
            if counter not in character.counters:
                raise ContentError(f"{label}: unknown counter {counter!r} in 'counters'")


def _load_counters(
    definitions: list[Definition],
    character: CharacterSpec,
    conditions: frozenset[str],
    template: str,
) -> dict[str, CounterRules]:
    """Counters by id. A counter's `full` effects may name any counter, so the ids are read first."""
    ids = frozenset(d.id for d in definitions if d.type == "counter")
    counters: dict[str, CounterRules] = {}
    for d in definitions:
        if d.type != "counter":
            continue
        label = f"{template}: counter {d.id}"
        if d.id in counters:
            raise ContentError(f"{template}: duplicate counter {d.id!r}")
        check_keys(
            d.data,
            {"type", "id", "label", "description", "min", "max", "initial", "full"},
            label,
        )
        minimum = _optional_signed_whole(d.data, "min", label)
        maximum = _whole(d.data, "max", label, minimum) if "max" in d.data else None
        initial = _whole(d.data, "initial", label, minimum) if "initial" in d.data else minimum
        if maximum is not None and initial > maximum:
            raise ContentError(f"{label}: 'initial' is above 'max'")
        full, _ = _check_effects(
            d.data.get("full", []), label, character, conditions, False, counters=ids
        )
        if full and maximum is None:
            raise ContentError(f"{label}: 'full' needs a 'max'")
        counters[d.id] = CounterRules(
            id=d.id,
            label=required_text(d.data, "label", label),
            minimum=minimum,
            maximum=maximum,
            initial=initial,
            full=full,
            description=_optional_text(d.data, "description", label) or "",
        )
    return counters


def _load_advancements(
    definitions: list[Definition],
    character: CharacterSpec,
    counters: Mapping[str, CounterRules],
    conditions: frozenset[str],
    template: str,
) -> dict[str, AdvancementRules]:
    """The rule set's Advancement, if it has one. Its options spend one point of its counter."""
    ids = frozenset(counters)
    loaded: dict[str, AdvancementRules] = {}
    for d in definitions:
        if d.type != "advancement":
            continue
        label = f"{template}: advancement {d.id}"
        if loaded:
            raise ContentError(f"{template}: more than one advancement")
        check_keys(d.data, {"type", "id", "label", "counter", "options"}, label)
        counter = required_text(d.data, "counter", label)
        if counter not in ids:
            raise ContentError(f"{label}: 'counter' names no counter ({counter!r})")
        raw = d.data.get("options")
        if not isinstance(raw, list) or not raw:
            raise ContentError(f"{label}: 'options' must be a non-empty list")
        options: dict[str, AdvancementOption] = {}
        for option in raw:
            if not isinstance(option, Mapping):
                raise ContentError(f"{label}: each option must be an object")
            value = required_text(option, "value", label)
            if value in options:
                raise ContentError(f"{label}: option {value!r} is repeated")
            olabel = f"{label}: option {value}"
            check_keys(
                option,
                {"value", "label", "text", "limit", "picks", "effects", "learn"},
                olabel,
            )
            picks = _flag(option, "picks", olabel)
            effects, _ = _check_effects(
                option.get("effects", []), olabel, character, conditions, picks, counters=ids
            )
            if picks and not any(op.get("stat") == PICKED for op in effects):
                raise ContentError(f"{olabel}: 'picks' is set, but no effect uses the pick")
            maximum: int | CounterCap | None = None
            for op in effects:
                if op.get("stat") != PICKED:
                    continue
                cap = op.get("cap")
                if isinstance(cap, int) and not isinstance(cap, bool):
                    maximum = cap
                elif isinstance(cap, Mapping):
                    maximum = CounterCap(
                        counter=required_text(cap, "counter", olabel),
                        bands={int(k): int(v) for k, v in cap["bands"].items()},
                    )
            options[value] = AdvancementOption(
                value=value,
                label=required_text(option, "label", olabel),
                text=required_text(option, "text", olabel),
                limit=_optional_limit(option, olabel),
                picks=picks,
                effects=effects,
                learn=_load_learn(option.get("learn"), character, olabel),
                maximum=maximum,
            )
        loaded[d.id] = AdvancementRules(
            id=d.id,
            label=required_text(d.data, "label", label),
            counter=counter,
            options=options,
        )
    return loaded


def _load_level_ups(
    definitions: list[Definition],
    modules: Mapping[str, ModuleType],
    character: CharacterSpec,
    counters: Mapping[str, CounterRules],
    campaign: CampaignSpec,
    rule_data: Mapping[str, object],
    template: str,
) -> dict[str, LevelUpRules]:
    """The rule set's level progression, if it has one. Its picks are the choices a player makes
    when a level is gained.

    A level-up names either its own ``tracks`` or an ``xp_source`` path into the rule data, so
    the experience chart has one authored home."""
    loaded: dict[str, LevelUpRules] = {}
    for definition in definitions:
        if definition.type != "level_up":
            continue
        label = f"{template}: level_up {definition.id}"
        if loaded:
            raise ContentError(f"{template}: more than one level_up")
        check_keys(
            definition.data,
            {
                "type",
                "id",
                "label",
                "description",
                "level_counter",
                "xp_counter",
                "tracks",
                "xp_source",
                "rate_option",
                "python_module",
                "apply_level",
                "summary",
                "picks",
                "template_kinds",
            },
            label,
        )
        levels = required_text(definition.data, "level_counter", label)
        if levels not in counters:
            raise ContentError(f"{label}: 'level_counter' names no counter ({levels!r})")
        xp = required_text(definition.data, "xp_counter", label)
        if xp not in counters:
            raise ContentError(f"{label}: 'xp_counter' names no counter ({xp!r})")
        rate = required_text(definition.data, "rate_option", label)
        if rate not in campaign.fields:
            raise ContentError(f"{label}: 'rate_option' names no campaign option ({rate!r})")
        tracks = _level_tracks(definition.data, rule_data, label)
        module_id = required_text(definition.data, "python_module", label)
        module = modules.get(module_id)
        if module is None:
            raise ContentError(f"{label}: unknown python_module {module_id!r}")
        apply_level = _level_up_function(
            module, required_text(definition.data, "apply_level", label), label
        )
        summary_name = _optional_text(definition.data, "summary", label)
        summary = (
            _level_up_function(module, summary_name, label) if summary_name is not None else None
        )
        picks = _load_level_picks(
            definition.data.get("picks"),
            module,
            character,
            counters,
            rule_data,
            label,
        )
        template_kinds = tuple(_names(definition.data, "template_kinds", label))
        known_kinds = {template.kind for template in character.templates.values()}
        unknown_kinds = sorted(set(template_kinds) - known_kinds)
        if unknown_kinds:
            raise ContentError(
                f"{label}: template_kinds names unknown template kinds {unknown_kinds!r}"
            )
        loaded[definition.id] = LevelUpRules(
            id=definition.id,
            label=required_text(definition.data, "label", label),
            description=_optional_text(definition.data, "description", label) or "",
            level_counter=levels,
            xp_counter=xp,
            tracks=tracks,
            rate_option=rate,
            max_level=len(next(iter(tracks.values()))),
            apply_level=apply_level,
            summary=summary,
            picks=picks,
            template_kinds=template_kinds,
        )
    return loaded


def _level_tracks(
    data: Mapping[str, Any], rule_data: Mapping[str, object], label: str
) -> Mapping[str, tuple[int, ...]]:
    """The experience tracks a level-up uses, either authored here or read from rule data."""
    source = _optional_text(data, "xp_source", label)
    raw = data.get("tracks")
    if source is not None and raw is not None:
        raise ContentError(f"{label}: give 'tracks' or 'xp_source', not both")
    if source is not None:
        return _load_level_tracks(_rule_data_path(rule_data, source, label), f"{label}.xp_source")
    return _load_level_tracks(raw, label)


def _rule_data_path(rule_data: Mapping[str, object], path: str, label: str) -> object:
    """Resolve a dotted path such as ``progression.xp`` through rule data and its attributes."""
    current: object = rule_data
    walked: list[str] = []
    for part in path.split("."):
        if not part:
            raise ContentError(f"{label}: 'xp_source' must be a dotted path")
        walked.append(part)
        if isinstance(current, Mapping):
            if part not in current:
                raise ContentError(f"{label}: 'xp_source' names no rule data {'.'.join(walked)!r}")
            current = current[part]
        else:
            if not hasattr(current, part):
                raise ContentError(f"{label}: 'xp_source' names no rule data {'.'.join(walked)!r}")
            current = getattr(current, part)
    return current


def _load_level_tracks(data: object, label: str) -> Mapping[str, tuple[int, ...]]:
    """The XP thresholds of each experience track, all the same length and rising from zero."""
    if not isinstance(data, Mapping) or not data:
        raise ContentError(f"{label}: experience tracks must be a non-empty object")
    tracks: dict[str, tuple[int, ...]] = {}
    length: int | None = None
    for name, values in data.items():
        track_label = f"{label}[{name!r}]"
        if not isinstance(name, str) or not name.strip():
            raise ContentError(f"{label}: a track name must be non-empty text")
        if not isinstance(values, (list, tuple)) or not values:
            raise ContentError(f"{track_label}: must be a non-empty list of levels")
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise ContentError(f"{track_label}: every threshold must be a whole number")
        if values[0] != 0:
            raise ContentError(f"{track_label}: the level 1 threshold must be 0")
        if any(left >= right for left, right in pairwise(values)):
            raise ContentError(f"{track_label}: thresholds must rise at every level")
        if length is None:
            length = len(values)
        elif len(values) != length:
            raise ContentError(f"{label}: every track must list the same number of levels")
        tracks[name] = tuple(values)
    return tracks


def _load_level_picks(
    data: object,
    module: ModuleType,
    character: CharacterSpec,
    counters: Mapping[str, CounterRules],
    rule_data: Mapping[str, object],
    label: str,
) -> Mapping[str, LevelUpPick]:
    if not isinstance(data, list) or not data:
        raise ContentError(f"{label}: 'picks' must be a non-empty list")
    picks: dict[str, LevelUpPick] = {}
    for value in data:
        if not isinstance(value, Mapping):
            raise ContentError(f"{label}: every pick must be an object")
        pick_id = required_text(value, "id", label)
        if pick_id in picks:
            raise ContentError(f"{label}: pick {pick_id!r} is repeated")
        pick_label = f"{label}: pick {pick_id}"
        kind = required_text(value, "kind", pick_label)
        if kind not in ("skill", "stat", "feature"):
            raise ContentError(f"{pick_label}: 'kind' must be skill, stat, or feature")
        if kind == "feature":
            check_keys(
                value,
                {
                    "id",
                    "label",
                    "kind",
                    "counter",
                    "feature_type",
                    "tags",
                    "exclude",
                    "options",
                    "apply",
                },
                pick_label,
            )
        else:
            check_keys(
                value,
                {
                    "id",
                    "label",
                    "kind",
                    "counter",
                    "costs",
                    "costs_ref",
                    "minimum_levels",
                    "minimum_levels_ref",
                    "maximum",
                    "maximum_ref",
                    "limit_counter",
                    "stat_group",
                    "apply",
                },
                pick_label,
            )
        counter = required_text(value, "counter", pick_label)
        if counter not in counters:
            raise ContentError(f"{pick_label}: 'counter' names no counter ({counter!r})")
        apply = _level_up_function(module, required_text(value, "apply", pick_label), pick_label)
        if kind == "feature":
            feature_type = required_text(value, "feature_type", pick_label)
            if feature_type not in character.feature_types:
                raise ContentError(
                    f"{pick_label}: 'feature_type' must be one of "
                    f"{', '.join(character.feature_types)}"
                )
            options_name = required_text(value, "options", pick_label)
            options = _level_up_function(module, options_name, pick_label)
            tags = _texts(value.get("tags", []), f"{pick_label}.tags")
            exclude = _texts(value.get("exclude", []), f"{pick_label}.exclude")
            picks[pick_id] = LevelUpPick(
                id=pick_id,
                label=required_text(value, "label", pick_label),
                kind=kind,
                counter=counter,
                feature_type=feature_type,
                tags=tags,
                exclude=exclude,
                options=options,
                apply=apply,
            )
            continue
        costs = _level_costs(
            _level_pick_field(value, "costs", rule_data, pick_label),
            f"{pick_label}.costs",
        )
        minimums = _level_costs(
            _level_pick_field(value, "minimum_levels", rule_data, pick_label),
            f"{pick_label}.minimum_levels",
        )
        if set(costs) != set(minimums):
            raise ContentError(f"{pick_label}: costs and minimum_levels must name the same steps")
        maximum = _level_pick_field(value, "maximum", rule_data, pick_label)
        if maximum is not None and (
            isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1
        ):
            raise ContentError(f"{pick_label}: 'maximum' must be a whole number above 0")
        limit = _optional_text(value, "limit_counter", pick_label)
        if limit is not None and limit not in counters:
            raise ContentError(f"{pick_label}: 'limit_counter' names no counter ({limit!r})")
        picks[pick_id] = LevelUpPick(
            id=pick_id,
            label=required_text(value, "label", pick_label),
            kind=kind,
            counter=counter,
            costs=costs,
            minimum_levels=minimums,
            maximum=maximum,
            limit_counter=limit or "",
            stat_group=_optional_text(value, "stat_group", pick_label) or "",
            apply=apply,
        )
    return picks


def _level_pick_field(
    value: Mapping[str, Any],
    key: str,
    rule_data: Mapping[str, object],
    label: str,
) -> object:
    """A pick setting given inline or by a ``*_ref`` path into rule data, exactly one of them."""
    direct = value.get(key)
    ref = _optional_text(value, f"{key}_ref", label)
    if direct is not None and ref is not None:
        raise ContentError(f"{label}: give {key!r} or {key + '_ref'!r}, not both")
    if ref is not None:
        return _rule_data_path(rule_data, ref, label)
    return direct


def _level_costs(data: object, label: str) -> Mapping[int, int]:
    """Costs by step number: a JSON object with integer keys, or a list indexed from one."""
    if isinstance(data, list):
        values = {index: value for index, value in enumerate(data, start=1)}
    elif isinstance(data, Mapping):
        values = {}
        for key, value in data.items():
            try:
                step = int(key)
            except (TypeError, ValueError):
                raise ContentError(f"{label}: step names must be whole numbers") from None
            values[step] = value
    else:
        raise ContentError(f"{label}: must be an object or a list")
    if not values:
        raise ContentError(f"{label}: must name at least one step")
    for step, cost in values.items():
        if isinstance(cost, bool) or not isinstance(cost, int) or cost < 1:
            raise ContentError(f"{label}: the cost at step {step} must be a whole number above 0")
    return dict(sorted(values.items()))


def _level_up_function(module: ModuleType, name: str, label: str) -> Callable[..., Any]:
    function = getattr(module, name, None)
    if not callable(function):
        raise ContentError(f"{label}: {name!r} is not a function in {module.__name__!r}")
    return function


def _texts(data: object, label: str) -> tuple[str, ...]:
    if not isinstance(data, list) or any(not isinstance(value, str) for value in data):
        raise ContentError(f"{label}: must be a list of text values")
    return tuple(data)


def _load_learn(data: object, character: CharacterSpec, label: str) -> LearnRules | None:
    """The feature an option teaches: a feature type, and the character field whose value its tag
    must match. None when the option teaches none."""
    if data is None:
        return None
    data = check_object_keys(
        data,
        {"feature_type", "tag", "tag_field", "exclude", "exclude_tag_fields"},
        f"{label}: 'learn'",
    )
    feature_type = required_text(data, "feature_type", label)
    if feature_type not in {f.feature_type for f in character.features.values()}:
        raise ContentError(f"{label}: 'learn' names no feature type ({feature_type!r})")
    tag = _optional_text(data, "tag", label) or ""
    tag_field = data.get("tag_field", "")
    if tag and tag_field:
        raise ContentError(f"{label}: 'learn' takes a tag or a tag_field, not both")
    if tag_field:
        if not isinstance(tag_field, str):
            raise ContentError(f"{label}: 'learn' tag_field must be text")
        field_spec = character.fields.get(tag_field)
        if field_spec is None or field_spec.table is None:
            raise ContentError(f"{label}: 'learn' needs a table field named {tag_field!r}")
    exclude = _texts(data.get("exclude", []), f"{label}: 'learn' exclude")
    exclude_tag_fields = _texts(
        data.get("exclude_tag_fields", []), f"{label}: 'learn' exclude_tag_fields"
    )
    for field_id in exclude_tag_fields:
        field_spec = character.fields.get(field_id)
        if field_spec is None or field_spec.table is None:
            raise ContentError(
                f"{label}: 'learn' needs a table field named {field_id!r} in exclude_tag_fields"
            )
    return LearnRules(
        feature_type=feature_type,
        tag=tag,
        tag_field=tag_field,
        exclude=exclude,
        exclude_tag_fields=exclude_tag_fields,
    )


def _load_periods(
    definitions: list[Definition],
    character: CharacterSpec,
    counters: Mapping[str, CounterRules],
    conditions: frozenset[str],
    tallies: Mapping[str, TallyRules],
    template: str,
) -> dict[str, PeriodRules]:
    loaded: dict[str, PeriodRules] = {}
    for d in definitions:
        if d.type != "period":
            continue
        label = f"{template}: period {d.id}"
        if d.id in loaded:
            raise ContentError(f"{template}: duplicate period {d.id!r}")
        check_keys(
            d.data,
            {"type", "id", "label", "description", "effects", "tallies", "resets"},
            label,
        )
        effects, _ = _check_effects(
            d.data.get("effects", []),
            label,
            character,
            conditions,
            False,
            counters=frozenset(counters),
        )
        resets = _text_list(d.data.get("resets", []), f"{label}: resets")
        if len(set(resets)) != len(resets):
            raise ContentError(f"{label}: 'resets' repeats a label")
        if any(":" in reset for reset in resets):
            raise ContentError(f"{label}: 'resets' must not contain ':'")
        if not effects and not resets:
            raise ContentError(f"{label}: a period needs 'effects' or 'resets'")
        reset = d.data.get("tallies", [])
        if not isinstance(reset, list) or not all(isinstance(t, str) for t in reset):
            raise ContentError(f"{label}: 'tallies' must be a list of tally ids")
        unknown = [t for t in reset if t not in tallies]
        if unknown:
            raise ContentError(f"{label}: 'tallies' names no tally ({', '.join(unknown)})")
        loaded[d.id] = PeriodRules(
            id=d.id,
            label=required_text(d.data, "label", label),
            effects=effects,
            description=_optional_text(d.data, "description", label) or "",
            tallies=tuple(reset),
            resets=tuple(resets),
        )
    return loaded


def _type_text(data: Mapping[str, Any], key: str, label: str, fallback: str) -> str:
    """A feature-type text field, or the default it overlays when the field is absent."""
    if key not in data:
        return fallback
    return _optional_text(data, key, label) or ""


def _type_flag(data: Mapping[str, Any], key: str, label: str, fallback: bool) -> bool:
    """A feature-type boolean, or the default it overlays when the field is absent."""
    if key not in data:
        return fallback
    return _flag_default(data, key, label, False)


def _load_feature_types(
    definitions: list[Definition], template: str
) -> dict[str, FeatureTypeRules]:
    """Every feature type the rule set offers. A definition overlays the engine default with the
    same id, so a rule set may relabel one field or add a tag group without restating the rest.
    A new id must give its name and is then independent of the defaults."""
    loaded: dict[str, FeatureTypeRules] = dict(DEFAULT_FEATURE_TYPES)
    for d in definitions:
        if d.type != "feature_type":
            continue
        label = f"{template}: feature_type {d.id}"
        check_keys(
            d.data,
            {
                "type",
                "id",
                "name",
                "plural",
                "group",
                "rank_label",
                "tag_label",
                "tag_groups",
                "unique",
                "inventory",
            },
            label,
        )
        base = loaded.get(d.id)
        if "name" in d.data:
            name = required_text(d.data, "name", label)
        elif base is not None:
            name = base.name
        else:
            raise ContentError(f"{label}: a new feature type needs a 'name'")

        tag_groups = base.tag_groups if base is not None else {}
        if "tag_groups" in d.data:
            raw_groups = d.data["tag_groups"]
            if not isinstance(raw_groups, Mapping):
                raise ContentError(f"{label}: 'tag_groups' must map tags to group names")
            tag_groups = {}
            for tag, group in raw_groups.items():
                if not isinstance(tag, str) or not tag.strip():
                    raise ContentError(f"{label}: 'tag_groups' tags must be non-empty text")
                if not isinstance(group, str) or not group.strip():
                    raise ContentError(f"{label}: 'tag_groups' values must be non-empty text")
                tag_groups[tag] = group
        loaded[d.id] = FeatureTypeRules(
            id=d.id,
            name=name,
            plural=_type_text(d.data, "plural", label, base.plural if base else ""),
            group=_type_text(d.data, "group", label, base.group if base else ""),
            rank_label=_type_text(d.data, "rank_label", label, base.rank_label if base else ""),
            tag_label=_type_text(d.data, "tag_label", label, base.tag_label if base else ""),
            tag_groups=dict(tag_groups),
            unique=_type_flag(d.data, "unique", label, base.unique if base else False),
            inventory=_type_flag(d.data, "inventory", label, base.inventory if base else False),
        )
    return loaded


def _load_tallies(definitions: list[Definition], template: str) -> dict[str, TallyRules]:
    loaded: dict[str, TallyRules] = {}
    for d in definitions:
        if d.type != "tally":
            continue
        label = f"{template}: tally {d.id}"
        if d.id in loaded:
            raise ContentError(f"{template}: duplicate tally {d.id!r}")
        check_keys(d.data, {"type", "id", "label", "entries", "dynamic", "max_entries"}, label)
        dynamic = _flag_default(d.data, "dynamic", label, False)
        max_entries = _whole(d.data, "max_entries", label, 1) if "max_entries" in d.data else None
        raw = d.data.get("entries", [])
        if not isinstance(raw, list):
            raise ContentError(f"{label}: 'entries' must be a list")
        if not raw and not dynamic:
            raise ContentError(f"{label}: 'entries' must be a non-empty list")
        entries: dict[str, str] = {}
        for entry in raw:
            if not isinstance(entry, Mapping):
                raise ContentError(f"{label}: each entry must be an object")
            check_keys(entry, {"value", "label"}, label)
            value = required_text(entry, "value", label)
            if value in entries:
                raise ContentError(f"{label}: entry {value!r} is repeated")
            entries[value] = required_text(entry, "label", label)
        loaded[d.id] = TallyRules(
            id=d.id,
            label=required_text(d.data, "label", label),
            entries=entries,
            dynamic=dynamic,
            max_entries=max_entries,
        )
    return loaded


def _load_relations(definitions: list[Definition], template: str) -> dict[str, RelationRules]:
    loaded: dict[str, RelationRules] = {}
    for d in definitions:
        if d.type != "relation":
            continue
        label = f"{template}: relation {d.id}"
        if d.id in loaded:
            raise ContentError(f"{template}: duplicate relation {d.id!r}")
        check_keys(d.data, {"type", "id", "label", "limit"}, label)
        loaded[d.id] = RelationRules(
            id=d.id,
            label=required_text(d.data, "label", label),
            limit=_optional_limit(d.data, label),
        )
    return loaded


def _optional_limit(data: Mapping[str, Any], label: str) -> int | None:
    if "limit" not in data:
        return None
    return _whole(data, "limit", label, 1)


def _load_checks(definitions: list[Definition], template: str) -> dict[str, CheckRules]:
    """The `check` definitions, by id: the dice a check rolls against a difficulty."""
    checks: dict[str, CheckRules] = {}
    for d in definitions:
        if d.type != "check":
            continue
        label = f"{template}: check {d.id}"
        check_keys(
            d.data,
            {
                "type",
                "id",
                "label",
                "dice",
                "unskilled",
                "default_difficulty",
                "minimum_difficulty",
                "maximum_difficulty",
            },
            label,
        )
        dice = required_text(d.data, "dice", label)
        if not is_formula(dice):
            raise ContentError(f"{label}: 'dice' is not a formula ({dice!r})")
        minimum = _integer(d.data, "minimum_difficulty", label, 2)
        maximum = _integer(d.data, "maximum_difficulty", label, 30)
        default = _integer(d.data, "default_difficulty", label, 8)
        if maximum < minimum:
            raise ContentError(f"{label}: 'maximum_difficulty' is below 'minimum_difficulty'")
        if not minimum <= default <= maximum:
            raise ContentError(f"{label}: 'default_difficulty' is outside the difficulty bounds")
        checks[d.id] = CheckRules(
            id=d.id,
            label=required_text(d.data, "label", label),
            dice=dice,
            unskilled=_integer(d.data, "unskilled", label, -1),
            default_difficulty=default,
            minimum_difficulty=minimum,
            maximum_difficulty=maximum,
        )
    return checks


def _integer(data: Mapping[str, Any], key: str, label: str, default: int) -> int:
    """A whole number of any sign, or `default` when the key is missing."""
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContentError(f"{label}: {key!r} must be a whole number")
    return value


def _load_moves(
    definitions: list[Definition],
    character: CharacterSpec,
    counters: frozenset[str],
    template: str,
) -> dict[str, MoveRules]:
    stored = frozenset(
        d.id for d in definitions if d.type == "condition" and d.data.get("derived") is None
    )
    moves: dict[str, MoveRules] = {}
    for d in definitions:
        if d.type != "move":
            continue
        label = f"{template}: move {d.id}"
        if d.id in moves:
            raise ContentError(f"{template}: duplicate move {d.id!r}")
        check_keys(
            d.data,
            {
                "type",
                "id",
                "label",
                "description",
                "dice",
                "stat",
                "stat_choice",
                "needs_all_conditions",
                "skill",
                "skill_floor",
                "bands",
                "tags",
            },
            label,
        )
        dice = required_text(d.data, "dice", label)
        if not is_formula(dice):
            raise ContentError(f"{label}: 'dice' is not a formula ({dice!r})")
        stat = d.data.get("stat")
        if stat == HIGHEST:
            if "stat_choice" not in d.data:
                raise ContentError(
                    f"{label}: stat {HIGHEST!r} needs a 'stat_choice' to choose from"
                )
            stat_choice = _load_stat_choice(d.data, None, character, label)
        else:
            if stat is not None and (not isinstance(stat, str) or stat not in character.stats):
                raise ContentError(f"{label}: 'stat' names no stat ({stat!r})")
            stat_choice = _load_stat_choice(d.data, stat, character, label)
        needs_all = _flag_default(d.data, "needs_all_conditions", label, False)
        if needs_all and not stored:
            raise ContentError(f"{label}: needs_all_conditions, but the rule set has no conditions")
        skill = d.data.get("skill", "")
        if not isinstance(skill, str) or (skill and not skill.strip()):
            raise ContentError(f"{label}: 'skill' must be the id of a skill")
        if skill and skill not in character.skills:
            raise ContentError(f"{label}: 'skill' names no skill ({skill!r})")
        skill_floor = d.data.get("skill_floor", 0)
        if skill_floor is not None and (
            isinstance(skill_floor, bool) or not isinstance(skill_floor, int)
        ):
            raise ContentError(f"{label}: 'skill_floor' must be a whole number or null")
        if "skill_floor" in d.data and not skill:
            raise ContentError(f"{label}: 'skill_floor' needs a 'skill'")
        tags = _text_list(d.data.get("tags", []), f"{label}: tags")
        if len(set(tags)) != len(tags):
            raise ContentError(f"{label}: 'tags' repeats a tag")
        moves[d.id] = MoveRules(
            id=d.id,
            label=required_text(d.data, "label", label),
            description=required_text(d.data, "description", label),
            dice=dice,
            stat=stat,
            bands=_load_bands(d.data.get("bands"), character, stored, counters, label),
            stat_choice=stat_choice,
            skill=skill,
            skill_floor=skill_floor,
            needs_all_conditions=needs_all,
            tags=tuple(tags),
        )
    return moves


def _check_feature_reactions(
    character: CharacterSpec,
    moves: Mapping[str, MoveRules],
    checks: Mapping[str, CheckRules],
    template: str,
) -> None:
    """Check the parts of a feature reaction that depend on the rule set's other content."""
    for feature in character.features.values():
        for reaction in feature.reactions:
            label = f"{template}: feature {feature.id} reaction {reaction.id}"
            when = reaction.when
            for move_id in when.get("moves", ()):
                if move_id not in moves:
                    raise ContentError(f"{label}: 'when.moves' names no move ({move_id!r})")
            for check_id in when.get("checks", ()):
                if check_id not in checks:
                    raise ContentError(f"{label}: 'when.checks' names no check ({check_id!r})")
            for stat in when.get("stats", ()):
                if stat not in character.stats:
                    raise ContentError(f"{label}: 'when.stats' names no stat ({stat!r})")
            for skill in when.get("skills", ()):
                if skill not in character.skills:
                    raise ContentError(f"{label}: 'when.skills' names no skill ({skill!r})")
            if (
                "min_total" in when
                and "max_total" in when
                and when["min_total"] > when["max_total"]
            ):
                raise ContentError(f"{label}: 'when.min_total' is above 'when.max_total'")
            candidates = _reaction_moves(moves, when)
            if reaction.trigger == "move" and not candidates:
                raise ContentError(f"{label}: no move matches this reaction's 'when' filters")
            target = reaction.effect.get("to") if reaction.effect["op"] == "raise_band" else None
            if target is not None:
                for move in candidates:
                    if all(band.label != target for band in move.bands):
                        raise ContentError(
                            f"{label}: raise_band target {target!r} is not a band of "
                            f"move {move.id!r}"
                        )
            for band in when.get("bands", ()):
                if not any(any(b.label == band for b in move.bands) for move in candidates):
                    raise ContentError(f"{label}: 'when.bands' names no band ({band!r})")


def _reaction_moves(moves: Mapping[str, MoveRules], when: Mapping[str, Any]) -> list[MoveRules]:
    """The moves a reaction's filters can match, in the rule set's file order."""
    named = set(when.get("moves", ()))
    tags = set(when.get("move_tags", ()))
    return [
        move
        for move in moves.values()
        if (not named or move.id in named) and (not tags or tags & set(move.tags))
    ]


def _load_stat_choice(
    data: Mapping[str, Any], stat: str | None, character: CharacterSpec, label: str
) -> tuple[str, ...]:
    """The optional `stat_choice` of a move: the stats the player picks from, when the move has
    no fixed `stat`. The player must pick one of them when rolling."""
    if "stat_choice" not in data:
        return ()
    if stat is not None:
        raise ContentError(f"{label}: 'stat_choice' cannot be given with a fixed 'stat'")
    choice = data["stat_choice"]
    if not isinstance(choice, list) or not choice:
        raise ContentError(f"{label}: 'stat_choice' must be a non-empty list of stats")
    for option in choice:
        if not isinstance(option, str) or option not in character.stats:
            raise ContentError(f"{label}: 'stat_choice' names no stat ({option!r})")
    if len(set(choice)) != len(choice):
        raise ContentError(f"{label}: 'stat_choice' repeats a stat")
    return tuple(choice)


def _load_bands(
    value: object,
    character: CharacterSpec,
    conditions: frozenset[str],
    counters: frozenset[str],
    label: str,
) -> tuple[MoveBand, ...]:
    if not isinstance(value, list) or not value:
        raise ContentError(f"{label}: 'bands' must be a non-empty list")
    bands: list[MoveBand] = []
    previous: int | None = None
    for index, band in enumerate(value):
        if not isinstance(band, Mapping):
            raise ContentError(f"{label}: each band must be an object")
        check_keys(band, {"label", "text", "min", "effects", "choices"}, label)
        last = index == len(value) - 1
        minimum = band.get("min")
        if last and minimum is not None:
            raise ContentError(f"{label}: the last band must have no 'min'")
        if not last:
            if not isinstance(minimum, int) or isinstance(minimum, bool):
                raise ContentError(f"{label}: every band but the last needs an integer 'min'")
            if previous is not None and minimum >= previous:
                raise ContentError(f"{label}: bands must run from the highest 'min' down")
        previous = minimum
        effects, _ = _check_effects(
            band.get("effects", []), label, character, conditions, False, counters=counters
        )
        text = band.get("text", "")
        if not isinstance(text, str):
            raise ContentError(f"{label}: band 'text' must be text")
        choices = _load_choices(
            band.get("choices"), character, conditions, counters, f"{label}: band {index + 1}"
        )
        bands.append(
            MoveBand(
                label=required_text(band, "label", label),
                text=text,
                min=minimum,
                effects=effects,
                choices=choices,
            )
        )
    return tuple(bands)


def _load_choices(
    value: object,
    character: CharacterSpec,
    conditions: frozenset[str],
    counters: frozenset[str],
    label: str,
) -> tuple[MoveChoice, ...]:
    """The costs a player may pick for a move band, each with its own effects."""
    if value is None:
        return ()
    if not isinstance(value, list) or not value:
        raise ContentError(f"{label}: 'choices' must be a non-empty list")
    choices: list[MoveChoice] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, Mapping):
            raise ContentError(f"{label}: each choice must be an object")
        check_keys(entry, {"id", "label", "text", "effects"}, f"{label}: choice")
        choice_id = required_text(entry, "id", f"{label}: choice")
        if choice_id in seen:
            raise ContentError(f"{label}: choice {choice_id!r} is repeated")
        seen.add(choice_id)
        text = entry.get("text", "")
        if not isinstance(text, str):
            raise ContentError(f"{label}: choice {choice_id!r} 'text' must be text")
        effects, _ = _check_effects(
            entry.get("effects", []),
            f"{label}: choice {choice_id}",
            character,
            conditions,
            False,
            counters=counters,
        )
        choices.append(
            MoveChoice(
                id=choice_id,
                label=required_text(entry, "label", f"{label}: choice {choice_id}"),
                text=text,
                effects=effects,
            )
        )
    return tuple(choices)


def _load_derived(value: object, character: CharacterSpec, label: str) -> DerivedRule | None:
    if value is None:
        return None
    value = check_object_keys(value, {"stat", "current", "needs_max"}, f"{label}: derived")
    stat = required_text(value, "stat", f"{label}: derived")
    if stat not in character.stats:
        raise ContentError(f"{label}: derived names no stat ({stat!r})")
    current = value.get("current")
    if not isinstance(current, int) or isinstance(current, bool) or current < 0:
        raise ContentError(f"{label}: derived 'current' must be a whole number of 0 or more")
    needs_max = _flag(value, "needs_max", f"{label}: derived")
    return DerivedRule(stat=stat, current=current, needs_max=needs_max)


def _load_injury_entries(
    definition: Definition,
    character: CharacterSpec,
    conditions: Mapping[str, ConditionRules],
    template: str,
) -> tuple[InjuryEntry, ...]:
    """An Injuries table: a non-empty, contiguous set of entries starting at roll 1."""
    label = f"{template}: injury table {definition.id}"
    check_keys(definition.data, {"type", "id", "name", "entries"}, label)
    entries = definition.data.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ContentError(f"{label}: 'entries' must be a non-empty list")
    stored = frozenset(cid for cid, rules in conditions.items() if rules.derived is None)
    loaded = []
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, Mapping):
            raise ContentError(f"{label}: each entry must be an object")
        entry_label = f"{label}: entry {position}"
        check_keys(entry, {"roll", "name", "text", "effects"}, entry_label)
        if entry.get("roll") != position:
            raise ContentError(f"{entry_label}: 'roll' must be {position}")
        effects, _ = _check_effects(entry.get("effects", []), entry_label, character, stored, False)
        loaded.append(
            InjuryEntry(
                roll=position,
                name=required_text(entry, "name", entry_label),
                text=required_text(entry, "text", entry_label),
                effects=effects,
            )
        )
    return tuple(loaded)


@dataclass
class _EffectCheck:
    """What an effect op's load-time check needs: the rule set's stats, stored conditions, and
    counters, and whether a `pick_stat` has supplied `$picked`."""

    label: str
    character: CharacterSpec
    conditions: frozenset[str]
    counters: frozenset[str] | None
    picked: bool = False


def _check_picked_stat(op: Mapping[str, Any], check: _EffectCheck, op_name: str) -> str:
    """The op's stat, checked against the rule set, allowing `$picked` only after a pick."""
    stat = required_text(op, "stat", check.label)
    if stat == PICKED:
        if not check.picked:
            raise ContentError(f"{check.label}: {PICKED!r} is used before a pick_stat")
    elif stat not in check.character.stats:
        raise ContentError(f"{check.label}: {op_name} names no stat ({stat!r})")
    return stat


def _check_note(op: Mapping[str, Any], check: _EffectCheck) -> None:
    required_text(op, "text", check.label)


def _check_set_max(op: Mapping[str, Any], check: _EffectCheck) -> None:
    _check_picked_stat(op, check, "set_max")
    dice = required_text(op, "dice", check.label)
    if not is_formula(dice):
        raise ContentError(f"{check.label}: set_max 'dice' is not a formula ({dice!r})")
    mode = required_text(op, "mode", check.label)
    if mode not in MAX_MODES:
        raise ContentError(f"{check.label}: set_max 'mode' must be one of {MAX_MODES}")


def _check_condition(op: Mapping[str, Any], check: _EffectCheck) -> None:
    condition = required_text(op, "id", check.label)
    if condition not in check.conditions:
        raise ContentError(f"{check.label}: condition op names no stored condition ({condition!r})")


def _check_save(op: Mapping[str, Any], check: _EffectCheck) -> None:
    stat = required_text(op, "stat", check.label)
    if stat not in check.character.stats:
        raise ContentError(f"{check.label}: save names no stat ({stat!r})")
    _check_effect_list(op.get("pass", []), check)
    _check_effect_list(op.get("fail", []), check)


def _check_pick_stat(op: Mapping[str, Any], check: _EffectCheck) -> None:
    options = op.get("options")
    if not isinstance(options, list) or not options:
        raise ContentError(f"{check.label}: pick_stat 'options' must be a non-empty list")
    for option in options:
        if option not in check.character.stats:
            raise ContentError(f"{check.label}: pick_stat names no stat ({option!r})")
    dice = required_text(op, "dice", check.label)
    if not DICE_SIDES.fullmatch(dice):
        raise ContentError(f"{check.label}: pick_stat 'dice' must be a single die such as '1d6'")
    check.picked = True


def _check_counter(op: Mapping[str, Any], check: _EffectCheck) -> None:
    counter = required_text(op, "id", check.label)
    if counter not in (check.counters or frozenset()):
        raise ContentError(f"{check.label}: counter op names no counter ({counter!r})")
    if sum(key in op for key in ("delta", "set", "reset")) != 1:
        raise ContentError(
            f"{check.label}: counter op needs exactly one of 'delta', 'set' or 'reset'"
        )
    if "reset" in op:
        if op["reset"] is not True:
            raise ContentError(f"{check.label}: counter op 'reset' must be true")
    else:
        amount = op.get("delta", op.get("set"))
        if not isinstance(amount, int) or isinstance(amount, bool):
            raise ContentError(f"{check.label}: counter op amount must be a whole number")


def _check_end_condition(op: Mapping[str, Any], check: _EffectCheck) -> None:
    condition = required_text(op, "id", check.label)
    if condition not in (*check.conditions, ALL_CONDITIONS):
        raise ContentError(
            f"{check.label}: end_condition names no stored condition ({condition!r})"
        )


def _check_retire(op: Mapping[str, Any], check: _EffectCheck) -> None:
    """A retire op has no payload beyond its name."""


def _check_raise_stat(op: Mapping[str, Any], check: _EffectCheck) -> None:
    _check_picked_stat(op, check, "raise_stat")
    amount = op.get("delta")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount < 1:
        raise ContentError(f"{check.label}: raise_stat 'delta' must be a positive whole number")
    cap = op.get("cap")
    if isinstance(cap, int) and not isinstance(cap, bool) and cap >= 1:
        return
    if isinstance(cap, Mapping):
        counter = required_text(cap, "counter", check.label)
        if check.counters is None or counter not in check.counters:
            raise ContentError(f"{check.label}: raise_stat cap names no counter ({counter!r})")
        bands = cap.get("bands")
        if not isinstance(bands, Mapping) or not bands:
            raise ContentError(f"{check.label}: raise_stat cap 'bands' must be a non-empty object")
        for key, value in bands.items():
            try:
                year = int(key)
            except (TypeError, ValueError) as exc:
                raise ContentError(
                    f"{check.label}: raise_stat cap bands must map whole years to positive caps"
                ) from exc
            if (
                isinstance(key, bool)
                or year < 0
                or isinstance(value, bool)
                or not isinstance(value, int)
                or value < 1
            ):
                raise ContentError(
                    f"{check.label}: raise_stat cap bands must map whole years to positive caps"
                )
        return
    raise ContentError(
        f"{check.label}: raise_stat 'cap' must be a positive whole number or a counter cap"
    )


@dataclass(frozen=True)
class EffectOpRules:
    """One effect op: the payload keys it reads, the check its payload must pass at load time, and
    whether only moves and counters may use it (they carry the rule set's counters and conditions
    into the effect)."""

    keys: frozenset[str]
    check: Callable[[Mapping[str, Any], _EffectCheck], None]
    move_only: bool = False


# Every effect op the rule set's JSON may use, in the order the engine reports them. Adding an op
# means registering its keys and check here and its executor in services.effects.
EFFECT_RULES: Mapping[str, EffectOpRules] = {
    "note": EffectOpRules(frozenset({"op", "text"}), _check_note),
    "set_max": EffectOpRules(frozenset({"op", "stat", "dice", "mode"}), _check_set_max),
    "condition": EffectOpRules(frozenset({"op", "id"}), _check_condition),
    "save": EffectOpRules(frozenset({"op", "stat", "pass", "fail"}), _check_save),
    "pick_stat": EffectOpRules(frozenset({"op", "options", "dice"}), _check_pick_stat),
    "counter": EffectOpRules(
        frozenset({"op", "id", "delta", "set", "reset"}), _check_counter, move_only=True
    ),
    "end_condition": EffectOpRules(frozenset({"op", "id"}), _check_end_condition, move_only=True),
    "retire": EffectOpRules(frozenset({"op"}), _check_retire),
    "raise_stat": EffectOpRules(frozenset({"op", "stat", "delta", "cap"}), _check_raise_stat),
}


def _check_effects(
    ops: object,
    label: str,
    character: CharacterSpec,
    conditions: frozenset[str],
    picked: bool,
    *,
    counters: frozenset[str] | None = None,
) -> tuple[tuple[Mapping[str, Any], ...], bool]:
    """Validate a list of effect ops, in order. `picked` says whether a `pick_stat` came before,
    so `$picked` may be used. Returns the ops and whether a stat has been picked by the end.
    `counters` is the ids of the rule set's counters, given only where move-only ops are allowed."""
    check = _EffectCheck(
        label=label,
        character=character,
        conditions=conditions,
        counters=counters,
        picked=picked,
    )
    checked = _check_effect_list(ops, check)
    return checked, check.picked


def _check_effect_list(ops: object, check: _EffectCheck) -> tuple[Mapping[str, Any], ...]:
    """Check one list of ops against the registry, updating `check.picked` as ops pick a stat."""
    if not isinstance(ops, list):
        raise ContentError(f"{check.label}: effects must be a list")
    checked: list[Mapping[str, Any]] = []
    for op in ops:
        if not isinstance(op, Mapping):
            raise ContentError(f"{check.label}: each effect must be an object")
        name = required_text(op, "op", check.label)
        rules = EFFECT_RULES.get(name)
        if rules is None:
            raise ContentError(
                f"{check.label}: effect op {name!r} is not one of {tuple(EFFECT_RULES)}"
            )
        check_keys(op, rules.keys, check.label)
        if rules.move_only and check.counters is None:
            raise ContentError(
                f"{check.label}: effect op {name!r} is only allowed in moves and counters"
            )
        rules.check(op, check)
        checked.append(op)
    return tuple(checked)


def _load_table(definition: Definition, template: str) -> tuple[TableOption, ...]:
    label = f"{template}: table {definition.id}"
    check_keys(definition.data, {"type", "id", "name", "options"}, label)
    options = definition.data.get("options")
    if not isinstance(options, list) or not options:
        raise ContentError(f"{label}: 'options' must be a non-empty list")
    loaded = tuple(_load_option(option, label) for option in options)
    if len({option.value for option in loaded}) != len(loaded):
        raise ContentError(f"{label}: every option must be unique")
    return loaded


def _load_option(option: object, label: str) -> TableOption:
    """An option is text, or an object with a `name` and a `description`, and optionally a
    `group`."""
    if isinstance(option, str) and option.strip():
        return TableOption(name=None, description=option)
    if (
        isinstance(option, dict)
        and set(option) <= {"name", "description", "group"}
        and {"name", "description"} <= set(option)
        and all(isinstance(option[key], str) and option[key].strip() for key in option)
    ):
        return TableOption(
            name=option["name"], description=option["description"], group=option.get("group")
        )
    raise ContentError(
        f"{label}: every option must be non-empty text or an object with "
        "non-empty 'name' and 'description' text, and an optional 'group'"
    )


def _dice_row(row: object, index: int, label: str) -> object:
    """A row's fields. The compact array form, `[roll, result]` or `[roll, result, text]`,
    names the same fields as the explicit object form."""
    if not isinstance(row, list):
        return row
    if len(row) not in (2, 3):
        raise ContentError(f"{label}: row {index} array must have 2 or 3 values")
    if not all(isinstance(value, str) for value in row):
        raise ContentError(f"{label}: row {index} array must hold text")
    return dict(zip(("roll", "result", "text"), row))


def _load_dice_table(definition: Definition, template: str) -> DiceTable:
    """A dice table has a single dice formula and rows of `roll` ("4" or "2-3") and `result`.
    A row is an object with `roll`, `result`, and optional `text`, or the compact array
    `[roll, result]` or `[roll, result, text]`. The rows must run in order with no gaps,
    covering every roll the dice can show. `genres` says which kinds of setting the table
    fits, and an absent list fits every setting."""
    label = f"{template}: dice table {definition.id}"
    check_keys(definition.data, {"type", "id", "name", "dice", "rows", "genres"}, label)
    name = _table_label(definition, template)
    genres = read_table_genres(definition.data.get("genres"), f"{label}: 'genres'")
    dice = definition.data.get("dice")
    formula = FORMULA.fullmatch(dice) if isinstance(dice, str) else None
    if formula is None:
        raise ContentError(f"{label}: 'dice' must be one dice formula such as '2d6'")
    low, high = int(formula.group(1)), int(formula.group(1)) * int(formula.group(2))
    rows = definition.data.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ContentError(f"{label}: 'rows' must be a non-empty list")
    loaded: list[DiceRow] = []
    for index, raw_row in enumerate(rows):
        row = _dice_row(raw_row, index, label)
        if not isinstance(row, dict) or not {"roll", "result"} <= set(row) <= {
            "roll",
            "result",
            "text",
        }:
            raise ContentError(
                f"{label}: row {index} needs 'roll' and 'result' and optional 'text' and no other keys"
            )
        roll, result = row["roll"], row["result"]
        text = row.get("text", "")
        bounds = ROLL_RANGE.fullmatch(roll) if isinstance(roll, str) else None
        if bounds is None:
            raise ContentError(
                f"{label}: row {index} 'roll' must be a number or a range like '3-5'"
            )
        start = int(bounds.group(1))
        end = int(bounds.group(2) or bounds.group(1))
        if start > end:
            raise ContentError(f"{label}: row {index} range {roll!r} runs backwards")
        if not isinstance(result, str) or not result.strip():
            raise ContentError(f"{label}: row {index} 'result' must be non-empty text")
        if "text" in row and (not isinstance(text, str) or not text.strip()):
            raise ContentError(f"{label}: row {index} 'text' must be non-empty text")
        loaded.append(DiceRow(start, end, result, text))
    expected = low
    for row in loaded:
        if row.low != expected:
            raise ContentError(
                f"{label}: rows must run from {low} to {high} without gaps or overlaps; "
                f"{row.low} is out of place"
            )
        expected = row.high + 1
    if expected != high + 1:
        raise ContentError(f"{label}: rows must cover every roll up to {high}")
    return DiceTable(id=definition.id, name=name, dice=dice, rows=tuple(loaded), genres=genres)


def _table_label(definition: Definition, template: str) -> str:
    """The name a player or the GM sees for a table. It defaults to the table id."""
    name = definition.data.get("name", definition.id)
    if not isinstance(name, str) or not name.strip():
        raise ContentError(f"{template}: table {definition.id}: 'name' must be non-empty text")
    return name


def _import_module(definition: Definition, template: str) -> ModuleType:
    label = f"{template}: python_module {definition.id}"
    check_keys(definition.data, {"type", "id", "module"}, label)
    name = required_text(definition.data, "module", label)
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise ContentError(f"{label}: cannot import {name!r}: {exc}") from exc


def _load_tool_call(
    definition: Definition,
    modules: Mapping[str, ModuleType],
    character: CharacterSpec,
    template: str,
    npc_hp: Mapping[str, int],
    travel: Travel | None = None,
    enum_sources: Mapping[str, Sequence[str]] | None = None,
    *,
    capabilities: Capabilities | None = None,
) -> ToolCall:
    """Load one tool_call definition, supplying what the JSON leaves out.

    `name` defaults to the tool id, `world_method` defaults to the id when the definition names
    no Python handler, and an omitted `parameters` loads as an empty object schema. Omitting
    `required` inside a schema reads as no required arguments. The defaults are only defaults:
    whatever they produce is validated exactly as an explicit value would be."""
    label = f"{template}: tool_call {definition.id}"
    data = definition.data
    check_keys(
        data,
        {
            "type",
            "id",
            "name",
            "description",
            "python_module",
            "function",
            "world_method",
            "bind",
            "rename",
            "parameters",
            "player_input",
            "activity",
            "character_options",
            "lookup",
            "journal_step",
            "review_step",
            "npc_hp_tier",
            "travel",
            "requires",
            "enabled_by",
        },
        label,
    )
    requires, enabled_by = _read_gate(data, label)
    _check_definition_gate(capabilities, requires, enabled_by, label)
    function, world_method = _tool_handler(data, modules, definition.id, label)
    inspected = function
    if inspected is None and world_method is not None:
        inspected = _world_method(world_method, label)
    accepted = _accepted_parameters(inspected, unbound_method=world_method is not None)
    name = data.get("name", definition.id)
    if not isinstance(name, str) or not TOOL_NAME.fullmatch(name):
        raise ContentError(f"{label}: 'name' must be 1 to 64 letters, digits, '_' or '-'")
    schema = data["parameters"] if "parameters" in data else _empty_parameters()
    parameters = _with_enum_from(
        _check_parameters(schema, label),
        enum_sources or {"stats": sorted(character.stats)},
        character,
        label,
    )
    if _flag(data, "character_options", label):
        parameters = _with_character(parameters, character, label)
    if _flag(data, "npc_hp_tier", label) and npc_hp:
        parameters = _with_npc_hp_tier(parameters, npc_hp, label)
    if _flag(data, "travel", label) and travel is not None:
        parameters = _with_travel(parameters, travel, label)
    if CONTEXT_ARGUMENT in parameters["properties"]:
        raise ContentError(f"{label}: {CONTEXT_ARGUMENT!r} is reserved and not a parameter")
    bind, rename = _tool_binding(data, parameters, accepted, world_method is not None, label)
    player_input = _check_player_input(data.get("player_input"), parameters, accepted, label)
    activity = _check_activity(data.get("activity"), label)
    _check_signature(
        accepted,
        parameters,
        label,
        consumed=_consumed(player_input),
        bind=bind,
        rename=rename,
    )
    return ToolCall(
        id=definition.id,
        name=name,
        description=required_text(data, "description", label),
        parameters=parameters,
        function=function,
        world_method=world_method,
        bind=bind,
        rename=rename,
        player_input=player_input,
        activity=activity,
        takes_context=world_method is None and CONTEXT_ARGUMENT in accepted,
        lookup=_flag(data, "lookup", label),
        journal_step=_flag(data, "journal_step", label),
        review_step=_flag(data, "review_step", label),
        requires=requires,
        enabled_by=enabled_by,
    )


def _tool_handler(
    data: Mapping[str, Any], modules: Mapping[str, ModuleType], tool_id: str, label: str
) -> tuple[Callable[..., Any] | None, str | None]:
    """What runs a tool: a rule set module's function, or the name of a WorldTools method.

    A tool names at most one of them. `world_method` binds an engine operation directly, so the
    rule set ships no one-line wrapper. When a tool names no handler at all, its id is the
    WorldTools method, which is the common case and needs no line. Naming both hides which one
    runs; naming neither when the id is no method leaves nothing to run."""
    if "world_method" in data:
        if "python_module" in data or "function" in data:
            raise ContentError(
                f"{label}: give either 'world_method' or 'python_module' and 'function', not both"
            )
        method_name = required_text(data, "world_method", label)
        _world_method(method_name, label)
        return None, method_name
    if "python_module" in data or "function" in data:
        if "python_module" not in data or "function" not in data:
            raise ContentError(f"{label}: {TOOL_HANDLER_HINT}")
        module_id = required_text(data, "python_module", label)
        if module_id not in modules:
            raise ContentError(f"{label}: unknown python_module {module_id!r}")
        function_name = required_text(data, "function", label)
        function = getattr(modules[module_id], function_name, None)
        if not callable(function):
            raise ContentError(f"{label}: {function_name!r} is not a function in {module_id!r}")
        return function, None
    try:
        _world_method(tool_id, label)
    except ContentError:
        raise ContentError(f"{label}: {TOOL_HANDLER_HINT}") from None
    return None, tool_id


def _world_method(name: str, label: str) -> Callable[..., Any]:
    """Check that `name` is a WorldTools method and return the unbound function.

    The import is lazy: the world service imports this module, so importing it here at module
    load would close the cycle."""
    from cygnusloom.services.world import WorldTools

    method = getattr(WorldTools, name, None)
    if not callable(method):
        raise ContentError(f"{label}: {name!r} is not a method of WorldTools")
    return method


def _tool_binding(
    data: Mapping[str, Any],
    parameters: Mapping[str, Any],
    accepted: Mapping[str, inspect.Parameter],
    bound_method: bool,
    label: str,
) -> tuple[Mapping[str, Any], Mapping[str, str]]:
    """The arguments a bound tool adds to, and renames for, its WorldTools method.

    `bind` supplies an argument the GM never gives, such as a fixed role or a check id. A value
    that is an object builds one argument from the schema arguments it names, such as a
    statblock's stats from flat numbers. `rename` passes a schema argument under the method's own
    name. Both are optional, and both are checked against the method and the schema so a mismatch
    fails at load time."""
    bind = data.get("bind", {})
    rename = data.get("rename", {})
    if not isinstance(bind, Mapping):
        raise ContentError(f"{label}: 'bind' must be an object")
    if not isinstance(rename, Mapping):
        raise ContentError(f"{label}: 'rename' must be an object")
    if not bind and not rename:
        return {}, {}
    if not bound_method:
        raise ContentError(f"{label}: 'bind' and 'rename' need 'world_method'")
    properties = parameters["properties"]
    fixed: dict[str, Any] = {}
    sources: set[str] = set()
    for target, value in bind.items():
        if target not in accepted:
            raise ContentError(f"{label}: 'bind' names {target!r}, which the method does not take")
        if target in properties:
            raise ContentError(f"{label}: 'bind' target {target!r} is also a schema argument")
        if target in rename.values():
            raise ContentError(f"{label}: 'bind' target {target!r} is also a rename target")
        if isinstance(value, Mapping):
            built: dict[str, str] = {}
            for key, source in value.items():
                if not isinstance(source, str) or source not in properties:
                    raise ContentError(
                        f"{label}: 'bind' {target!r} names {source!r}, "
                        "which is not a schema argument"
                    )
                built[str(key)] = source
                sources.add(source)
            fixed[target] = built
        else:
            fixed[target] = value
    renamed: dict[str, str] = {}
    for source, target in rename.items():
        if source not in properties:
            raise ContentError(
                f"{label}: 'rename' names {source!r}, which is not a schema argument"
            )
        if not isinstance(target, str) or not target:
            raise ContentError(
                f"{label}: 'rename' must give the method parameter {source!r} becomes"
            )
        if target not in accepted:
            raise ContentError(
                f"{label}: 'rename' target {target!r} is not a parameter of the method"
            )
        if target in properties:
            raise ContentError(f"{label}: 'rename' target {target!r} is also a schema argument")
        if target in renamed.values():
            raise ContentError(f"{label}: 'rename' sends two arguments to {target!r}")
        renamed[source] = target
    for source in renamed:
        if source in sources:
            raise ContentError(f"{label}: 'bind' builds {source!r}, which 'rename' also renames")
    return fixed, renamed


def _accepted_parameters(
    function: Callable[..., Any] | None, unbound_method: bool = False
) -> Mapping[str, inspect.Parameter]:
    """The parameters the engine may pass. A world method is an unbound class function, so its
    signature includes `self`, which the engine never passes. It never takes the tool context
    either, so `ctx` is left out too."""
    if function is None:
        raise ContentError("a tool with 'world_method' has no Python function to inspect")
    parameters = inspect.signature(function).parameters
    if not unbound_method:
        return parameters
    return {
        name: parameter
        for name, parameter in parameters.items()
        if name not in (SELF_ARGUMENT, CONTEXT_ARGUMENT)
    }


def _npc_hp(data: Mapping[str, Any], label: str) -> dict[str, int]:
    """The HP tiers for NPCs with no statblock, by tier name. The tier names are free text."""
    tiers = data.get("npc_hp", {})
    if not isinstance(tiers, dict):
        raise ContentError(f"{label}: 'npc_hp' must map tier names to HP")
    for tier in tiers:
        if not tier.strip():
            raise ContentError(f"{label}: 'npc_hp' tier names must not be blank")
    return {tier: _whole(tiers, tier, f"{label}: npc_hp", minimum=1) for tier in tiers}


def _plus_mode(data: Mapping[str, Any], label: str) -> str:
    mode = data.get("plus", "sum")
    if mode not in PLUS_MODES:
        raise ContentError(f"{label}: 'plus' must be one of {', '.join(PLUS_MODES)}")
    return mode


def _flag(data: Mapping[str, Any], key: str, label: str) -> bool:
    flag = data.get(key, False)
    if not isinstance(flag, bool):
        raise ContentError(f"{label}: {key!r} must be true or false")
    return flag


def _with_character(
    parameters: dict[str, Any], character: CharacterSpec, label: str
) -> dict[str, Any]:
    """Add the rule set's character options as a `character` argument. It is optional, so the
    GM may create an NPC with no options and the rest are rolled or left blank."""
    if CHARACTER_ARGUMENT in parameters["properties"]:
        raise ContentError(f"{label}: {CHARACTER_ARGUMENT!r} is generated and must not be declared")
    properties = {**parameters["properties"], CHARACTER_ARGUMENT: character_schema(character)}
    return {**parameters, "properties": properties}


def _with_npc_hp_tier(
    parameters: dict[str, Any], npc_hp: Mapping[str, int], label: str
) -> dict[str, Any]:
    """Add the rule set's HP tiers as an optional `hp_tier` argument. A rule set with no tiers gets no
    argument."""
    if NPC_HP_TIER_ARGUMENT in parameters["properties"]:
        raise ContentError(
            f"{label}: {NPC_HP_TIER_ARGUMENT!r} is generated and must not be declared"
        )
    tier = {
        "type": "string",
        "enum": sorted(npc_hp),
        "description": "The HP tier of an NPC with no statblock, set to the tier their role in the story calls for.",
    }
    properties = {**parameters["properties"], NPC_HP_TIER_ARGUMENT: tier}
    return {**parameters, "properties": properties}


def _with_enum_from(
    parameters: dict[str, Any],
    sources: Mapping[str, Sequence[str]],
    character: CharacterSpec,
    label: str,
) -> dict[str, Any]:
    """Fill in the enum of each property that names a source list, such as the rule set's moves.
    A property may name the source as text, or as an object whose `source` is narrowed by
    `feature_type`, `tag`, and `max_rank` when the source is `features`."""
    properties: dict[str, Any] = {}
    for name, prop in parameters["properties"].items():
        source = prop.get(ENUM_FROM_KEY) if isinstance(prop, dict) else None
        if source is None:
            properties[name] = prop
            continue
        filled = {k: v for k, v in prop.items() if k != ENUM_FROM_KEY}
        options = _enum_options(name, source, sources, character, label)
        # A rule set that defines nothing for the source, such as core before it inherits a
        # rule set, leaves the property without choices rather than rejecting the tool.
        if options:
            filled["enum"] = options
            default = filled.get("default")
            if default is not None and default not in options:
                raise ContentError(f"{label}: {name!r} default is not one of its enum_from choices")
        properties[name] = filled
    return {**parameters, "properties": properties}


# The keys an `enum_from` object may take besides `source`.
ENUM_FROM_FILTERS = frozenset({"feature_type", "tag", "max_rank"})


def _enum_options(
    name: str,
    source: object,
    sources: Mapping[str, Sequence[str]],
    character: CharacterSpec,
    label: str,
) -> list[str]:
    """The ids an `enum_from` names, filtered when it is an object."""
    where = f"{label}: {name!r}"
    if isinstance(source, str):
        return _enum_source(source, sources, where)
    if not isinstance(source, Mapping):
        raise ContentError(f"{where} has enum_from that must be a source name or an object")
    unknown = set(source) - ENUM_FROM_FILTERS - {"source"}
    if unknown:
        raise ContentError(f"{where}: enum_from does not take {', '.join(sorted(unknown))}")
    source_name = required_text(source, "source", f"{where}: enum_from")
    options = _enum_source(source_name, sources, where)
    filters = set(source) - {"source"}
    if not filters:
        return options
    if source_name != "features":
        raise ContentError(f"{where}: only a 'features' enum_from may be filtered")
    feature_type = _optional_nonempty_text(source, "feature_type", f"{where}: enum_from")
    if feature_type is not None and feature_type not in character.feature_types:
        raise ContentError(
            f"{where}: enum_from 'feature_type' must be one of {', '.join(character.feature_types)}"
        )
    tag = _optional_nonempty_text(source, "tag", f"{where}: enum_from")
    max_rank = source.get("max_rank")
    if "max_rank" in source and (
        isinstance(max_rank, bool) or not isinstance(max_rank, int) or max_rank < 0
    ):
        raise ContentError(f"{where}: enum_from 'max_rank' must be a whole number, 0 or more")
    chosen = []
    for feature_id in options:
        feature = character.features[feature_id]
        if feature_type is not None and feature.feature_type != feature_type:
            continue
        if tag is not None and feature.tag != tag:
            continue
        if max_rank is not None and feature.rank > max_rank:
            continue
        chosen.append(feature_id)
    return chosen


def _enum_source(source: str, sources: Mapping[str, Sequence[str]], label: str) -> list[str]:
    if source not in sources:
        raise ContentError(
            f"{label} has enum_from {source!r}, which is not one of {sorted(sources)}"
        )
    return list(sources[source])


def _with_travel(parameters: dict[str, Any], travel: Travel, label: str) -> dict[str, Any]:
    """Add the rule set's travel modes as an optional `travel` argument."""
    if TRAVEL_ARGUMENT in parameters["properties"]:
        raise ContentError(f"{label}: {TRAVEL_ARGUMENT!r} is generated and must not be declared")
    default = travel.default_mode
    mode = {
        "type": "string",
        "enum": sorted(travel.modes),
        "default": default,
        "description": f"How the party travels. Defaults to {default}.",
    }
    properties = {**parameters["properties"], TRAVEL_ARGUMENT: mode}
    return {**parameters, "properties": properties}


def _consumed(player_input: PlayerInput | None) -> frozenset[str]:
    """Schema arguments the engine uses itself rather than passing to the function."""
    if player_input is None:
        return frozenset()
    consumed = {PROMPT_ARGUMENT}
    if player_input.dice_param:
        consumed.add(player_input.dice_param)
    return frozenset(consumed)


def _check_player_input(
    data: object,
    parameters: Mapping[str, Any],
    accepted: Mapping[str, inspect.Parameter],
    label: str,
) -> PlayerInput | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ContentError(f"{label}: 'player_input' must be an object")
    check_keys(
        data,
        {
            "param",
            "dice",
            "dice_param",
            "move_param",
            "check_param",
            "weapon_param",
            "weapon_roll_param",
            "details",
            "modifier_param",
        },
        f"{label}: player_input",
    )
    dice = data.get("dice")
    dice_param = data.get("dice_param")
    move_param = data.get("move_param")
    check_param = data.get("check_param")
    weapon_param = data.get("weapon_param")
    weapon_roll_param = None
    modifier_param = data.get("modifier_param")
    sources = [
        source
        for source in (dice, dice_param, move_param, check_param, weapon_param)
        if source is not None
    ]
    if len(sources) != 1:
        raise ContentError(
            f"{label}: 'player_input' needs exactly one of 'dice', 'dice_param', "
            "'move_param', 'check_param', or 'weapon_param'"
        )
    for key, value in (("move_param", move_param), ("check_param", check_param)):
        if value is None:
            continue
        schema = parameters["properties"].get(value)
        if not isinstance(schema, dict) or schema.get("type") != "string":
            raise ContentError(f"{label}: 'player_input.{key}' must name a string parameter")
        if value not in parameters.get("required", []):
            raise ContentError(f"{label}: 'player_input.{key}' must name a required parameter")
    if modifier_param is not None:
        if dice_param is None and weapon_param is None:
            raise ContentError(
                f"{label}: 'player_input.modifier_param' needs 'dice_param' or 'weapon_param'"
            )
        schema = parameters["properties"].get(modifier_param)
        if not isinstance(schema, dict) or schema.get("type") != "string":
            raise ContentError(
                f"{label}: 'player_input.modifier_param' must name a string parameter"
            )
    if dice is not None and not is_formula(dice):
        raise ContentError(f"{label}: 'player_input.dice' must be a formula such as '1d20'")
    if dice_param is not None:
        schema = parameters["properties"].get(dice_param)
        if not isinstance(schema, dict) or schema.get("type") != "string":
            raise ContentError(f"{label}: 'player_input.dice_param' must name a string parameter")
        if dice_param not in parameters.get("required", []):
            raise ContentError(f"{label}: 'player_input.dice_param' must be a required parameter")
    if weapon_param is not None:
        schema = parameters["properties"].get(weapon_param)
        if not isinstance(schema, dict) or schema.get("type") != "string":
            raise ContentError(f"{label}: 'player_input.weapon_param' must name a string parameter")
        if weapon_param not in parameters.get("required", []):
            raise ContentError(f"{label}: 'player_input.weapon_param' must be a required parameter")
    param = data.get("param")
    if weapon_param is not None:
        if param is not None:
            raise ContentError(
                f"{label}: 'player_input.param' cannot be set with 'weapon_param', "
                "because the engine rolls the weapon's dice"
            )
        weapon_roll_param = data.get("weapon_roll_param")
        if weapon_roll_param is not None:
            if not isinstance(weapon_roll_param, str) or not weapon_roll_param:
                raise ContentError(
                    f"{label}: 'player_input.weapon_roll_param' must name a function parameter"
                )
            if weapon_roll_param in parameters["properties"]:
                raise ContentError(
                    f"{label}: 'player_input.weapon_roll_param' must not be a schema parameter"
                )
            if weapon_roll_param not in accepted:
                raise ContentError(
                    f"{label}: function does not accept parameter {weapon_roll_param!r}"
                )
    elif data.get("weapon_roll_param") is not None:
        raise ContentError(f"{label}: 'player_input.weapon_roll_param' needs 'weapon_param'")
    elif not isinstance(param, str) or not param:
        raise ContentError(f"{label}: 'player_input.param' must name a function parameter")
    prompt = parameters["properties"].get(PROMPT_ARGUMENT)
    if not isinstance(prompt, dict) or prompt.get("type") != "string":
        raise ContentError(
            f"{label}: a tool with 'player_input' needs a string '{PROMPT_ARGUMENT}' parameter"
        )
    if PROMPT_ARGUMENT not in parameters.get("required", []):
        raise ContentError(f"{label}: '{PROMPT_ARGUMENT}' must be required")
    if param is not None:
        if param in parameters["properties"]:
            raise ContentError(f"{label}: 'player_input.param' must not be a schema parameter")
        if param not in accepted:
            raise ContentError(f"{label}: function does not accept parameter {param!r}")
    details = _check_template(data.get("details"), "player_input.details", label)
    if details is not None:
        _check_fields(details, parameters["properties"], "player_input.details", label)
    return PlayerInput(
        dice=dice,
        dice_param=dice_param,
        move_param=move_param,
        check_param=check_param,
        weapon_param=weapon_param,
        weapon_roll_param=weapon_roll_param,
        param=param,
        details=details,
        modifier_param=modifier_param,
    )


def _check_activity(template: object, label: str) -> str | None:
    return _check_template(template, "activity", label)


def _check_template(template: object, key: str, label: str) -> str | None:
    if template is None:
        return None
    if not isinstance(template, str) or not template.strip():
        raise ContentError(f"{label}: '{key}' must be a non-empty text template")
    try:
        list(string.Formatter().parse(template))
    except ValueError as exc:
        raise ContentError(f"{label}: '{key}' is not a valid template: {exc}") from exc
    return template


def _check_fields(template: str, arguments: Mapping[str, Any], key: str, label: str) -> None:
    """A details template may only name schema arguments, so a typo fails at load."""
    for _, name, _, _ in string.Formatter().parse(template):
        if name and name not in arguments:
            raise ContentError(f"{label}: '{key}' names unknown argument {name!r}")


SCHEMA_TYPES = frozenset({"string", "integer", "number", "boolean", "array", "object", "null"})
SCHEMA_KEYS = frozenset(
    {
        "type",
        "description",
        "properties",
        "required",
        "enum",
        "default",
        "items",
        "additionalProperties",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "enum_from",
    }
)
UNSUPPORTED_SCHEMA_KEYS = ("$ref", "anyOf", "oneOf", "allOf")
# The scalar types an `enum` list may hold.
ENUM_TYPES = frozenset({"string", "integer", "number", "boolean", "null"})


def _schema_value_matches(value: object, kind: str) -> bool:
    """Whether a JSON value is of one schema type. A boolean is not an integer."""
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "array":
        return isinstance(value, list)
    if kind == "object":
        return isinstance(value, Mapping)
    if kind == "null":
        return value is None
    return False


def _empty_parameters() -> dict[str, Any]:
    """The parameter schema of a tool that takes no arguments."""
    return {"type": "object", "properties": {}}


def _check_parameters(schema: object, label: str) -> dict[str, Any]:
    """Check a tool's parameter schema strictly: every nested node must use a supported type
    and only the schema keys the engine understands. A `$ref`, `anyOf`, `oneOf`, or `allOf` is
    rejected rather than handed to a provider that may interpret it differently.

    An absent `parameters` reaches here as an empty object schema. A root schema with no `type`
    is the common case, so it reads as `object`; a nested node still needs its type. `required`
    may be omitted, which reads as no required arguments; when given, it must list names from
    `properties`."""
    if not isinstance(schema, dict) or ("type" in schema and schema["type"] != "object"):
        raise ContentError(f"{label}: 'parameters' must be an object schema with type 'object'")
    checked = schema if "type" in schema else {"type": "object", **schema}
    properties = checked.get("properties")
    if not isinstance(properties, dict):
        raise ContentError(f"{label}: 'parameters' needs a 'properties' object")
    _check_schema_node(checked, label, root=True)
    return checked


def _check_schema_node(node: object, label: str, *, root: bool = False) -> None:
    if not isinstance(node, dict):
        raise ContentError(f"{label}: a schema node must be an object")
    unsupported = [key for key in UNSUPPORTED_SCHEMA_KEYS if key in node]
    if unsupported:
        names = ", ".join(repr(key) for key in unsupported)
        raise ContentError(f"{label}: schema key(s) {names} are not supported")
    check_keys(node, SCHEMA_KEYS, label)
    kind = node.get("type")
    if kind not in SCHEMA_TYPES:
        raise ContentError(
            f"{label}: schema 'type' must be one of {', '.join(sorted(SCHEMA_TYPES))}"
        )
    if root and kind != "object":
        raise ContentError(f"{label}: the parameter schema must have type 'object'")
    if "description" in node and not isinstance(node["description"], str):
        raise ContentError(f"{label}: schema 'description' must be text")
    if "enum" in node:
        if kind not in ENUM_TYPES:
            raise ContentError(f"{label}: schema 'enum' needs a scalar type")
        values = node["enum"]
        if not isinstance(values, list) or not values:
            raise ContentError(f"{label}: schema 'enum' must be a non-empty list")
        if not all(_schema_value_matches(value, kind) for value in values):
            raise ContentError(f"{label}: schema 'enum' has a value that is not a {kind}")
        if len({json.dumps(value, sort_keys=True) for value in values}) != len(values):
            raise ContentError(f"{label}: schema 'enum' repeats a value")
    if "default" in node:
        default = node["default"]
        if not _schema_value_matches(default, kind):
            raise ContentError(f"{label}: schema 'default' is not a {kind}")
        if "enum" in node and default not in node["enum"]:
            raise ContentError(f"{label}: schema 'default' is not one of its 'enum'")
    if "enum_from" in node:
        if "enum" in node:
            raise ContentError(f"{label}: schema 'enum_from' cannot be combined with 'enum'")
        if kind != "string":
            raise ContentError(f"{label}: schema 'enum_from' needs a string property")
        source = node["enum_from"]
        if isinstance(source, str):
            if not source:
                raise ContentError(f"{label}: schema 'enum_from' must name a source")
        elif not isinstance(source, Mapping):
            raise ContentError(f"{label}: schema 'enum_from' must be a source name or an object")
    if "items" in node:
        if kind != "array":
            raise ContentError(f"{label}: schema 'items' needs an array property")
        _check_schema_node(node["items"], f"{label}: items")
    elif kind == "array":
        raise ContentError(f"{label}: an array schema needs 'items'")
    if "properties" in node:
        if kind != "object":
            raise ContentError(f"{label}: schema 'properties' needs an object property")
        properties = node["properties"]
        if not isinstance(properties, Mapping):
            raise ContentError(f"{label}: schema 'properties' must be an object")
        for name, prop in properties.items():
            if not isinstance(name, str) or not name:
                raise ContentError(f"{label}: schema property names must be non-empty text")
            _check_schema_node(prop, f"{label}: property {name!r}")
    elif kind == "object" and "additionalProperties" not in node:
        raise ContentError(f"{label}: an object schema needs 'properties'")
    if "required" in node:
        if kind != "object":
            raise ContentError(f"{label}: schema 'required' needs an object property")
        required = node["required"]
        if not isinstance(required, list) or not all(
            isinstance(item, str) and item in (node.get("properties") or {}) for item in required
        ):
            raise ContentError(f"{label}: 'required' must list names from 'properties'")
        if len(set(required)) != len(required):
            raise ContentError(f"{label}: 'required' repeats a name")
    if "additionalProperties" in node:
        if kind != "object":
            raise ContentError(f"{label}: schema 'additionalProperties' needs an object property")
        extra = node["additionalProperties"]
        if isinstance(extra, Mapping):
            _check_schema_node(extra, f"{label}: additionalProperties")
        elif not isinstance(extra, bool):
            raise ContentError(
                f"{label}: schema 'additionalProperties' must be true, false, or a schema"
            )
    if kind in ("integer", "number"):
        for key in ("minimum", "maximum"):
            if key in node and not _schema_value_matches(node[key], "number"):
                raise ContentError(f"{label}: schema {key!r} must be a number")
        if "minimum" in node and "maximum" in node and node["minimum"] > node["maximum"]:
            raise ContentError(f"{label}: schema 'minimum' is above 'maximum'")
    elif any(key in node for key in ("minimum", "maximum")):
        raise ContentError(f"{label}: schema 'minimum' and 'maximum' need a number property")
    if kind == "string":
        for key in ("minLength", "maxLength"):
            if key in node and (
                not isinstance(node[key], int) or isinstance(node[key], bool) or node[key] < 0
            ):
                raise ContentError(f"{label}: schema {key!r} must be a whole number, 0 or more")
        if "minLength" in node and "maxLength" in node and node["minLength"] > node["maxLength"]:
            raise ContentError(f"{label}: schema 'minLength' is above 'maxLength'")
        if "pattern" in node:
            if not isinstance(node["pattern"], str):
                raise ContentError(f"{label}: schema 'pattern' must be text")
            try:
                re.compile(node["pattern"])
            except re.error as exc:
                raise ContentError(f"{label}: schema 'pattern' is not a regex: {exc}") from exc
    elif any(key in node for key in ("minLength", "maxLength", "pattern")):
        raise ContentError(f"{label}: schema length keys need a string property")
    if kind == "array":
        for key in ("minItems", "maxItems"):
            if key in node and (
                not isinstance(node[key], int) or isinstance(node[key], bool) or node[key] < 0
            ):
                raise ContentError(f"{label}: schema {key!r} must be a whole number, 0 or more")
        if "minItems" in node and "maxItems" in node and node["minItems"] > node["maxItems"]:
            raise ContentError(f"{label}: schema 'minItems' is above 'maxItems'")
    elif any(key in node for key in ("minItems", "maxItems")):
        raise ContentError(f"{label}: schema item bounds need an array property")


def _check_signature(
    accepted: Mapping[str, inspect.Parameter],
    parameters: Mapping[str, Any],
    label: str,
    consumed: frozenset[str] = frozenset(),
    bind: Mapping[str, Any] | None = None,
    rename: Mapping[str, str] | None = None,
) -> None:
    """The GM calls the function with keyword arguments taken from the schema, so both must agree.
    A renamed argument reaches the method under its own name, and a bound one comes from the tool,
    so neither has to match a parameter name in the schema."""
    bind = bind or {}
    rename = rename or {}
    takes_any_keyword = any(p.kind is p.VAR_KEYWORD for p in accepted.values())
    built = {
        source for value in bind.values() if isinstance(value, Mapping) for source in value.values()
    }
    if not takes_any_keyword:
        for name in parameters["properties"]:
            if name in consumed or name in rename or name in built:
                continue
            if name not in accepted:
                raise ContentError(f"{label}: function does not accept parameter {name!r}")
    required = set(parameters.get("required", []))
    renamed = {target: source for source, target in rename.items()}
    for name, param in accepted.items():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD) or name == CONTEXT_ARGUMENT:
            continue
        if name in bind:
            continue
        shown = renamed.get(name, name)
        if param.default is param.empty and shown not in required:
            raise ContentError(
                f"{label}: parameter {shown!r} has no default, so it must be required"
            )
