"""World templates and rule sets, loaded once from the bundled data directory."""

from __future__ import annotations

import importlib
import inspect
import re
import string
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from cygnusforge.content.character import (
    CampaignSpec,
    CharacterSpec,
    Feature,
    TableOption,
    build_campaign,
    build_character,
    character_schema,
    load_feature,
    rule_ids,
)
from cygnusforge.content.definitions import (
    ContentError,
    Definition,
    read_definitions,
    required_text,
)
from cygnusforge.rules.dice import FORMULA, PLUS_MODES, is_formula
from cygnusforge.services.attacks import ATTACK_FLAGS

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
# A dice table row's roll: one number, or an inclusive range such as "3-5".
ROLL_RANGE = re.compile(r"([0-9]+)(?:-([0-9]+))?")
PROMPT_ARGUMENT = "prompt"
# A tool flagged `character_options` gets this argument, built from the rule set's character spec.
CHARACTER_ARGUMENT = "character"
# A tool flagged `npc_hp_tier` gets this optional argument, whose values are the rule set's HP tiers.
NPC_HP_TIER_ARGUMENT = "hp_tier"
# A tool flagged `travel` gets this argument, with the rule set's travel modes as its choices.
TRAVEL_ARGUMENT = "travel"
DEFAULT_TRAVEL_MODE = "on_foot"
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
    # An icon id from the icon registry, shown on the map. Places without one get a default.
    icon: str | None = None
    # The ids of rules attached to this place. Their text reaches the GM while the player is here.
    rules: tuple[str, ...] = ()


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
    # the Warden gives it stats with update_npc.
    story_only: bool = False
    # The ids of rules attached to this NPC. Their text reaches the GM while the NPC is here.
    rules: tuple[str, ...] = ()
    # The rule set's NPC HP tier, such as "average", for an NPC with no statblock. None uses the
    # rule set's ordinary character HP roll.
    hp_tier: str | None = None


@dataclass(frozen=True)
class WorldStart:
    """Where a new campaign's player character begins, and the hooks that bring them there."""

    location: str
    hooks: str


@dataclass(frozen=True)
class WorldTemplate:
    id: str
    name: str
    description: str
    locations: tuple[LocationTemplate, ...]
    npcs: tuple[NpcTemplate, ...]
    # The world's monsters, tables, features, and rules, as definitions. They are checked against
    # each rule set in WorldContent, since a monster's stats depend on the rule set.
    definitions: tuple[Definition, ...] = ()
    start: WorldStart | None = None


@dataclass(frozen=True)
class WorldContent:
    """A world's own monsters, tables, features, and rules, checked against one rule set."""

    monsters: Mapping[str, MonsterTemplate]
    tables: Mapping[str, tuple[TableOption, ...]]
    features: Mapping[str, Feature]
    rules: Mapping[str, str]


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
    # A string argument naming the weapon. The engine rolls that weapon's dice itself, so the
    # player's roll is only for the table and no total is passed to the function.
    weapon_param: str | None = None
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
    """A kind of ground on an encounter map. Entering difficult ground costs two movement."""

    id: str
    name: str
    color: str
    difficult: bool = False


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

    def row_for(self, roll: int) -> DiceRow:
        return next(row for row in self.rows if row.low <= roll <= row.high)


@dataclass(frozen=True)
class MonsterTemplate:
    """A statblock a GM can base a monster on. `stats` has every stat of the rule set's character,
    and `attacks` maps each attack's name to its dice formula. `attack_flags` gives the flags of
    the attacks that have any, such as blast. A large group is a group the Warden fights as one
    unit. `rules` are the ids of the rules attached to the monster; `rule_text` holds the text of
    those written in the statblock itself."""

    id: str
    name: str
    description: str
    stats: Mapping[str, int]
    armor: int
    attacks: Mapping[str, str]
    large_group: bool = False
    rules: tuple[str, ...] = ()
    attack_flags: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    rule_text: Mapping[str, str] = field(default_factory=dict)


DAMAGE_KINDS = ("absorb", "harm", "overflow")
# When a condition can end: by a rest, extended rest, aid from someone else, the passing of time,
# or the GM's say-so. A condition's `ends` lists the ones that end it; `gm` always does.
CONDITION_ENDS = ("rest", "extended_rest", "aid", "time", "gm")
# The effect ops an injury or condition may run, in the order they are listed.
EFFECT_OPS = ("note", "set_max", "condition", "save", "pick_stat")
# `if_higher` compares the roll with the maximum; `if_above_current` compares it with the current score.
MAX_MODES = ("if_higher", "if_above_current", "add", "always")
# The name a `pick_stat` op stores its choice under, for later ops to use as their `stat`.
PICKED = "$picked"
DICE_SIDES = re.compile(r"1d[0-9]+")
# The Injuries table has one entry for each hit that takes exactly 0 HP, up to this many points.
INJURY_ROLLS = 12


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
    and `on_end` are the effect ops run when it does. A derived condition has `derived` instead."""

    id: str
    label: str
    description: str
    ends: tuple[str, ...]
    on_end: tuple[Mapping[str, Any], ...]
    derived: DerivedRule | None = None


@dataclass(frozen=True)
class InjuryEntry:
    roll: int
    name: str
    text: str
    effects: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class InjuryTable:
    """The Injuries table, with the `stat` whose hit that reaches 0 rolls on it."""

    id: str
    stat: str
    entries: tuple[InjuryEntry, ...]


@dataclass(frozen=True)
class MoraleRules:
    """When a side of NPCs must make a morale save and what it is made with. `save_stat` is the
    leader's stat the save uses. `first_casualty` and `half` say whether a side checks at its first
    casualty and at half its number. `lone_at` is the stat a lone foe checks at when it reaches 0.
    `retreat_stat` is the stat a character saves against to run from a fight."""

    save_stat: str
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
    injury_table: InjuryTable | None = None
    # The stats outside combat harm can take, such as strength, dexterity, and willpower.
    outside_harm: tuple[str, ...] = ()
    morale: MoraleRules | None = None
    modifiers: Mapping[str, str] = field(default_factory=dict)
    # A player character who is out of action and dying dies when this many hours pass unaided.
    # Zero means dying characters do not die on a clock.
    dying_hours: int = 0
    # The conditions that leave a character dying, and the ones that make a failed save fatal.
    dying_conditions: tuple[str, ...] = ()
    fatal_conditions: tuple[str, ...] = ()
    # The dice an unarmed attack rolls, and the names that mean unarmed, such as "fists".
    unarmed_damage: str = ""
    unarmed_names: frozenset[str] = frozenset()
    # The items a character can use on a dying character to stop the clock, by name.
    aid_items: tuple[str, ...] = ()
    # The most armor a character can have. Zero means armor has no cap.
    armor_max: int = 0


@dataclass(frozen=True)
class ClockRules:
    """The parts of a day, in order. A place not visited for `stale_days` days is flagged when a
    new day begins, so the Warden can update it."""

    parts: tuple[str, ...]
    stale_days: int
    # The currency feature a hireling's wage is paid in, taken from its employer each day. Empty
    # when the rule set has no wages.
    wage_currency: str = ""
    # How many hours each part of the day holds. Hours count the time a dying character has left.
    hours_per_part: int = 1


@dataclass(frozen=True)
class RestLength:
    """A rest the Warden can call for. It passes `hours` hours, `parts` parts of a day, or `days`
    days, and `until_morning` runs on to the next morning. A full rest restores what `restores`
    lists and clears the item kinds in `clears`. A deprived character recovers nothing."""

    id: str
    hours: int
    parts: int
    days: int
    until_morning: bool
    full: bool
    restores: tuple[str, ...]
    clears: tuple[str, ...]
    # Whether the party eats during this rest: each character who is not lodged uses a food use.
    food: bool = False
    # For a rest that runs until morning: a part it begins in maps to the part it ends at instead,
    # such as a night rest begun in the morning that stops at the night part.
    stop_at: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class NeedRules:
    """A character deprived of a crucial need gains one `penalty_item` a day once it has gone on
    for more than `after_days` days."""

    penalty_item: str
    after_days: int


@dataclass(frozen=True)
class InventoryRules:
    """How many slots a character's inventory has, what the penalty costs, and which stat takes harm
    when the inventory fills. `item_sizes` is in `RuleSet`."""

    capacity: int
    penalty_name: str
    # How long the penalty lasts, as a phrase, such as "until a full night's rest".
    penalty_lasts: str
    penalty_item: str
    penalty_slots: int
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
    """The item kinds magic uses, the size each takes, and the penalty a spellbook cast costs.
    A spellbook holds one spell, a scroll is one use of a spell, and a charged item has uses that recharge."""

    spellbook_kind: str
    spellbook_size: str
    scroll_kind: str
    scroll_size: str
    charged_kind: str
    cast_penalty: int


@dataclass(frozen=True)
class PromptFragment:
    """A rule-set sentence group for the GM prompt, such as how armor absorbs a blow."""

    id: str
    text: str


@dataclass(frozen=True)
class RuleSet:
    id: str
    name: str
    description: str
    rules: Mapping[str, str]
    tool_calls: Mapping[str, ToolCall]
    character: CharacterSpec
    # What "+" between dice terms means: "sum" adds them, "highest" keeps the highest total.
    plus: str
    # Options shared by every character in a campaign, such as the omen.
    campaign: CampaignSpec
    # Named option lists, such as "personality", used to generate NPC traits.
    tables: Mapping[str, tuple[TableOption, ...]] = field(default_factory=dict)
    # Rules whose text goes into every prompt. The others are read with lookup_rule.
    important: frozenset[str] = frozenset()
    # Rules written into a statblock, by id. They are read with lookup_rule, and reach the GM
    # while a monster that carries them is present, but are not listed in every prompt.
    attached_rules: Mapping[str, str] = field(default_factory=dict)
    # The scale of encounter maps, or None when the rule set has no grid: its encounters have no
    # movement limit.
    grid: EncounterGrid | None = None
    # How combat resolves, or None when the rule set has no combat.
    combat: CombatRules | None = None
    # Statblocks for monsters, by id.
    monsters: Mapping[str, MonsterTemplate] = field(default_factory=dict)
    # HP for NPCs with no statblock, by tier name, such as "average": 3.
    npc_hp: Mapping[str, int] = field(default_factory=dict)
    # Conditions an injury or effect can give, by id. Derived conditions are included.
    conditions: Mapping[str, ConditionRules] = field(default_factory=dict)
    # Tables rolled with dice, by id, such as the Warden's reaction table.
    dice_tables: Mapping[str, DiceTable] = field(default_factory=dict)
    # Time, rest, and needs. Each is None or empty when the rule set does not track them.
    clock: ClockRules | None = None
    rests: Mapping[str, RestLength] = field(default_factory=dict)
    # The rest length an interrupted rest counts as, such as "moments".
    interrupted_rest: str = ""
    need: NeedRules | None = None
    inventory: InventoryRules | None = None
    # Item sizes by id, as the slots an item of that size takes.
    item_sizes: Mapping[str, int] = field(default_factory=dict)
    # How magic uses items, or None when the rule set has no magic.
    magic: MagicRules | None = None
    # The food a party eats on rests, or None when the rule set does not track food.
    food: FoodRules | None = None
    # What the marketplace sells, or None when the rule set has no marketplace.
    marketplace: Marketplace | None = None
    # Prompt fragments the GM prompt adds after its generic text, in file order.
    prompt_fragments: tuple[PromptFragment, ...] = ()
    # The most sentences a speech line may run, or None when the rule set does not cap it.
    sentence_cap: int | None = None
    # How fast travel goes, or None when the rule set does not charge time for travel.
    travel: Travel | None = None
    # Encounter-map content, by id: ground, objects that stand on it, and starter maps.
    terrain: Mapping[str, EncounterTerrain] = field(default_factory=dict)
    encounter_objects: Mapping[str, EncounterObject] = field(default_factory=dict)
    encounter_templates: Mapping[str, EncounterTemplate] = field(default_factory=dict)


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


WORLD_TYPES = ("monster", "table", "feature", "rule")
STATBLOCK_KEYS = ("stats", "armor", "attacks")


def _load_world(directory: Path) -> WorldTemplate:
    template = directory.name
    definitions = read_definitions(directory)
    world = _single(definitions, "world", template)
    _check_directory_id(template, world.id)
    label = f"{template}: world"
    locations = tuple(_load_location(d, template) for d in definitions if d.type == "location")
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
    _check_unique_names([*locations, *npcs], template)
    return WorldTemplate(
        id=world.id,
        name=required_text(world.data, "name", label),
        description=required_text(world.data, "description", label),
        locations=locations,
        npcs=npcs,
        definitions=(*own, *statblocks),
        start=_load_world_start(world.data, label, location_ids),
    )


def _load_world_start(
    data: Mapping[str, Any], label: str, location_ids: set[str]
) -> WorldStart | None:
    """The optional `start` object: a known location and the hooks that bring the party there."""
    if "start" not in data:
        return None
    start = data["start"]
    if not isinstance(start, Mapping):
        raise ContentError(f"{label}: 'start' must be an object")
    start_label = f"{label}: start"
    location = required_text(start, "location", start_label)
    if location not in location_ids:
        raise ContentError(f"{start_label} names unknown location {location!r}")
    return WorldStart(location=location, hooks=required_text(start, "hooks", start_label))


def _has_statblock(data: Mapping[str, Any]) -> bool:
    return any(key in data for key in STATBLOCK_KEYS)


def _npc_statblock(definition: Definition, template: str) -> Definition:
    """An NPC's own statblock, as a monster with the NPC's id. Monster fields come from the NPC."""
    fields = ("name", "description", *STATBLOCK_KEYS)
    data = {key: definition.data[key] for key in fields if key in definition.data}
    return Definition(
        type="monster", id=definition.id, data={"type": "monster", "id": definition.id, **data}
    )


def _build_world_content(template: WorldTemplate, rule_set: RuleSet) -> WorldContent:
    name = template.id
    own = template.definitions
    monsters = {
        d.id: _load_monster(d, rule_set.character, name, pool=_hit_pool(rule_set.combat))
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
    features = {d.id: load_feature(d, name) for d in own if d.type == "feature"}
    rules = {
        d.id: required_text(d.data, "text", f"{name}: rule {d.id}") for d in own if d.type == "rule"
    }
    written = _attached_rules(monsters.values(), name)
    shared = sorted(written.keys() & rules.keys())
    if shared:
        raise ContentError(f"{name}: rules {shared} are both world rules and statblock rules")
    rules = {**rules, **written}
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
    return WorldContent(
        monsters=monsters,
        tables={d.id: _load_table(d, name) for d in own if d.type == "table"},
        features=features,
        rules=rules,
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
        icon=_optional_text(data, "icon", label),
        rules=rule_ids(data, label),
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
    injury_tables = {
        d.id: _load_injury_entries(d, character, conditions, template)
        for d in definitions
        if d.type == "injury_table"
    }
    combat = (
        _load_combat(combats[0], modules, character, injury_tables, template) if combats else None
    )
    monsters = {
        d.id: _load_monster(d, character, template, pool=_hit_pool(combat))
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
    rests = _load_rests(definitions, character, template, clock)
    interrupted_rest = _load_interrupted_rest(definitions, rests, template)
    need = _load_need(definitions, template)
    inventory = _load_inventory(definitions, character, template)
    item_sizes = _load_item_sizes(definitions, template)
    _check_item_sizes(character, item_sizes, template)
    magic = _load_magic(definitions, item_sizes, template)
    food = _load_food(definitions, rests, item_sizes, character, template)
    marketplace = _load_marketplace(definitions, item_sizes, character, template)
    prompt_fragments = _load_prompts(definitions, template)
    sentence_cap = _load_sentence_cap(definitions, template)
    travel = _load_travel(definitions, template)
    terrain = _load_encounter_terrain(definitions, template)
    encounter_objects = _load_encounter_objects(definitions, template)
    encounter_templates = _load_encounter_templates(
        definitions, terrain, encounter_objects, template
    )
    npc_hp = _npc_hp(head.data, label)
    tool_calls: dict[str, ToolCall] = {}
    tool_names: set[str] = set()
    for d in definitions:
        if d.type != "tool_call":
            continue
        call = _load_tool_call(d, modules, character, template, npc_hp, travel)
        if call.name in tool_names:
            raise ContentError(f"{template}: duplicate tool name {call.name!r}")
        tool_names.add(call.name)
        tool_calls[call.id] = call
    return RuleSet(
        id=head.id,
        name=required_text(head.data, "name", label),
        description=required_text(head.data, "description", label),
        rules=rules,
        attached_rules=written,
        important=important,
        tool_calls=tool_calls,
        character=character,
        plus=_plus_mode(head.data, label),
        npc_hp=npc_hp,
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
        need=need,
        inventory=inventory,
        item_sizes=item_sizes,
        magic=magic,
        food=food,
        marketplace=marketplace,
        prompt_fragments=prompt_fragments,
        sentence_cap=sentence_cap,
        travel=travel,
        terrain=terrain,
        encounter_objects=encounter_objects,
        encounter_templates=encounter_templates,
    )


def _load_travel(definitions: list[Definition], template: str) -> Travel | None:
    definition = _single_optional(definitions, "travel", template)
    if definition is None:
        return None
    label = f"{template}: travel {definition.id}"
    modes_data = definition.data.get("modes")
    if not isinstance(modes_data, dict) or not modes_data:
        raise ContentError(f"{label}: 'modes' must map mode names to speeds")
    modes = {}
    for name, mode in modes_data.items():
        mode_label = f"{label}: mode {name!r}"
        if not isinstance(mode, dict):
            raise ContentError(f"{mode_label} must be an object")
        speed = mode.get("km_per_hour")
        if not isinstance(speed, (int, float)) or isinstance(speed, bool) or speed <= 0:
            raise ContentError(f"{mode_label}: 'km_per_hour' must be a number above 0")
        modes[name] = TravelMode(
            name=name,
            km_per_hour=float(speed),
            note=required_text(mode, "note", mode_label),
        )
    default = definition.data.get("default_leg")
    if not isinstance(default, dict):
        raise ContentError(f"{label}: 'default_leg' must be an object")
    return Travel(
        modes=modes,
        default_leg_minutes=_whole(default, "minutes", f"{label}: default_leg", 1),
        default_leg_note=required_text(default, "note", f"{label}: default_leg"),
    )


def _load_combat(
    definition: Definition,
    modules: Mapping[str, ModuleType],
    character: CharacterSpec,
    injury_tables: Mapping[str, tuple[InjuryEntry, ...]],
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
    if "player_character_dead" in defeat_data:
        defeat["player_character_dead"] = required_text(
            defeat_data, "player_character_dead", f"{label}: defeat"
        )
    injury_table = _load_injury_table_ref(data, character, injury_tables, label)
    outside_harm = _load_outside_harm(data, character, label)
    unarmed_damage, unarmed_names = _load_unarmed(data, label)
    return CombatRules(
        id=definition.id,
        module=module,
        damage=damage,
        check_die=check_die,
        check_sides=int(check_die[2:]),
        initiative_stat=initiative_stat,
        defeat=defeat,
        injury_table=injury_table,
        outside_harm=outside_harm,
        morale=_load_morale(data, character, label),
        modifiers=_load_modifiers(data, label),
        dying_hours=_whole(data, "dying_hours", label, 0) if "dying_hours" in data else 0,
        armor_max=_whole(data, "armor_max", label, 0),
        dying_conditions=_names(data, "dying_conditions", label),
        fatal_conditions=_names(data, "fatal_conditions", label),
        aid_items=_names(data, "aid_items", label),
        unarmed_damage=unarmed_damage,
        unarmed_names=unarmed_names,
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
        for key in ("save_stat", "lone_at", "retreat_stat")
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
        save_stat=stats["save_stat"],
        first_casualty=flags["first_casualty"],
        half=flags["half"],
        lone_at=stats["lone_at"],
        retreat_stat=stats["retreat_stat"],
    )


def _load_injury_table_ref(
    data: Mapping[str, Any],
    character: CharacterSpec,
    injury_tables: Mapping[str, tuple[InjuryEntry, ...]],
    label: str,
) -> InjuryTable | None:
    """The `injuries` key names the Injuries table to roll on and the stat whose hit to 0 rolls on it."""
    if "injuries" not in data:
        return None
    ref = data["injuries"]
    if not isinstance(ref, Mapping):
        raise ContentError(f"{label}: 'injuries' must be an object")
    table_id = required_text(ref, "table", f"{label}: injuries")
    if table_id not in injury_tables:
        raise ContentError(f"{label}: 'injuries' names no injury table ({table_id!r})")
    stat = required_text(ref, "stat", f"{label}: injuries")
    if stat not in character.stats:
        raise ContentError(f"{label}: 'injuries' names no stat ({stat!r})")
    return InjuryTable(id=table_id, stat=stat, entries=injury_tables[table_id])


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


def _load_unarmed(data: Mapping[str, Any], label: str) -> tuple[str, frozenset[str]]:
    """The dice an unarmed attack rolls and the names that mean unarmed. Optional; names are
    compared without regard to case."""
    block = data.get("unarmed")
    if block is None:
        return "", frozenset()
    if not isinstance(block, Mapping):
        raise ContentError(f"{label}: 'unarmed' must be an object")
    damage = block.get("damage")
    if not isinstance(damage, str) or not is_formula(damage):
        raise ContentError(f"{label}: 'unarmed.damage' must be a dice formula such as '1d4'")
    names = _names(block, "names", f"{label}: unarmed")
    if not names:
        raise ContentError(f"{label}: 'unarmed.names' needs at least one name")
    return damage, frozenset(name.strip().casefold() for name in names)


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
) -> MonsterTemplate:
    label = f"{template}: monster {definition.id}"
    data = definition.data
    stats = data.get("stats")
    if not isinstance(stats, dict) or set(stats) != set(character.stats):
        names = ", ".join(sorted(character.stats))
        raise ContentError(f"{label}: 'stats' must give a whole number for each of: {names}")
    for key, value in stats.items():
        _check_whole(value, f"{label}: stat {key!r}")
    if pool is not None and stats[pool] < 1:
        raise ContentError(f"{label}: stat {pool!r} must be at least 1")
    armor = data.get("armor", 0)
    _check_whole(armor, f"{label}: 'armor'")
    attacks, attack_flags = _statblock_attacks(data.get("attacks", {}), label)
    large_group = data.get("large_group", False)
    if not isinstance(large_group, bool):
        raise ContentError(f"{label}: 'large_group' must be true or false")
    rules, rule_text = _statblock_rules(data, label)
    return MonsterTemplate(
        id=definition.id,
        name=required_text(data, "name", label),
        description=required_text(data, "description", label),
        stats=dict(stats),
        armor=armor,
        attacks=attacks,
        large_group=large_group,
        rules=rules,
        attack_flags=attack_flags,
        rule_text=rule_text,
    )


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
        elif isinstance(entry, dict) and "dice" in entry and set(entry) <= {"dice", "flags"}:
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


def _color(data: Mapping[str, Any], label: str) -> str:
    color = required_text(data, "color", label)
    if not _HEX_COLOR.fullmatch(color):
        raise ContentError(f"{label}: color must be a hex colour like #3a3f47, not {color!r}")
    return color


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
        terrain[d.id] = EncounterTerrain(
            id=d.id,
            name=required_text(d.data, "name", label),
            color=_color(d.data, label),
            difficult=_flag(d.data, "difficult", label),
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
        objects[d.id] = EncounterObject(
            id=d.id,
            name=required_text(d.data, "name", label),
            icon=required_text(d.data, "icon", label),
            color=_color(d.data, label),
            blocks=_flag(d.data, "blocks", label),
            solid=_flag(d.data, "solid", label),
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
        first = _corner(fill, "from", columns, rows, fill_label)
        last = _corner(fill, "to", columns, rows, fill_label)
        values = _changes(fill, terrain, objects, fill_label)
        for column in range(min(first[0], last[0]), max(first[0], last[0]) + 1):
            for row in range(min(first[1], last[1]), max(first[1], last[1]) + 1):
                state[column, row].update(values)
    for cell in _entries(data, "cells", label):
        cell_label = f"{label}: cell"
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


def _load_prompts(definitions: list[Definition], template: str) -> tuple[PromptFragment, ...]:
    """The `prompt` definitions, in file order. Each is a sentence group the GM prompt adds."""
    fragments = []
    ids: set[str] = set()
    for d in definitions:
        if d.type != "prompt":
            continue
        label = f"{template}: prompt {d.id!r}"
        if d.id in ids:
            raise ContentError(f"{template}: duplicate prompt {d.id!r}")
        ids.add(d.id)
        fragments.append(PromptFragment(id=d.id, text=required_text(d.data, "text", label)))
    return tuple(fragments)


def _load_sentence_cap(definitions: list[Definition], template: str) -> int | None:
    """The speech sentence cap from the `narration` definition, or None when there is none."""
    head = _single_optional(definitions, "narration", template)
    if head is None:
        return None
    return _whole(head.data, "sentence_cap", f"{template}: narration", minimum=1)


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
    wage_currency = definition.data.get("wage_currency", "")
    if not isinstance(wage_currency, str):
        raise ContentError(f"{label}: 'wage_currency' must be text")
    hours_per_part = (
        _whole(definition.data, "hours_per_part", label, 1)
        if "hours_per_part" in definition.data
        else 1
    )
    return ClockRules(
        parts=parts,
        stale_days=stale_days,
        wage_currency=wage_currency,
        hours_per_part=hours_per_part,
    )


def _load_rests(
    definitions: list[Definition],
    character: CharacterSpec,
    template: str,
    clock: ClockRules | None,
) -> dict[str, RestLength]:
    """The rest lengths of the rule set's single rest definition, by id. Each needs `full` and at
    least one of `parts`, `days`, or `until: "morning"`. `stop_at` maps a part of the day to the
    part a rest begun in it ends at instead, and needs `until` and a clock."""
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
        food = data.get("food", False)
        if not isinstance(food, bool):
            raise ContentError(f"{length_label}: 'food' must be true or false")
        loaded[length_id] = RestLength(
            id=length_id,
            hours=hours,
            parts=parts,
            days=days,
            until_morning=until == "morning",
            full=full,
            restores=restores,
            clears=_names(data, "clears", length_label),
            food=food,
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


def _load_need(definitions: list[Definition], template: str) -> NeedRules | None:
    definition = _single_optional(definitions, "need", template)
    if definition is None:
        return None
    label = f"{template}: need {definition.id}"
    return NeedRules(
        penalty_item=required_text(definition.data, "penalty_item", label),
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
        penalty_name=required_text(definition.data, "penalty_name", label),
        penalty_lasts=required_text(definition.data, "penalty_lasts", label),
        penalty_item=required_text(definition.data, "penalty_item", label),
        penalty_slots=_whole(definition.data, "penalty_slots", label, 0),
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
        charged_kind=required_text(definition.data, "charged_kind", label),
        cast_penalty=_whole(definition.data, "cast_penalty", label, 0),
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


def _load_injury_entries(
    definition: Definition,
    character: CharacterSpec,
    conditions: Mapping[str, ConditionRules],
    template: str,
) -> tuple[InjuryEntry, ...]:
    """An Injuries table: one entry for each roll from 1 to INJURY_ROLLS, in order."""
    label = f"{template}: injury table {definition.id}"
    entries = definition.data.get("entries")
    if not isinstance(entries, list) or len(entries) != INJURY_ROLLS:
        raise ContentError(f"{label}: 'entries' must list exactly {INJURY_ROLLS} entries")
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
            InjuryEntry(
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
    npc_hp: Mapping[str, int],
    travel: Travel | None = None,
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
    if _flag(data, "npc_hp_tier", label) and npc_hp:
        parameters = _with_npc_hp_tier(parameters, npc_hp, label)
    if _flag(data, "travel", label) and travel is not None:
        parameters = _with_travel(parameters, travel, label)
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
    tier = {"type": "string", "enum": sorted(npc_hp), "description": "The NPC's HP tier."}
    properties = {**parameters["properties"], NPC_HP_TIER_ARGUMENT: tier}
    return {**parameters, "properties": properties}


def _with_travel(parameters: dict[str, Any], travel: Travel, label: str) -> dict[str, Any]:
    """Add the rule set's travel modes as an optional `travel` argument."""
    if TRAVEL_ARGUMENT in parameters["properties"]:
        raise ContentError(f"{label}: {TRAVEL_ARGUMENT!r} is generated and must not be declared")
    mode = {
        "type": "string",
        "enum": sorted(travel.modes),
        "default": DEFAULT_TRAVEL_MODE,
        "description": "How the party travels. Defaults to on foot.",
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
    data: object, parameters: Mapping[str, Any], function: Callable[..., Any], label: str
) -> PlayerInput | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ContentError(f"{label}: 'player_input' must be an object")
    dice = data.get("dice")
    dice_param = data.get("dice_param")
    weapon_param = data.get("weapon_param")
    modifier_param = data.get("modifier_param")
    sources = [source for source in (dice, dice_param, weapon_param) if source is not None]
    if len(sources) != 1:
        raise ContentError(
            f"{label}: 'player_input' needs exactly one of 'dice', 'dice_param', or 'weapon_param'"
        )
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
        accepted = inspect.signature(function).parameters
        if param not in accepted:
            raise ContentError(f"{label}: function does not accept parameter {param!r}")
    details = _check_template(data.get("details"), "player_input.details", label)
    if details is not None:
        _check_fields(details, parameters["properties"], "player_input.details", label)
    return PlayerInput(
        dice=dice,
        dice_param=dice_param,
        weapon_param=weapon_param,
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
