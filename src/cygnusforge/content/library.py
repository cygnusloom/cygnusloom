"""World templates and rule sets, loaded once from the bundled data directory."""

from __future__ import annotations

import importlib
import inspect
import re
import string
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from cygnusforge.content.character import (
    CampaignSpec,
    CharacterSpec,
    TableOption,
    build_campaign,
    build_character,
    character_schema,
)
from cygnusforge.content.definitions import (
    ContentError,
    Definition,
    read_definitions,
    required_text,
)
from cygnusforge.rules.dice import FORMULA, is_formula

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
# A dice table row's roll: one number, or an inclusive range such as "3-5".
ROLL_RANGE = re.compile(r"([0-9]+)(?:-([0-9]+))?")
PROMPT_ARGUMENT = "prompt"
# A tool flagged `character_options` gets this argument, built from the rule set's character spec.
CHARACTER_ARGUMENT = "character"
# The engine passes its ToolContext to a function that declares this parameter. The model
# never sees it, so it must not appear in a tool's schema.
CONTEXT_ARGUMENT = "ctx"
CORE_RULE_SET = "core"
# Every rule set must define these tables. NPCs get a random entry from each one by default.
REQUIRED_TABLES = ("personality", "accent", "quirk")


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


@dataclass(frozen=True)
class WorldTemplate:
    id: str
    name: str
    description: str
    locations: tuple[LocationTemplate, ...]
    npcs: tuple[NpcTemplate, ...]


@dataclass(frozen=True)
class PlayerInput:
    """The player supplies a value for a tool before it runs.

    The player rolls `dice` (a fixed formula such as "1d20") or, when `dice_param` is set, the
    formula the GM gave in that string argument, such as "1d8+1d8". The player may also type
    free-form text instead. A roll's total is passed to the function as keyword argument `param`.
    `details`, if set, is a template filled from the arguments and shown with the prompt, such as
    the modifiers. When `modifier_param` is set, the string argument it names is an attack modifier
    (such as "impaired"); the dice then come from that modifier, not from `dice_param`."""

    param: str
    dice: str | None = None
    dice_param: str | None = None
    details: str | None = None
    modifier_param: str | None = None


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    description: str
    parameters: Mapping[str, Any]
    function: Callable[..., Any]
    player_input: PlayerInput | None = None
    # A str.format template shown to the player after the tool runs. Fields come from the
    # arguments and the result.
    activity: str | None = None
    # True when the function declares CONTEXT_ARGUMENT and the engine must pass it.
    takes_context: bool = False
    # True for tools that only read the world. The GM lists them with list_lookups.
    lookup: bool = False


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
class DiceRow:
    """The rolls from `low` to `high`, inclusive, give `result`."""

    low: int
    high: int
    result: str


@dataclass(frozen=True)
class DiceTable:
    """A table rolled with dice, such as 2d6 for a reaction. Its rows cover every roll the dice
    can show, from the fewest to the most."""

    id: str
    name: str
    dice: str
    rows: tuple[DiceRow, ...]

    def row_for(self, roll: int) -> DiceRow:
        return next(row for row in self.rows if row.low <= roll <= row.high)


@dataclass(frozen=True)
class MonsterTemplate:
    """A statblock a GM can base a monster on. `stats` has every stat of the rule set's character,
    and `attacks` maps each attack's name to its dice formula. A `detachment` is a large group the
    Warden fights as one unit."""

    id: str
    name: str
    description: str
    stats: Mapping[str, int]
    armor: int
    attacks: Mapping[str, str]
    detachment: bool = False


DAMAGE_KINDS = ("absorb", "harm", "overflow")
# When a condition can end: by a rest, extended rest, aid from someone else, the passing of time,
# or the GM's say-so. A condition's `ends` lists the ones that end it; `gm` always does.
CONDITION_ENDS = ("rest", "extended_rest", "aid", "time", "gm")
# The effect ops a scar or condition may run, in the order they are listed.
EFFECT_OPS = ("note", "set_max", "condition", "save", "pick_stat")
# `if_higher` compares the roll with the maximum; `if_above_current` compares it with the current score.
MAX_MODES = ("if_higher", "if_above_current", "add", "always")
# The name a `pick_stat` op stores its choice under, for later ops to use as their `stat`.
PICKED = "$picked"
DICE_SIDES = re.compile(r"1d[0-9]+")
# The Scars table has one entry for each hit that takes exactly 0 HP, up to this many points.
SCAR_ROLLS = 12


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
    """A condition a scar or an effect can give a character. `ends` lists the triggers that end it,
    and `on_end` are the effect ops run when it does. A derived condition has `derived` instead."""

    id: str
    label: str
    description: str
    ends: tuple[str, ...]
    on_end: tuple[Mapping[str, Any], ...]
    derived: DerivedRule | None = None


@dataclass(frozen=True)
class ScarEntry:
    roll: int
    name: str
    text: str
    effects: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class ScarTable:
    """The Scars table, with the `stat` whose hit that reaches 0 rolls on it."""

    id: str
    stat: str
    entries: tuple[ScarEntry, ...]


@dataclass(frozen=True)
class MoraleRules:
    """When a side of NPCs must make a morale save and what it is made with. `wil_stat` is the
    leader's stat the save uses. `first_casualty` and `half` say whether a side checks at its first
    casualty and at half its number. `lone_at` is the stat a lone foe checks at when it reaches 0.
    `retreat_stat` is the stat a character saves against to run from a fight."""

    wil_stat: str
    first_casualty: bool
    half: bool
    lone_at: str
    retreat_stat: str


@dataclass(frozen=True)
class CombatRules:
    """How a rule set resolves combat. `module` supplies `save_succeeds(roll, score)` and
    `initiative_order(entries)`. `damage` is the order a hit's damage goes through. `check_die` is
    the die a save or initiative roll is made with, and `check_sides` its number of sides.
    `initiative_stat` is what a player character saves against to act. `defeat` says where a defeat
    leaves a player character (`player_character`) and an NPC (`npc`). `morale` is None when the
    rule set has no morale checks. `modifiers` maps each attack modifier's name to the dice it rolls
    instead of the attack's own."""

    id: str
    module: ModuleType
    damage: tuple[DamageStep, ...]
    check_die: str
    check_sides: int
    initiative_stat: str
    defeat: Mapping[str, str]
    scar_table: ScarTable | None = None
    # The stats outside combat harm can take, such as strength, dexterity, and willpower.
    outside_harm: tuple[str, ...] = ()
    morale: MoraleRules | None = None
    modifiers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ClockRules:
    """The parts of a day, in order. A place not visited for `stale_days` days is flagged when a
    new day begins, so the Warden can update it."""

    parts: tuple[str, ...]
    stale_days: int


@dataclass(frozen=True)
class RestLength:
    """A rest the Warden can call for. It passes `parts` parts of a day, or `days` days, and
    `until_morning` runs on to the next morning. A full rest restores what `restores` lists and
    clears the item kinds in `clears`. A deprived character recovers nothing."""

    id: str
    parts: int
    days: int
    until_morning: bool
    full: bool
    restores: tuple[str, ...]
    clears: tuple[str, ...]
    # Whether the party eats during this rest: each character who is not lodged uses a food use.
    food: bool = False


@dataclass(frozen=True)
class DeprivationRules:
    """A character deprived of a crucial need gains one `fatigue_item` a day once it has gone on
    for more than `after_days` days."""

    fatigue_item: str
    after_days: int


@dataclass(frozen=True)
class InventoryRules:
    """How many slots a character's inventory has, what Fatigue costs, and which stat takes harm
    when the inventory fills. `item_sizes` is in `RuleSet`."""

    capacity: int
    fatigue_item: str
    fatigue_slots: int
    full_stat: str


@dataclass(frozen=True)
class FoodRules:
    """The food a party eats on rests. `feature` is a consumable item feature; a character starts
    with it as an inventory item named `item`, and each night's rest spends one of its uses."""

    feature: str
    item: str


@dataclass(frozen=True)
class ShopItem:
    """A catalogue item. `price` is in gold. `armor` is added to the buyer's armor. `damage` is the
    attack die the item gives, as text for the Warden."""

    id: str
    name: str
    category: str
    price: int
    size: str
    armor: int
    damage: str
    uses: int
    description: str


@dataclass(frozen=True)
class ShopService:
    """A service the marketplace sells. `effect` is "lodging", "restore", or "deferred". Lodging
    covers `fits` characters a night. A restore gives back the attributes in `restores`."""

    id: str
    name: str
    price: int
    effect: str
    fits: int = 1
    restores: tuple[str, ...] = ()


@dataclass(frozen=True)
class Hireling:
    id: str
    name: str
    wage_per_day: int


@dataclass(frozen=True)
class Marketplace:
    # The feature id of the currency, such as gold. Buying takes it from the buyer's features.
    currency: str
    items: tuple[ShopItem, ...]
    services: tuple[ShopService, ...]
    hirelings: tuple[Hireling, ...]


@dataclass(frozen=True)
class MagicRules:
    """The item kinds magic uses, the size each takes, and the Fatigue a spellbook cast costs.
    A spellbook holds one spell, a scroll is one use of a spell, and a relic has uses that recharge."""

    spellbook_kind: str
    spellbook_size: str
    scroll_kind: str
    scroll_size: str
    relic_kind: str
    cast_fatigue: int


@dataclass(frozen=True)
class RuleSet:
    id: str
    name: str
    description: str
    rules: Mapping[str, str]
    tool_calls: Mapping[str, ToolCall]
    character: CharacterSpec
    # Options shared by every character in a campaign, such as the omen.
    campaign: CampaignSpec
    # Named option lists, such as "personality", used to generate NPC traits.
    tables: Mapping[str, tuple[TableOption, ...]] = field(default_factory=dict)
    # Rules whose text goes into every prompt. The others are read with lookup_rule.
    important: frozenset[str] = frozenset()
    # The scale of encounter maps, or None when the rule set has no grid: its encounters have no
    # movement limit.
    grid: EncounterGrid | None = None
    # How combat resolves, or None when the rule set has no combat.
    combat: CombatRules | None = None
    # Statblocks for monsters, by id.
    monsters: Mapping[str, MonsterTemplate] = field(default_factory=dict)
    # Conditions a scar or effect can give, by id. Derived conditions are included.
    conditions: Mapping[str, ConditionRules] = field(default_factory=dict)
    # Tables rolled with dice, by id, such as the Warden's reaction table.
    dice_tables: Mapping[str, DiceTable] = field(default_factory=dict)
    # Time, rest, and needs. Each is None or empty when the rule set does not track them.
    clock: ClockRules | None = None
    rests: Mapping[str, RestLength] = field(default_factory=dict)
    # The rest length an interrupted rest counts as, such as "moments".
    interrupted_rest: str = ""
    deprivation: DeprivationRules | None = None
    inventory: InventoryRules | None = None
    # Item sizes by id, as the slots an item of that size takes.
    item_sizes: Mapping[str, int] = field(default_factory=dict)
    # How magic uses items, or None when the rule set has no magic.
    magic: MagicRules | None = None
    # The food a party eats on rests, or None when the rule set does not track food.
    food: FoodRules | None = None
    # What the marketplace sells, or None when the rule set has no marketplace.
    marketplace: Marketplace | None = None


@dataclass(frozen=True)
class ContentLibrary:
    world_templates: Mapping[str, WorldTemplate]
    # Selectable rule sets. The core rule set is not listed here: it is merged into each of these.
    rule_sets: Mapping[str, RuleSet]
    core: RuleSet

    @classmethod
    def load(cls, data_dir: Path = DATA_DIR) -> ContentLibrary:
        """Load every world template and rule set. Each subdirectory is one of either.

        The `core` rule set is loaded on its own first, then merged under every other rule
        set. A definition in a rule set replaces the core definition with the same type and id."""
        worlds = {d.name: _load_world(d) for d in _subdirectories(data_dir / "worlds")}
        rule_set_dirs = _subdirectories(data_dir / "rulesets")
        core_dir = next((d for d in rule_set_dirs if d.name == CORE_RULE_SET), None)
        if core_dir is None:
            raise ContentError(f"missing the {CORE_RULE_SET!r} rule set")
        core_definitions = read_definitions(core_dir)
        core = _build_rule_set(core_definitions, CORE_RULE_SET)
        rule_sets = {
            d.name: _load_rule_set(d, core_definitions)
            for d in rule_set_dirs
            if d.name != CORE_RULE_SET
        }
        return cls(world_templates=worlds, rule_sets=rule_sets, core=core)


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


def _load_world(directory: Path) -> WorldTemplate:
    template = directory.name
    definitions = read_definitions(directory)
    world = _single(definitions, "world", template)
    _check_directory_id(template, world.id)
    label = f"{template}: world"
    locations = tuple(_load_location(d, template) for d in definitions if d.type == "location")
    npcs = tuple(_load_npc(d, template) for d in definitions if d.type == "npc")
    location_ids = {location.id for location in locations}
    for npc in npcs:
        if npc.location not in location_ids:
            raise ContentError(
                f"{template}: npc {npc.id!r} is in unknown location {npc.location!r}"
            )
    _check_parents(locations, template)
    _check_unique_names([*locations, *npcs], template)
    return WorldTemplate(
        id=world.id,
        name=required_text(world.data, "name", label),
        description=required_text(world.data, "description", label),
        locations=locations,
        npcs=npcs,
    )


def _load_location(definition: Definition, template: str) -> LocationTemplate:
    label = f"{template}: location {definition.id}"
    data = definition.data
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
    return LocationTemplate(
        id=definition.id,
        name=required_text(data, "name", label),
        description=required_text(data, "description", label),
        discovered=discovered,
        x=None if x is None else float(x),
        y=None if y is None else float(y),
        parent=_optional_text(data, "parent", label),
    )


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
    quirks = data.get("quirks")
    if quirks is not None:
        if not isinstance(quirks, list) or not all(
            isinstance(q, str) and q.strip() for q in quirks
        ):
            raise ContentError(f"{label}: 'quirks' must be a list of non-empty text")
        quirks = tuple(quirks)
    return NpcTemplate(
        id=definition.id,
        name=required_text(data, "name", label),
        location=required_text(data, "location", label),
        description=required_text(data, "description", label),
        personality=_optional_text(data, "personality", label),
        accent=_optional_text(data, "accent", label),
        quirks=quirks,
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


def _load_rule_set(directory: Path, core_definitions: list[Definition]) -> RuleSet:
    """A rule set is its own definitions plus every core definition it does not replace."""
    template = directory.name
    own = read_definitions(directory)
    own_keys = {(d.type, d.id) for d in own}
    inherited = [
        d for d in core_definitions if d.type != "rule_set" and (d.type, d.id) not in own_keys
    ]
    return _build_rule_set([*own, *inherited], template)


def _build_rule_set(definitions: list[Definition], template: str) -> RuleSet:
    head = _single(definitions, "rule_set", template)
    _check_directory_id(template, head.id)
    label = f"{template}: rule_set"
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
    dice_tables = {
        d.id: _load_dice_table(d, template) for d in definitions if d.type == "dice_table"
    }
    for name in REQUIRED_TABLES:
        if name not in tables:
            raise ContentError(f"{template}: missing table {name!r}")
    table_names = {d.id: _table_label(d, template) for d in definitions if d.type == "table"}
    character = build_character(definitions, tables, table_names, template)
    campaign = build_campaign(definitions, tables, table_names, template)
    grids = [d for d in definitions if d.type == "encounter_grid"]
    if len(grids) > 1:
        raise ContentError(f"{template}: more than one encounter grid")
    grid = _load_grid(grids[0], template) if grids else None
    combats = [d for d in definitions if d.type == "combat"]
    if len(combats) > 1:
        raise ContentError(f"{template}: more than one combat definition")
    conditions = _load_conditions(definitions, character, template)
    scar_tables = {
        d.id: _load_scar_entries(d, character, conditions, template)
        for d in definitions
        if d.type == "scar_table"
    }
    combat = (
        _load_combat(combats[0], modules, character, scar_tables, template) if combats else None
    )
    monsters = {
        d.id: _load_monster(d, character, template) for d in definitions if d.type == "monster"
    }
    clock = _load_clock(definitions, template)
    rests = _load_rests(definitions, character, template)
    interrupted_rest = _load_interrupted_rest(definitions, rests, template)
    deprivation = _load_deprivation(definitions, template)
    inventory = _load_inventory(definitions, character, template)
    item_sizes = _load_item_sizes(definitions, template)
    _check_item_sizes(character, item_sizes, template)
    magic = _load_magic(definitions, item_sizes, template)
    food = _load_food(definitions, rests, item_sizes, character, template)
    marketplace = _load_marketplace(definitions, item_sizes, character, template)
    tool_calls: dict[str, ToolCall] = {}
    tool_names: set[str] = set()
    for d in definitions:
        if d.type != "tool_call":
            continue
        call = _load_tool_call(d, modules, character, template)
        if call.name in tool_names:
            raise ContentError(f"{template}: duplicate tool name {call.name!r}")
        tool_names.add(call.name)
        tool_calls[call.id] = call
    return RuleSet(
        id=head.id,
        name=required_text(head.data, "name", label),
        description=required_text(head.data, "description", label),
        rules=rules,
        important=important,
        tool_calls=tool_calls,
        character=character,
        campaign=campaign,
        tables=tables,
        grid=grid,
        combat=combat,
        monsters=monsters,
        conditions=conditions,
        dice_tables=dice_tables,
        clock=clock,
        rests=rests,
        interrupted_rest=interrupted_rest,
        deprivation=deprivation,
        inventory=inventory,
        item_sizes=item_sizes,
        magic=magic,
        food=food,
        marketplace=marketplace,
    )


def _load_combat(
    definition: Definition,
    modules: Mapping[str, ModuleType],
    character: CharacterSpec,
    scar_tables: Mapping[str, tuple[ScarEntry, ...]],
    template: str,
) -> CombatRules:
    label = f"{template}: combat {definition.id}"
    data = definition.data
    module_id = required_text(data, "python_module", label)
    if module_id not in modules:
        raise ContentError(f"{label}: unknown python_module {module_id!r}")
    module = modules[module_id]
    for function_name in ("save_succeeds", "initiative_order"):
        if not callable(getattr(module, function_name, None)):
            raise ContentError(f"{label}: {function_name!r} is not a function in {module_id!r}")
    damage = _load_damage(data, character, label)
    check_die = required_text(data, "check_die", label)
    if not re.fullmatch(r"1d[0-9]+", check_die) or int(check_die[2:]) < 2:
        raise ContentError(f"{label}: 'check_die' must be a single die such as '1d20'")
    initiative_stat = required_text(data, "initiative_stat", label)
    if initiative_stat not in character.stats:
        raise ContentError(f"{label}: 'initiative_stat' names no stat ({initiative_stat!r})")
    defeat_data = data.get("defeat")
    if not isinstance(defeat_data, Mapping):
        raise ContentError(f"{label}: 'defeat' must be an object")
    defeat = {
        key: required_text(defeat_data, key, f"{label}: defeat")
        for key in ("player_character", "npc")
    }
    scar_table = _load_scar_table_ref(data, character, scar_tables, label)
    outside_harm = _load_outside_harm(data, character, label)
    return CombatRules(
        id=definition.id,
        module=module,
        damage=damage,
        check_die=check_die,
        check_sides=int(check_die[2:]),
        initiative_stat=initiative_stat,
        defeat=defeat,
        scar_table=scar_table,
        outside_harm=outside_harm,
        morale=_load_morale(data, character, label),
        modifiers=_load_modifiers(data, label),
    )


def _load_morale(
    data: Mapping[str, Any], character: CharacterSpec, label: str
) -> MoraleRules | None:
    morale_data = data.get("morale")
    if morale_data is None:
        return None
    if not isinstance(morale_data, Mapping):
        raise ContentError(f"{label}: 'morale' must be an object")
    morale_label = f"{label}: morale"
    stats = {
        key: required_text(morale_data, key, morale_label)
        for key in ("wil_stat", "lone_at", "retreat_stat")
    }
    for key, stat in stats.items():
        if stat not in character.stats:
            raise ContentError(f"{morale_label}: {key!r} names no stat ({stat!r})")
    flags = {}
    for key in ("first_casualty", "half"):
        value = morale_data.get(key)
        if not isinstance(value, bool):
            raise ContentError(f"{morale_label}: {key!r} must be true or false")
        flags[key] = value
    return MoraleRules(
        wil_stat=stats["wil_stat"],
        first_casualty=flags["first_casualty"],
        half=flags["half"],
        lone_at=stats["lone_at"],
        retreat_stat=stats["retreat_stat"],
    )


def _load_scar_table_ref(
    data: Mapping[str, Any],
    character: CharacterSpec,
    scar_tables: Mapping[str, tuple[ScarEntry, ...]],
    label: str,
) -> ScarTable | None:
    """The `scars` key names the Scars table to roll on and the stat whose hit to 0 rolls on it."""
    if "scars" not in data:
        return None
    ref = data["scars"]
    if not isinstance(ref, Mapping):
        raise ContentError(f"{label}: 'scars' must be an object")
    table_id = required_text(ref, "table", f"{label}: scars")
    if table_id not in scar_tables:
        raise ContentError(f"{label}: 'scars' names no scar table ({table_id!r})")
    stat = required_text(ref, "stat", f"{label}: scars")
    if stat not in character.stats:
        raise ContentError(f"{label}: 'scars' names no stat ({stat!r})")
    return ScarTable(id=table_id, stat=stat, entries=scar_tables[table_id])


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


def _load_monster(
    definition: Definition, character: CharacterSpec, template: str
) -> MonsterTemplate:
    label = f"{template}: monster {definition.id}"
    data = definition.data
    stats = data.get("stats")
    if not isinstance(stats, dict) or set(stats) != set(character.stats):
        names = ", ".join(sorted(character.stats))
        raise ContentError(f"{label}: 'stats' must give a whole number for each of: {names}")
    for key, value in stats.items():
        _check_whole(value, f"{label}: stat {key!r}")
    armor = data.get("armor", 0)
    _check_whole(armor, f"{label}: 'armor'")
    attacks = data.get("attacks", {})
    if not isinstance(attacks, dict):
        raise ContentError(f"{label}: 'attacks' must be an object of attack names to dice")
    for attack_name, formula in attacks.items():
        if not isinstance(attack_name, str) or not attack_name.strip():
            raise ContentError(f"{label}: attack names must be non-empty text")
        if not is_formula(formula):
            raise ContentError(
                f"{label}: attack {attack_name!r} must be a dice formula such as '1d8'"
            )
    detachment = data.get("detachment", False)
    if not isinstance(detachment, bool):
        raise ContentError(f"{label}: 'detachment' must be true or false")
    return MonsterTemplate(
        id=definition.id,
        name=required_text(data, "name", label),
        description=required_text(data, "description", label),
        stats=dict(stats),
        armor=armor,
        attacks=dict(attacks),
        detachment=detachment,
    )


def _check_whole(value: object, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ContentError(f"{label} must be a whole number, at least 0")


def _load_grid(definition: Definition, template: str) -> EncounterGrid:
    label = f"{template}: encounter grid {definition.id}"
    cell_feet = definition.data.get("cell_feet")
    move_feet = definition.data.get("move_feet")
    for key, value in (("cell_feet", cell_feet), ("move_feet", move_feet)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ContentError(f"{label}: {key!r} must be a whole number of feet, at least 1")
    if move_feet < cell_feet:
        raise ContentError(f"{label}: 'move_feet' must be at least one cell")
    return EncounterGrid(cell_feet=cell_feet, move_feet=move_feet)


def _single_optional(definitions: list[Definition], kind: str, template: str) -> Definition | None:
    found = [d for d in definitions if d.type == kind]
    if len(found) > 1:
        raise ContentError(f"{template}: more than one {kind} definition")
    return found[0] if found else None


def _whole(data: Mapping[str, Any], key: str, label: str, minimum: int) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ContentError(f"{label}: {key!r} must be a whole number, at least {minimum}")
    return value


def _names(data: Mapping[str, Any], key: str, label: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ContentError(f"{label}: {key!r} must be a list of non-empty strings")
    return tuple(value)


def _load_clock(definitions: list[Definition], template: str) -> ClockRules | None:
    definition = _single_optional(definitions, "clock", template)
    if definition is None:
        return None
    label = f"{template}: clock {definition.id}"
    parts = _names(definition.data, "parts", label)
    if not parts or len(set(parts)) != len(parts):
        raise ContentError(f"{label}: 'parts' must be a non-empty list of distinct names")
    stale_days = _whole(definition.data, "stale_days", label, 1)
    return ClockRules(parts=parts, stale_days=stale_days)


def _load_rests(
    definitions: list[Definition], character: CharacterSpec, template: str
) -> dict[str, RestLength]:
    """The rest lengths of the rule set's single rest definition, by id. Each needs `full` and at
    least one of `parts`, `days`, or `until: "morning"`."""
    definition = _single_optional(definitions, "rest", template)
    if definition is None:
        return {}
    label = f"{template}: rest {definition.id}"
    lengths = definition.data.get("lengths")
    if not isinstance(lengths, Mapping) or not lengths:
        raise ContentError(f"{label}: 'lengths' must be a non-empty object")
    loaded: dict[str, RestLength] = {}
    for length_id, data in lengths.items():
        length_label = f"{label}: length {length_id}"
        if not isinstance(data, Mapping):
            raise ContentError(f"{length_label} must be an object")
        parts = _whole(data, "parts", length_label, 0) if "parts" in data else 0
        days = _whole(data, "days", length_label, 0) if "days" in data else 0
        until = data.get("until")
        if until is not None and until != "morning":
            raise ContentError(f"{length_label}: 'until' must be \"morning\"")
        if not (parts or days or until):
            raise ContentError(f"{length_label}: needs 'parts', 'days', or 'until'")
        full = data.get("full")
        if not isinstance(full, bool):
            raise ContentError(f"{length_label}: 'full' must be true or false")
        restores = _names(data, "restores", length_label)
        for stat in restores:
            if stat not in character.stats:
                raise ContentError(f"{length_label}: 'restores' names no stat ({stat!r})")
        food = data.get("food", False)
        if not isinstance(food, bool):
            raise ContentError(f"{length_label}: 'food' must be true or false")
        loaded[length_id] = RestLength(
            id=length_id,
            parts=parts,
            days=days,
            until_morning=until == "morning",
            full=full,
            restores=restores,
            clears=_names(data, "clears", length_label),
            food=food,
        )
    return loaded


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


def _load_deprivation(definitions: list[Definition], template: str) -> DeprivationRules | None:
    definition = _single_optional(definitions, "deprivation", template)
    if definition is None:
        return None
    label = f"{template}: deprivation {definition.id}"
    return DeprivationRules(
        fatigue_item=required_text(definition.data, "fatigue_item", label),
        after_days=_whole(definition.data, "after_days", label, 0),
    )


def _load_inventory(
    definitions: list[Definition], character: CharacterSpec, template: str
) -> InventoryRules | None:
    definition = _single_optional(definitions, "inventory", template)
    if definition is None:
        return None
    label = f"{template}: inventory {definition.id}"
    full_stat = required_text(definition.data, "full_stat", label)
    if full_stat not in character.stats:
        raise ContentError(f"{label}: 'full_stat' names no stat ({full_stat!r})")
    return InventoryRules(
        capacity=_whole(definition.data, "capacity", label, 1),
        fatigue_item=required_text(definition.data, "fatigue_item", label),
        fatigue_slots=_whole(definition.data, "fatigue_slots", label, 0),
        full_stat=full_stat,
    )


def _load_item_sizes(definitions: list[Definition], template: str) -> dict[str, int]:
    return {
        d.id: _whole(d.data, "slots", f"{template}: item size {d.id}", 0)
        for d in definitions
        if d.type == "item_size"
    }


def _load_magic(
    definitions: list[Definition], item_sizes: Mapping[str, int], template: str
) -> MagicRules | None:
    definition = _single_optional(definitions, "magic", template)
    if definition is None:
        return None
    label = f"{template}: magic {definition.id}"
    sizes = {
        key: required_text(definition.data, key, label) for key in ("spellbook_size", "scroll_size")
    }
    for key, size in sizes.items():
        if size not in item_sizes:
            raise ContentError(f"{label}: {key!r} names no item size ({size!r})")
    return MagicRules(
        spellbook_kind=required_text(definition.data, "spellbook_kind", label),
        spellbook_size=sizes["spellbook_size"],
        scroll_kind=required_text(definition.data, "scroll_kind", label),
        scroll_size=sizes["scroll_size"],
        relic_kind=required_text(definition.data, "relic_kind", label),
        cast_fatigue=_whole(definition.data, "cast_fatigue", label, 0),
    )


def _load_food(
    definitions: list[Definition],
    rests: Mapping[str, RestLength],
    item_sizes: Mapping[str, int],
    character: CharacterSpec,
    template: str,
) -> FoodRules | None:
    """The food block of the rule set's rest definition. A rest length with `food` needs it."""
    definition = _single_optional(definitions, "rest", template)
    if definition is None or "food" not in definition.data:
        if any(length.food for length in rests.values()):
            raise ContentError(f"{template}: a rest eats food, so the rest needs a 'food' block")
        return None
    label = f"{template}: rest {definition.id} food"
    data = definition.data["food"]
    if not isinstance(data, Mapping):
        raise ContentError(f"{label} must be an object")
    feature = required_text(data, "feature", label)
    if feature not in character.features:
        raise ContentError(f"{label}: 'feature' names no character feature ({feature!r})")
    food = character.features[feature]
    if food.feature_type != "item" or food.uses < 1:
        raise ContentError(f"{label}: 'feature' must be a consumable item with uses ({feature!r})")
    return FoodRules(feature=feature, item=food.name)


def _check_item_sizes(character: CharacterSpec, item_sizes: Mapping[str, int], template: str):
    """Every item feature must name an item size, so the engine can carry it."""
    for feature in character.features.values():
        if feature.feature_type == "item" and feature.size not in item_sizes:
            raise ContentError(
                f"{template}: feature {feature.id!r} names no item size ({feature.size!r})"
            )


def _load_marketplace(
    definitions: list[Definition],
    item_sizes: Mapping[str, int],
    character: CharacterSpec,
    template: str,
) -> Marketplace | None:
    """The marketplace: `shop_item`, `service`, and `hireling` definitions. Items may be petty,
    normal, or bulky, as the rule set's item sizes say."""
    shop = [d for d in definitions if d.type == "shop_item"]
    services = [d for d in definitions if d.type == "service"]
    hirelings = [d for d in definitions if d.type == "hireling"]
    head = _single_optional(definitions, "marketplace", template)
    if head is None:
        if shop or services or hirelings:
            raise ContentError(f"{template}: the marketplace needs a 'marketplace' definition")
        return None
    currency = required_text(head.data, "currency", f"{template}: marketplace")
    if currency not in character.features:
        raise ContentError(f"{template}: marketplace 'currency' names no feature ({currency!r})")
    items = []
    for d in shop:
        label = f"{template}: shop item {d.id}"
        size = required_text(d.data, "size", label)
        if size not in item_sizes:
            raise ContentError(f"{label}: 'size' names no item size ({size!r})")
        items.append(
            ShopItem(
                id=d.id,
                name=required_text(d.data, "name", label),
                category=required_text(d.data, "category", label),
                price=_whole(d.data, "price", label, 0),
                size=size,
                armor=_whole(d.data, "armor", label, 0),
                damage=str(d.data.get("damage", "")),
                uses=_whole(d.data, "uses", label, 0),
                description=str(d.data.get("description", "")),
            )
        )
    loaded_services = []
    for d in services:
        label = f"{template}: service {d.id}"
        effect = required_text(d.data, "effect", label)
        if effect not in ("lodging", "restore", "deferred"):
            raise ContentError(f"{label}: 'effect' must be lodging, restore, or deferred")
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
            )
        )
    hired = [
        Hireling(
            id=d.id,
            name=required_text(d.data, "name", f"{template}: hireling {d.id}"),
            wage_per_day=_whole(d.data, "wage_per_day", f"{template}: hireling {d.id}", 0),
        )
        for d in hirelings
    ]
    return Marketplace(
        currency=currency,
        items=tuple(items),
        services=tuple(loaded_services),
        hirelings=tuple(hired),
    )


def _load_conditions(
    definitions: list[Definition], character: CharacterSpec, template: str
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
        ends = d.data.get("ends", [])
        if not isinstance(ends, list) or any(end not in CONDITION_ENDS for end in ends):
            raise ContentError(f"{label}: 'ends' must list only {CONDITION_ENDS}")
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
        )
    return conditions


def _load_derived(value: object, character: CharacterSpec, label: str) -> DerivedRule | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ContentError(f"{label}: 'derived' must be an object")
    stat = required_text(value, "stat", f"{label}: derived")
    if stat not in character.stats:
        raise ContentError(f"{label}: derived names no stat ({stat!r})")
    current = value.get("current")
    if not isinstance(current, int) or isinstance(current, bool) or current < 0:
        raise ContentError(f"{label}: derived 'current' must be a whole number of 0 or more")
    needs_max = _flag(value, "needs_max", f"{label}: derived")
    return DerivedRule(stat=stat, current=current, needs_max=needs_max)


def _load_scar_entries(
    definition: Definition,
    character: CharacterSpec,
    conditions: Mapping[str, ConditionRules],
    template: str,
) -> tuple[ScarEntry, ...]:
    """A Scars table: one entry for each roll from 1 to SCAR_ROLLS, in order."""
    label = f"{template}: scar table {definition.id}"
    entries = definition.data.get("entries")
    if not isinstance(entries, list) or len(entries) != SCAR_ROLLS:
        raise ContentError(f"{label}: 'entries' must list exactly {SCAR_ROLLS} entries")
    stored = frozenset(cid for cid, rules in conditions.items() if rules.derived is None)
    loaded = []
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, Mapping):
            raise ContentError(f"{label}: each entry must be an object")
        entry_label = f"{label}: entry {position}"
        if entry.get("roll") != position:
            raise ContentError(f"{entry_label}: 'roll' must be {position}")
        effects, _ = _check_effects(entry.get("effects", []), entry_label, character, stored, False)
        loaded.append(
            ScarEntry(
                roll=position,
                name=required_text(entry, "name", entry_label),
                text=required_text(entry, "text", entry_label),
                effects=effects,
            )
        )
    return tuple(loaded)


def _check_effects(
    ops: object,
    label: str,
    character: CharacterSpec,
    conditions: frozenset[str],
    picked: bool,
) -> tuple[tuple[Mapping[str, Any], ...], bool]:
    """Validate a list of effect ops, in order. `picked` says whether a `pick_stat` came before,
    so `$picked` may be used. Returns the ops and whether a stat has been picked by the end."""
    if not isinstance(ops, list):
        raise ContentError(f"{label}: effects must be a list")
    checked: list[Mapping[str, Any]] = []
    for op in ops:
        if not isinstance(op, Mapping):
            raise ContentError(f"{label}: each effect must be an object")
        name = required_text(op, "op", label)
        if name not in EFFECT_OPS:
            raise ContentError(f"{label}: effect op {name!r} is not one of {EFFECT_OPS}")
        if name == "note":
            required_text(op, "text", label)
        elif name == "set_max":
            stat = required_text(op, "stat", label)
            if stat == PICKED:
                if not picked:
                    raise ContentError(f"{label}: {PICKED!r} is used before a pick_stat")
            elif stat not in character.stats:
                raise ContentError(f"{label}: set_max names no stat ({stat!r})")
            dice = required_text(op, "dice", label)
            if not is_formula(dice):
                raise ContentError(f"{label}: set_max 'dice' is not a formula ({dice!r})")
            mode = required_text(op, "mode", label)
            if mode not in MAX_MODES:
                raise ContentError(f"{label}: set_max 'mode' must be one of {MAX_MODES}")
        elif name == "condition":
            condition = required_text(op, "id", label)
            if condition not in conditions:
                raise ContentError(
                    f"{label}: condition op names no stored condition ({condition!r})"
                )
        elif name == "save":
            stat = required_text(op, "stat", label)
            if stat not in character.stats:
                raise ContentError(f"{label}: save names no stat ({stat!r})")
            _, picked = _check_effects(op.get("pass", []), label, character, conditions, picked)
            _, picked = _check_effects(op.get("fail", []), label, character, conditions, picked)
        elif name == "pick_stat":
            options = op.get("options")
            if not isinstance(options, list) or not options:
                raise ContentError(f"{label}: pick_stat 'options' must be a non-empty list")
            for option in options:
                if option not in character.stats:
                    raise ContentError(f"{label}: pick_stat names no stat ({option!r})")
            dice = required_text(op, "dice", label)
            if not DICE_SIDES.fullmatch(dice):
                raise ContentError(f"{label}: pick_stat 'dice' must be a single die such as '1d6'")
            picked = True
        checked.append(op)
    return tuple(checked), picked


def _load_table(definition: Definition, template: str) -> tuple[TableOption, ...]:
    label = f"{template}: table {definition.id}"
    options = definition.data.get("options")
    if not isinstance(options, list) or not options:
        raise ContentError(f"{label}: 'options' must be a non-empty list")
    loaded = tuple(_load_option(option, label) for option in options)
    if len({option.value for option in loaded}) != len(loaded):
        raise ContentError(f"{label}: every option must be unique")
    return loaded


def _load_option(option: object, label: str) -> TableOption:
    """An option is text, or an object with a `name` and a `description`."""
    if isinstance(option, str) and option.strip():
        return TableOption(name=None, description=option)
    if (
        isinstance(option, dict)
        and set(option) == {"name", "description"}
        and all(isinstance(option[key], str) and option[key].strip() for key in option)
    ):
        return TableOption(name=option["name"], description=option["description"])
    raise ContentError(
        f"{label}: every option must be non-empty text or an object with "
        "non-empty 'name' and 'description' text"
    )


def _load_dice_table(definition: Definition, template: str) -> DiceTable:
    """A dice table has a single dice formula and rows of `roll` ("4" or "2-3") and `result`.
    The rows must run in order with no gaps, covering every roll the dice can show."""
    label = f"{template}: dice table {definition.id}"
    name = _table_label(definition, template)
    dice = definition.data.get("dice")
    formula = FORMULA.fullmatch(dice) if isinstance(dice, str) else None
    if formula is None:
        raise ContentError(f"{label}: 'dice' must be one dice formula such as '2d6'")
    low, high = int(formula.group(1)), int(formula.group(1)) * int(formula.group(2))
    rows = definition.data.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ContentError(f"{label}: 'rows' must be a non-empty list")
    loaded: list[DiceRow] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {"roll", "result"}:
            raise ContentError(f"{label}: row {index} needs 'roll' and 'result' and no other keys")
        roll, result = row["roll"], row["result"]
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
        loaded.append(DiceRow(start, end, result))
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
    return DiceTable(id=definition.id, name=name, dice=dice, rows=tuple(loaded))


def _table_label(definition: Definition, template: str) -> str:
    """The name a player or the GM sees for a table. It defaults to the table id."""
    name = definition.data.get("name", definition.id)
    if not isinstance(name, str) or not name.strip():
        raise ContentError(f"{template}: table {definition.id}: 'name' must be non-empty text")
    return name


def _import_module(definition: Definition, template: str) -> ModuleType:
    label = f"{template}: python_module {definition.id}"
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
) -> ToolCall:
    label = f"{template}: tool_call {definition.id}"
    data = definition.data
    module_id = required_text(data, "python_module", label)
    if module_id not in modules:
        raise ContentError(f"{label}: unknown python_module {module_id!r}")
    function_name = required_text(data, "function", label)
    function = getattr(modules[module_id], function_name, None)
    if not callable(function):
        raise ContentError(f"{label}: {function_name!r} is not a function in {module_id!r}")
    name = required_text(data, "name", label)
    if not TOOL_NAME.fullmatch(name):
        raise ContentError(f"{label}: 'name' must be 1 to 64 letters, digits, '_' or '-'")
    parameters = _check_parameters(data.get("parameters"), label)
    if _flag(data, "character_options", label):
        parameters = _with_character(parameters, character, label)
    if CONTEXT_ARGUMENT in parameters["properties"]:
        raise ContentError(f"{label}: {CONTEXT_ARGUMENT!r} is reserved and not a parameter")
    player_input = _check_player_input(data.get("player_input"), parameters, function, label)
    activity = _check_activity(data.get("activity"), label)
    _check_signature(function, parameters, label, consumed=_consumed(player_input))
    return ToolCall(
        id=definition.id,
        name=name,
        description=required_text(data, "description", label),
        parameters=parameters,
        function=function,
        player_input=player_input,
        activity=activity,
        takes_context=CONTEXT_ARGUMENT in inspect.signature(function).parameters,
        lookup=_flag(data, "lookup", label),
    )


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


def _consumed(player_input: PlayerInput | None) -> frozenset[str]:
    """Schema arguments the engine uses itself rather than passing to the function."""
    if player_input is None:
        return frozenset()
    consumed = {PROMPT_ARGUMENT}
    if player_input.dice_param:
        consumed.add(player_input.dice_param)
    return frozenset(consumed)


def _check_player_input(
    data: object, parameters: Mapping[str, Any], function: Callable[..., Any], label: str
) -> PlayerInput | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ContentError(f"{label}: 'player_input' must be an object")
    dice = data.get("dice")
    dice_param = data.get("dice_param")
    modifier_param = data.get("modifier_param")
    if (dice is None) == (dice_param is None):
        raise ContentError(f"{label}: 'player_input' needs exactly one of 'dice' or 'dice_param'")
    if modifier_param is not None:
        if dice_param is None:
            raise ContentError(f"{label}: 'player_input.modifier_param' needs 'dice_param'")
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
    param = data.get("param")
    if not isinstance(param, str) or not param:
        raise ContentError(f"{label}: 'player_input.param' must name a function parameter")
    prompt = parameters["properties"].get(PROMPT_ARGUMENT)
    if not isinstance(prompt, dict) or prompt.get("type") != "string":
        raise ContentError(
            f"{label}: a tool with 'player_input' needs a string '{PROMPT_ARGUMENT}' parameter"
        )
    if PROMPT_ARGUMENT not in parameters.get("required", []):
        raise ContentError(f"{label}: '{PROMPT_ARGUMENT}' must be required")
    if param in parameters["properties"]:
        raise ContentError(f"{label}: 'player_input.param' must not be a schema parameter")
    accepted = inspect.signature(function).parameters
    if param not in accepted:
        raise ContentError(f"{label}: function does not accept parameter {param!r}")
    details = _check_template(data.get("details"), "player_input.details", label)
    if details is not None:
        _check_fields(details, parameters["properties"], "player_input.details", label)
    return PlayerInput(
        dice=dice,
        dice_param=dice_param,
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


def _check_parameters(schema: object, label: str) -> dict[str, Any]:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ContentError(f"{label}: 'parameters' must be an object schema with type 'object'")
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ContentError(f"{label}: 'parameters' needs a 'properties' object")
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(
        isinstance(item, str) and item in properties for item in required
    ):
        raise ContentError(f"{label}: 'required' must list names from 'properties'")
    return schema


def _check_signature(
    function: Callable[..., Any],
    parameters: Mapping[str, Any],
    label: str,
    consumed: frozenset[str] = frozenset(),
) -> None:
    """The GM calls the function with keyword arguments taken from the schema, so both must agree."""
    accepted = inspect.signature(function).parameters
    takes_any_keyword = any(p.kind is p.VAR_KEYWORD for p in accepted.values())
    if not takes_any_keyword:
        for name in parameters["properties"]:
            if name not in accepted and name not in consumed:
                raise ContentError(f"{label}: function does not accept parameter {name!r}")
    required = set(parameters.get("required", []))
    for name, param in accepted.items():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD) or name == CONTEXT_ARGUMENT:
            continue
        if param.default is param.empty and name not in required:
            raise ContentError(
                f"{label}: parameter {name!r} has no default, so it must be required"
            )
