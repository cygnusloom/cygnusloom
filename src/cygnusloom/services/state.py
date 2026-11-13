"""The campaign state a rule set and the engine read: characters, places, maps, and turns.

`load_state` in `cygnusloom.services.events` folds the event log into these dataclasses. They are
plain state models; the event classes and persistence live in `events`."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cygnusloom.llm.base import ToolRequest
from cygnusloom.services.tools import Ask

PC_KIND = "pc"
NPC_KIND = "npc"
CAMPAIGN_WIDE = "campaign-wide"
DEFAULT_DIMENSION = "default"
# Token counts are estimates: about four characters per token.
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


class Channel(StrEnum):
    TRANSCRIPT = "transcript"
    LOG = "log"
    INTERNAL = "internal"


@dataclass(frozen=True)
class ContextPart:
    """One piece of what the model was given: its role, where it came from, and its text."""

    role: str
    source: str
    content: str

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.content)

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "source": self.source, "content": self.content}


@dataclass(frozen=True)
class Position:
    """Where a place is on the world map, in kilometers from the map's center: x grows east and y
    grows north. The map is flat for now, so z stays 0 and the dimension stays the default."""

    x: float
    y: float
    z: float = 0.0
    dimension: str = DEFAULT_DIMENSION


@dataclass(frozen=True)
class Place:
    name: str
    description: str
    # Discovered places are ones the player character has visited or heard about in detail.
    discovered: bool = False
    position: Position | None = None
    # The place this one is inside, such as a building in a city. Such a place has no position of
    # its own; it is shown with its parent.
    parent: str | None = None
    # The campaign day a player character was last in this place, or None if none has been.
    visited_day: int | None = None
    # The campaign day a fight last ended here, so the GM can describe the aftermath and clean
    # the scene. Cleared when the location's description is updated.
    fight_day: int | None = None
    icon: str | None = None
    # What the player sees of a discovered place. The full description stays GM-only.
    summary: str | None = None
    # The ids of rules attached to the place, whose text reaches the GM while the player is here.
    rules: tuple[str, ...] = ()
    # Authored overland routes from this place, by destination location id. A route gives the
    # path, distance, terrain, and vast of an overland move that leads there.
    routes: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The world's id for this place, such as "isthmus-town", or None for one the GM invented.
    # Authored routes are keyed by it.
    location_id: str | None = None


# The looks the player's world map may choose. The CSS colours each palette, so a campaign's
# world can shift from a warm parchment chart to a cold midnight atlas without new art.
WORLD_MAP_PALETTES = (
    "parchment",
    "atlas",
    "verdant",
    "ember",
    "frost",
    "midnight",
    "ivory",
    "obsidian",
)


@dataclass(frozen=True)
class Region:
    """An area of one kind of ground on the world map. Its points are outline corners in
    kilometers from the map's center, in order around the edge. It is shown on the player's map
    once it is discovered, or once a discovered place lies inside it. A colour, pattern, ink, or
    label that is None follows the terrain kind; an empty pattern hides the texture."""

    name: str
    terrain: str
    points: tuple[tuple[float, float], ...]
    discovered: bool = False
    color: str | None = None
    pattern: str | None = None
    ink: str | None = None
    label: str | None = None


@dataclass(frozen=True)
class WorldMapStyle:
    """The look of the player's world map: its palette, ambient light, custom ground and line
    colours, and whether the chart draws clouds and contour lines."""

    palette: str = "parchment"
    ambient: str = "day"
    background_color: str = ""
    line_color: str = ""
    show_clouds: bool = True
    show_contours: bool = True


@dataclass(frozen=True)
class Item:
    """Something a character carries. `size` is the rule set's item size, empty for the penalty item, and
    `slots` is what it takes from the inventory. `kind` marks special items such as the penalty item."""

    name: str
    size: str
    slots: int
    kind: str = ""
    # A magic item's spell text, or a charged item's recharge rule.
    text: str = ""
    # A charged item's uses left, and the most it holds.
    uses: int = 0
    max_uses: int = 0
    # The clock when a charged item was last used. It cannot recharge until the clock moves on.
    spent_on: Clock | None = None
    # A weapon's damage dice, such as "d6". Empty when the item is not a weapon.
    damage: str = ""
    # The ids of rules attached to the item. Their text reaches the GM while the item is carried.
    rules: tuple[str, ...] = ()
    # Engine flags the item carries, such as "shield".
    flags: tuple[str, ...] = ()
    # True when the item is readied, false when it is stowed and needs an action to retrieve.
    ready: bool = True


def _position(x: float | None, y: float | None) -> Position | None:
    return None if x is None or y is None else Position(x=x, y=y)


@dataclass
class CustomFeature:
    """A feature invented during play, such as a researched spell, potion, or enchantment. It is
    kept on the character rather than in the rule set's content, so it is available to the sheet,
    feature lookups, and the compendium without changing the loaded files."""

    id: str
    name: str
    feature_type: str
    description: str
    tag: str = ""
    rank: int = 0
    subject: str = ""
    base: str = ""
    mods: list[str] = field(default_factory=list)
    stress: int = 0
    cause: str = ""


@dataclass
class FactionAsset:
    """One asset a faction owns. Its kind names the rule set's asset definition; its stats are
    resolved when it is bought, so later rule-set changes do not rewrite the campaign. An asset
    may act once per faction turn, so it is ready while `acted_turn` is behind the faction's
    current `turn`."""

    id: str
    kind: str
    name: str
    category: str
    hp_max: int
    hp: int
    magic: str
    attack: str = ""
    counter: str = ""
    qualities: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    acted_turn: int = 0


@dataclass
class Faction:
    """A campaign faction the GM invents, such as a magic-school college. Its points live in the
    tally named `tally`, under the entry `entry`, so the existing cup, award, and reset paths
    keep it. A rule set with no faction tally makes a tally-less faction, whose assets and turn
    are its whole record."""

    id: str
    name: str
    description: str
    trait: str
    tally: str
    entry: str
    turn: int = 1
    assets: dict[str, FactionAsset] = field(default_factory=dict)

    def asset_ready(self, asset: FactionAsset) -> bool:
        """Whether the asset may still act this faction turn."""
        return asset.acted_turn < self.turn


@dataclass
class Character:
    kind: str
    name: str
    location: str | None
    description: str
    personality: str
    accent: str
    quirks: list[str]
    # The short player-facing description of what the player knows of this character, shown on
    # the Characters page. The GM's private `description` stays separate and may hold secrets.
    summary: str = ""
    # How the GM refers to the character, such as "she/her". None if it was never recorded.
    pronouns: str | None = None
    # The character options, resolved when the character was created. See content/character.py.
    stats: dict[str, int] = field(default_factory=dict)
    templates: dict[str, str] = field(default_factory=dict)
    features: dict[str, int] = field(default_factory=dict)
    # Features invented during play, by id. A feature listed in `features` may be one of these
    # rather than one of the rule set's own features.
    custom_features: dict[str, CustomFeature] = field(default_factory=dict)
    fields: dict[str, str] = field(default_factory=dict)
    # Skill levels by skill id, for the rule sets that have skills. A skill not listed has no level.
    skills: dict[str, int] = field(default_factory=dict)
    # What each hit absorbs. Monsters have it in their statblock; player characters get it from gear.
    armor: int = 0
    # The armor and shield equipped by explicit choice, and their values. Set by
    # DefenseEquipmentSet. The armor total stays with ArmorSet.
    equipped_armor: str = ""
    equipped_armor_value: int = 0
    equipped_shield: str = ""
    equipped_shield_value: int = 0
    # How much of the armor total comes from the wielded shield, so a shield bash can suspend
    # exactly that contribution.
    shield_bonus: int = 0
    # A statblock's printed AC, without the defense modifier, when equipment is later tracked.
    innate_defense: int = 0
    # An extra adjustment to defense from the equipped gear, set with it.
    defense_adjustment: int = 0
    # True once DefenseEquipmentSet has set the equipment. A character defended by a statblock's
    # absolute AC, such as an NPC, stays False.
    defense_tracked: bool = False
    # A monster's attacks, by name, as dice formulas such as "1d8+1d8". Empty for player characters.
    attacks: dict[str, str] = field(default_factory=dict)
    # The flags of the attacks that have any, by attack name, such as ["blast"].
    attack_flags: dict[str, list[str]] = field(default_factory=dict)
    # Each attack's shock and the armor it shocks through, by attack name, such as [2, 13]. Empty
    # for characters whose rule set has no shock.
    attack_shock: dict[str, list[int]] = field(default_factory=dict)
    # Each attack's melee reach in feet, when it is longer than the rule set's default.
    attack_reach: dict[str, int] = field(default_factory=dict)
    # Each attack's ranged bands and their farthest distance in feet, by band name.
    attack_ranges: dict[str, dict[str, int | None]] = field(default_factory=dict)
    # The name of the bestiary statblock a monster was built from, such as "Wolf". Empty if none.
    creature: str = ""
    # A story-only NPC has no stats yet. It cannot fight, make saves, or be attacked until given stats.
    story_only: bool = False
    # A named NPC the story treats as important: at 0 HP it is mortally wounded, not dead.
    important: bool = False
    # The ids of rules attached to the NPC. Their text reaches the GM while the NPC is here.
    rules: list[str] = field(default_factory=list)
    # Harm taken so far, by stat id. A stat's current value is its score less its harm, at least 0.
    harm: dict[str, int] = field(default_factory=dict)
    # The stat id of a save the character must make after harm overflowed, or None.
    save_due: str | None = None
    defeated: bool = False
    # The ids of the conditions the character has, and the names of their injuries, in the order gained.
    conditions: list[str] = field(default_factory=list)
    injuries: list[str] = field(default_factory=list)
    # The value of each counter the character keeps, by counter id. A counter not listed is at its initial value.
    counters: dict[str, int] = field(default_factory=dict)
    # How many times each limited reaction has been used, keyed "reset:feature:reaction".
    reaction_uses: dict[str, int] = field(default_factory=dict)
    # How many times each advancement option has been taken, by option id.
    advancements_taken: dict[str, int] = field(default_factory=dict)
    # The names of the character's Friends and Rivals, by relation id, in the order added.
    relations: dict[str, list[str]] = field(default_factory=dict)
    # Ran from the fight, or broke and fled. Cleared when the encounter ends.
    fled: bool = False
    # Gave up the fight and laid down arms. Cleared when the encounter ends.
    surrendered: bool = False
    # A debug buff: no harm reaches the character. Set from the debug panel.
    invincible: bool = False
    # A large group is a group the GM fights as one unit.
    large_group: bool = False
    # A large group whose critical save failed. The GM must decide it is defeated or flees.
    critical_due: bool = False
    # Marks the rule set's hooks set on the character, by key. A mark's value is any JSON value.
    marks: dict[str, Any] = field(default_factory=dict)
    # What the character carries. The penalty item is an item of the rule set's penalty kind.
    items: list[Item] = field(default_factory=list)
    # A hireling is a player-controlled character a party member hired. Its employer pays its
    # wage each day. Empty and zero for everyone else.
    hireling: bool = False
    employer: str = ""
    wage: int = 0
    # Hours left before a dying character dies unless aided. None while the character is not dying.
    dying_hours: int | None = None
    # Rounds left in an encounter before a dying character dies. None when no round clock runs.
    dying_rounds: int | None = None
    # The encounter round in which a shield last turned aside Shock, so it can stop only the
    # first Shock in each round.
    shock_blocked_round: int = 0
    # A shield bash suspends the shield's AC until the character's next turn. The armor before
    # the suspension is kept so the next turn can restore it without recomputing the sheet.
    shield_suspended: bool = False
    shield_suspended_armor: int = 0
    # Total defense adds its bonus to the character's armor until the character's next turn,
    # when the armor before the action is restored.
    total_defense: bool = False
    total_defense_armor: int = 0
    # Turns lost to surprise. Each EncounterTurnEnded for this character consumes one.
    surprised_turns: int = 0
    # Grappling links two characters. `grappling` is the character held, and `grappled_by` is
    # the holder. Both carry the rule set's grappled condition.
    grappling: str = ""
    grappled_by: str = ""
    # An execution attack prepared against an unaware target. The weapon is the one prepared.
    execution_prepared: bool = False
    execution_weapon: str = ""
    # Structured feature choices, keyed "feature_id:pick_id", in the order taken.
    feature_choices: dict[str, list[str]] = field(default_factory=dict)
    # Extra modifiers from feature choices, by stat id, such as Developed Attribute's +1.
    stat_modifiers: dict[str, int] = field(default_factory=dict)
    # The character is dead. The body stays where the character fell, with its gear.
    dead: bool = False
    # The campaign day the character fell, for scene clean-up. None while the character has not
    # fallen, or when the fall predates this record.
    fell_day: int | None = None
    # The character has left the story, and the player plays someone else. It can come back later.
    retired: bool = False
    # The account that plays this character. None means the campaign's owner, unless the
    # character was deliberately unclaimed when a player left.
    player_id: int | None = None
    # The account's current display name, filled in when the log is folded. Events store only
    # the account id, so a name change reaches every character and message.
    player_name: str = ""
    # The character has no player and the table shares the part. Owners can leave a character
    # behind when they remove its player.
    unclaimed: bool = False

    @property
    def out_of_play(self) -> bool:
        """Whether the player can no longer act as this character: dead, or left the story."""
        return self.dead or self.retired

    def modifier(self, spec: Any, stat: str) -> int:
        """The stat's modifier, including any bonus a feature choice added."""
        return spec.modifier(self.stats, stat) + self.stat_modifiers.get(stat, 0)


def current(character: Character, stat: str) -> int:
    """A stat's score less the harm it has taken, never below 0."""
    return max(0, character.stats.get(stat, 0) - character.harm.get(stat, 0))


def hit_points(character: Character, stat: str | None) -> tuple[int, int] | None:
    """A character's current and maximum hit points, or None when the rule set has no hit
    point stat or the character has no score in it."""
    if stat is None or stat not in character.stats:
        return None
    return current(character, stat), character.stats[stat]


def modifier_value(character: Character, stat: str) -> int:
    """A stat's score less the harm it has taken. Unlike current, it can go below 0, as a trait
    that a move adds can."""
    return character.stats.get(stat, 0) - character.harm.get(stat, 0)


def defeat_status(character: Character, defeat: Mapping[str, str], *, killed: bool = False) -> str:
    """Where a defeat leaves a character, in the rule set's words: `defeat` maps `player_character`
    and `npc` to a label, and optionally `player_character_dead` for a player character killed
    outright. A character with no label in `defeat` reads as just defeated."""
    if killed and character.kind == PC_KIND and "player_character_dead" in defeat:
        return defeat["player_character_dead"]
    if character.kind == PC_KIND:
        key = "player_character"
    elif character.important and "important_npc" in defeat:
        key = "important_npc"
    else:
        key = "npc"
    return defeat.get(key, "defeated")


def out_of_fight(character: Character) -> bool:
    """Whether a character can no longer act in the encounter: defeated, fled, or surrendered."""
    return character.defeated or character.fled or character.surrendered


@dataclass
class Project:
    """A long-running effort: a project, a piece of research, a magic working, or a faction
    turn. `progress` counts toward `target`; a free-form `kind` keeps the tracker generic."""

    name: str
    kind: str
    goal: str
    target: int
    progress: int = 0
    notes: list[str] = field(default_factory=list)
    completed: bool = False
    # What the project cost, and who paid it.
    cost: int = 0
    currency: str = ""
    owner: str = ""
    abandoned: bool = False


@dataclass(frozen=True)
class Note:
    # The id of the note.added event that wrote it, so it stays the same across replays.
    id: int
    subject: str
    text: str


@dataclass(frozen=True)
class Storyline:
    """A plot thread the GM tracks. Its name and summary are player-facing; the plot, cast, places,
    objects, and notes are the GM's own record and may hold secrets."""

    id: int
    name: str
    summary: str = ""
    plot: str = ""
    characters: tuple[str, ...] = ()
    locations: tuple[str, ...] = ()
    objects: tuple[str, ...] = ()
    notes: str = ""
    active: bool = True
    # A resolved storyline's plot has concluded. It leaves the active and focused lists, but its
    # record and resolution stay searchable so a later storyline can grow from it.
    resolved: bool = False
    # The short player-facing note on how a resolved storyline ended. Empty until resolved.
    resolution: str = ""


@dataclass(frozen=True)
class KnownNpc:
    """What the player character knows of an NPC: the details the player has seen or been told,
    how the NPC stands with the player, and where the player last knew the NPC to be. The GM's
    full record of the NPC may hold more than this."""

    name: str
    description: str = ""
    # What the player knows of the NPC in a short player-facing paragraph.
    summary: str = ""
    personality: str = ""
    accent: str = ""
    quirks: tuple[str, ...] = ()
    pronouns: str | None = None
    # What the player has learned of how the NPC stands with them, such as "friendly, owes a debt".
    relationship: str = ""
    # The place the player last knew the NPC to be, or None when they do not know.
    location: str | None = None


def normalize_journal(text: str) -> str:
    """The journal text with no run of more than one blank line and no surrounding whitespace."""
    return re.sub(r"\n{3,}", "\n\n", text).strip()


@dataclass
class Turn:
    """A GM reply in progress, from the player's message that opened it until the reply is
    recorded. `begun` is false until the model's context is set up, which may summarize first."""

    opener: int
    text: str
    # The campaign's opening: the GM's first turn, which the player does not write.
    opening: bool = False
    begun: bool = False
    head: list[ContextPart] = field(default_factory=list)
    context: list[ContextPart] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    round_start: int = 0
    rounds: int = 0
    queue: list[ToolRequest] = field(default_factory=list)
    narrating: bool = False
    # The player's message is out of character: an out-of-character step replies next. The story
    # is narrated after that reply only when `ooc_narrate` is set.
    ooc: bool = False
    ooc_narrate: bool = True
    prompt: Ask | None = None
    # NPC lines written this turn that the narration has not seen yet: {"speaker", "dialogue"}.
    speech: list[dict[str, str]] = field(default_factory=list)
    # What a turn review found wrong with the narration it set aside, so the next narration can
    # correct it. Cleared when that narration is accepted.
    revision: str = ""
    # Why the last step stopped, when it stopped with an error and has not been retried yet.
    failed: str | None = None
    # The NPC whose encounter turn this reply plays out, or None for a reply to the player.
    actor: str | None = None
    # The account whose message opened this turn, or None for an opening or an NPC's turn. Only
    # that player answers the turn's prompts and continues it.
    player_id: int | None = None
    # The account's current display name, filled in when the log is folded.
    player_name: str = ""


@dataclass(frozen=True)
class CellMark:
    """What is on one cell of a map: a terrain id and an object id, each "" for none.

    The optional fields customize one cell without a new terrain or object: a background
    colour, a terrain pattern, an icon for the object, and a short label shown in the tooltip."""

    terrain: str = ""
    object: str = ""
    color: str = ""
    pattern: str = ""
    icon: str = ""
    label: str = ""


def marks_from_list(items: Iterable[Mapping[str, Any]]) -> dict[tuple[int, int], CellMark]:
    """Turn event payload marks, a list of {"column", "row", "terrain", "object", ...}, into a dict."""
    return {
        (int(item["column"]), int(item["row"])): CellMark(
            terrain=str(item.get("terrain") or ""),
            object=str(item.get("object") or ""),
            color=str(item.get("color") or ""),
            pattern=str(item.get("pattern") or ""),
            icon=str(item.get("icon") or ""),
            label=str(item.get("label") or ""),
        )
        for item in items
    }


@dataclass(frozen=True)
class EncounterMap:
    """A grid of cells set in a location. Cells are as wide as the rule set's grid says; a rule set
    without one has no scale, and its cells only place participants relative to each other."""

    name: str
    location: str
    columns: int
    rows: int
    description: str
    # Terrain id under every cell without a mark; "" for none.
    background: str = ""
    # Marked cells, keyed by (column, row). A cell with no terrain and no object is not kept here.
    marks: dict[tuple[int, int], CellMark] = field(default_factory=dict)
    # Tokens placed outside an encounter, keyed by name without case. A local map keeps its
    # characters, NPCs, and props here until an encounter starts on it.
    tokens: dict[str, tuple[int, int]] = field(default_factory=dict)
    # The id of the map.created event, so the newest map at a place is known on replay.
    created: int = 0
    # The map's look: a palette and ambient from the rule set, and colour overrides for the
    # board's ground and grid. Each is "" for the board's default.
    palette: str = ""
    ambient: str = ""
    background_color: str = ""
    grid_color: str = ""


@dataclass
class Encounter:
    """An encounter in progress on a map. Participants take turns in the settled initiative order,
    or in the order they joined until it is settled."""

    map: str
    location: str
    # Display names of the participants, in turn order.
    order: list[str] = field(default_factory=list)
    # Cell of each participant, as (column, row) counted from the top left. Keys are name_key.
    cells: dict[str, tuple[int, int]] = field(default_factory=dict)
    # Index into `order` of the participant whose turn is next.
    up: int = 0
    round: int = 1
    # The participant whose turn is running, or None between turns.
    acting: str | None = None
    # Cells the acting participant has moved this turn.
    moved: int = 0
    # Attack actions the acting participant has used this turn. A statblock's multiattack is one
    # action, so its attacks per action all count here once.
    attacked: int = 0
    # The side each participant fights on, as given when they joined. Keys are name_key.
    sides: dict[str, str] = field(default_factory=dict)
    # Whether round one starts with each player character rolling initiative.
    rolls_initiative: bool = False
    # Whether the rule set rolls initiative for each side, so its members act as one block.
    side_initiative: bool = False
    # True once the round one order has been set from the initiative rolls.
    settled: bool = False
    # Initiative rolls that passed, by name_key. A player character missing here has not rolled.
    initiative: dict[str, bool] = field(default_factory=dict)
    # Morale checks each side has made, as tags: "first" at the first casualty, "half" at half.
    morale: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class Clock:
    """The campaign day, starting at 1, and the index of the part of the day within the rule set's
    clock."""

    day: int = 1
    part: int = 0
    hour: int = 0


@dataclass(frozen=True)
class RestRecord:
    """A rest the party took. `full` rests are uninterrupted rests that restore the party."""

    day: int
    part: str
    length: str
    full: bool
    interrupted: bool
    reason: str
    scope: str = "party"
    name: str = ""


@dataclass(frozen=True)
class MusicCue:
    """The GM's latest background-music choice. `scene` ties the cue to the encounter, place, and
    day-part it was chosen for, so a cue expires when the scene moves on."""

    mood: str
    source: str = "gm"
    scene: str = ""


@dataclass(frozen=True)
class WorldObject:
    """Something a place holds, such as currency, an item, or anything the GM describes. It is not
    an actor. A hidden object is neither listed nor takeable."""

    id: int
    place: str
    contents: str
    currency: int = 0
    item: str = ""
    # The item's own description, separate from the object's `contents`.
    item_text: str = ""
    item_size: str = ""
    item_slots: int = 0
    hidden: bool = False
    name: str = ""


@dataclass(frozen=True)
class DebugInjection:
    """A one-shot fault a debug panel setting forces into the next GM turn. `mode` is "tool" for
    a tool call run just before the narration, or "narration" for text that replaces it."""

    mode: str
    tool: str = ""
    arguments: str = "{}"
    text: str = ""


@dataclass
class CampaignState:
    """The campaign as its log says it is now. Keys of `places` and `characters` are the names
    without case, so places and characters share one namespace."""

    # The account that owns the campaign. A player character with no `player_id` belongs to
    # the owner, which keeps campaigns recorded before sharing worked exactly as they did.
    owner_id: int | None = None
    places: dict[str, Place] = field(default_factory=dict)
    characters: dict[str, Character] = field(default_factory=dict)
    # World objects by the id of the event that placed them.
    objects: dict[int, WorldObject] = field(default_factory=dict)
    notes: list[Note] = field(default_factory=list)
    # Facts about the world the GM invented, by topic, shown to the GM every turn.
    settings: dict[str, str] = field(default_factory=dict)
    # The player-facing overview of the world shown in the compendium. The GM writes it without
    # secrets; the full setting stays in `settings`.
    world_description: str = ""
    # What the player character knows of each met NPC, by name without case.
    known: dict[str, KnownNpc] = field(default_factory=dict)
    # The journal the player reads, oldest first.
    journal: str = ""
    # The event that began the last journal step. The next one writes from the log after it.
    journal_through: int = 0
    # Storylines by the id of the storyline.created event that began them.
    storylines: dict[int, Storyline] = field(default_factory=dict)
    # The storyline the GM is focused on. The first active one is focused when this is unset.
    focus_storyline_id: int | None = None
    # A one-shot fault the debug panel forces into the next GM turn, or None.
    debug_injection: DebugInjection | None = None
    # Options shared by the whole campaign, keyed by field id. Empty until they are chosen.
    options: dict[str, str] = field(default_factory=dict)
    # Long-running efforts by name without case, such as projects, research, and faction turns.
    projects: dict[str, Project] = field(default_factory=dict)
    summary: str = ""
    through: int = 0
    turn: Turn | None = None
    # Regions of the world map by name without case.
    regions: dict[str, Region] = field(default_factory=dict)
    # The world map's palette, ambient light, and custom colours.
    world_map_style: WorldMapStyle = field(default_factory=WorldMapStyle)
    # Encounter maps by name without case, and the encounter in progress, if any.
    maps: dict[str, EncounterMap] = field(default_factory=dict)
    encounter: Encounter | None = None
    clock: Clock = field(default_factory=Clock)
    # The GM's latest background-music cue, if any. A cue belongs to the scene it was chosen in.
    music: MusicCue | None = None
    rests: list[RestRecord] = field(default_factory=list)
    # Travel minutes not yet a whole hour on the clock. They carry over to the next move.
    travel_minutes: int = 0
    # The day of the last weather roll, and how many catastrophe weather results in a row came before it.
    weather_day: int | None = None
    weather_streak: int = 0
    start_season: str | None = None
    # The campaign's factions by stable id, such as the school's colleges.
    factions: dict[str, Faction] = field(default_factory=dict)
    # The campaign's tallies by tally id, then entry id, such as each House's points.
    tallies: dict[str, dict[str, int]] = field(default_factory=dict)
    # The award keys each tally has already paid out, so an award is never paid twice.
    tally_awards: dict[str, set[str]] = field(default_factory=dict)
    # The positive gains each tally entry has paid since its last reset, so a repeated gain is noticed.
    tally_gains: dict[str, dict[str, set[int]]] = field(default_factory=dict)

    def named(self, name: str) -> Place | Character | None:
        key = name_key(name)
        if key in self.places:
            return self.places[key]
        if key in self.characters:
            return self.characters[key]
        # A world's authored ids use hyphens, like "isthmus-town", while the place is named
        # "Isthmus Town". Fall back to a key that ignores case, punctuation, and separators so
        # either form finds the same object.
        loose = alias_key(name)
        for place in self.places.values():
            if alias_key(place.name) == loose:
                return place
        return next(
            (
                character
                for character in self.characters.values()
                if alias_key(character.name) == loose
            ),
            None,
        )

    def pcs(self) -> list[Character]:
        """Every player character in play, in the order they joined. Hirelings are not player
        characters even though the table may control them."""
        return [c for c in self.characters.values() if c.kind == PC_KIND and not c.hireling]

    def active_storylines(self) -> list[Storyline]:
        """The storylines still in play, oldest first."""
        return [storyline for storyline in self.storylines.values() if storyline.active]

    def resolved_storylines(self) -> list[Storyline]:
        """The storylines whose plots have concluded, oldest first."""
        return [storyline for storyline in self.storylines.values() if storyline.resolved]

    def focused_storyline(self) -> Storyline | None:
        """The storyline the GM is focused on: the chosen one while it is active, otherwise the
        oldest active one. None when no storyline is active."""
        focused = self.storylines.get(self.focus_storyline_id or -1)
        if focused is not None and focused.active:
            return focused
        active = self.active_storylines()
        return active[0] if active else None

    def player_character(self, player_id: int) -> Character | None:
        """The character this account plays: their living one if there is one, otherwise their
        out-of-play one so the page can offer a replacement or a hireling. Characters recorded
        before campaigns were shared belong to the owner."""
        owned = [c for c in self.pcs() if self.owner_of(c) == player_id]
        return next((c for c in owned if not c.out_of_play), owned[0] if owned else None)

    def players(self) -> list[Character]:
        """Every player character that still has a player, including unclaimed ones."""
        return self.pcs()

    def owner_of(self, character: Character) -> int | None:
        """The account that plays a character. A character without a recorded player belongs
        to the campaign owner; an unclaimed character has no owner at all."""
        if character.unclaimed:
            return None
        return character.player_id if character.player_id is not None else self.owner_id

    def owned_by(self, character: Character, player_id: int) -> bool:
        """Whether this account may act for the character. An unclaimed character is the whole
        table's, so any player may take its part."""
        if character.unclaimed:
            return True
        return self.owner_of(character) in (None, player_id)

    def pc(self) -> Character | None:
        """The campaign's primary player character: the owner's living one if there is one,
        otherwise the owner's dead one, and failing that the first player character. Anything
        that shows one character to the player should prefer `player_character` instead."""
        pcs = self.pcs()
        owned = [c for c in pcs if self.owner_of(c) == self.owner_id]
        candidates = owned or pcs
        return next((c for c in candidates if not c.out_of_play), next(iter(candidates), None))


def speaker_name(state: CampaignState, player_id: int | None = None) -> str | None:
    """Whose turn the player's next message speaks for: the acting PC or hireling in an encounter
    when it belongs to this player, else the player's own character. None when there is no
    character to speak for."""
    encounter = state.encounter
    if encounter is not None and encounter.acting is not None:
        acting = state.characters.get(name_key(encounter.acting))
        if (
            acting is not None
            and acting.kind == PC_KIND
            and (player_id is None or state.owned_by(acting, player_id))
        ):
            return acting.name
    pc = state.player_character(player_id) if player_id is not None else state.pc()
    return pc.name if pc is not None else None


def name_key(name: str) -> str:
    return name.strip().casefold()


def alias_key(name: str) -> str:
    """A loose name key for matching authored ids such as "isthmus-town" to "Isthmus Town"."""
    return re.sub(r"[^a-z0-9]+", "", name.casefold())


def _player_present(state: CampaignState) -> Character | None:
    """The player character, when they can take in their surroundings: alive and placed."""
    pc = state.pc()
    if pc is None or pc.out_of_play or pc.location is None:
        return None
    return pc


def _remember_npc(state: CampaignState, npc: Character) -> None:
    """Record what the player sees of an NPC at their shared location: how the NPC carries
    themselves, sounds, and behaves. The NPC's own description is a GM record that may hold
    secrets, so it is never copied here; only what the GM records with update_npc_knowledge is."""
    before = state.known.get(name_key(npc.name))
    # A summary the player was already told stays, and a hireling dismissed into the world keeps
    # the player-facing summary it had while it was in the company.
    summary = before.summary if before is not None else ""
    if not summary:
        summary = npc.summary
    state.known[name_key(npc.name)] = KnownNpc(
        name=npc.name,
        description=before.description if before is not None else "",
        summary=summary,
        personality=npc.personality,
        accent=npc.accent,
        quirks=tuple(npc.quirks),
        pronouns=npc.pronouns,
        relationship=before.relationship if before is not None else "",
        location=npc.location,
    )


def same_item_name(a: str, b: str) -> bool:
    """Item names match ignoring case and a plural s, so "potion" matches "Potions" on the list."""
    x, y = name_key(a), name_key(b)
    return x == y or x + "s" == y or y + "s" == x


PARTY_SIDE = "party"
FOES_SIDE = "foes"
MORALE_LABELS = {"first": "first casualty", "half": "half casualties"}


def side_key(encounter: Encounter, name: str) -> str:
    """The side a participant fights on, folded for comparisons."""
    return name_key(encounter.sides.get(name_key(name), ""))


def player_sides(encounter: Encounter, state: CampaignState) -> set[str]:
    """The side keys that hold a player character, falling back to the party side when no player
    character is in the encounter. A side is a group of allies, so a player character's custom side
    name still counts as the party for the rules that put the party first."""
    sides = {
        side_key(encounter, name)
        for name in encounter.order
        if state.characters[name_key(name)].kind == PC_KIND
    }
    return sides or {name_key(PARTY_SIDE)}


def group_by_side(
    encounter: Encounter,
    keep_turn: bool = True,
    party: set[str] | None = None,
    party_first: bool = True,
) -> None:
    """Keep the order grouped by side. With `party_first`, the sides that hold a player character
    come first, then the other sides in the order they first appear; without it, every side keeps
    the order it first appears in, so an initiative winner stays first. With `keep_turn`, the
    participant whose turn is next keeps it."""
    if not encounter.order:
        return
    next_name = (
        encounter.order[encounter.up] if keep_turn and encounter.up < len(encounter.order) else None
    )
    first: dict[str, int] = {}
    for index, name in enumerate(encounter.order):
        first.setdefault(side_key(encounter, name), index)
    party_sides = party if party is not None else {name_key(PARTY_SIDE)}

    def rank(name: str) -> tuple[int, int]:
        side = side_key(encounter, name)
        return (0 if party_first and side in party_sides else 1, first[side])

    encounter.order.sort(key=rank)
    if next_name is not None:
        encounter.up = encounter.order.index(next_name)


def _next_round(encounter: Encounter, state: CampaignState) -> None:
    encounter.up = 0
    encounter.round += 1
    if encounter.side_initiative:
        # A side-initiative rule set fixes the order of the sides for the whole fight; only the
        # members of each side are kept together.
        group_by_side(encounter, keep_turn=False, party_first=False)
    else:
        # Other rule sets begin every later round with the party, then its opponents.
        group_by_side(encounter, keep_turn=False, party=player_sides(encounter, state))


def _sits_out_first_round(encounter: Encounter, character: Character) -> bool:
    """Whether a player character failed the first round's initiative save. Such a character
    loses that round's turn; later rounds begin with the party as usual."""
    return (
        encounter.round == 1
        and character.kind == PC_KIND
        and encounter.initiative.get(name_key(character.name)) is False
    )


def _skip_defeated(encounter: Encounter, state: CampaignState) -> None:
    """Pass the turn over participants who are out of the fight, and over a player character who
    lost the first round's initiative save. It stays put when everyone is out."""
    if all(out_of_fight(state.characters[name_key(name)]) for name in encounter.order):
        return
    while True:
        character = state.characters[name_key(encounter.order[encounter.up])]
        if not (out_of_fight(character) or _sits_out_first_round(encounter, character)):
            return
        encounter.up += 1
        if encounter.up >= len(encounter.order):
            _next_round(encounter, state)


def token_label(name: str) -> str:
    """A short label for a map token: the initials of a multi-word name, or the first three letters
    of a single word. The full name is the token's tooltip."""
    words = name.split()
    if len(words) > 1:
        return "".join(word[0] for word in words[:3]).upper()
    return name.strip()[:3]


def token_labels(names: Iterable[str]) -> dict[str, str]:
    """Map each name to a token label that no other name in the set shares. A label that clashes is
    lengthened, one letter at a time, until it is unique or the name has no letters left to add."""
    names = list(names)
    compact = {name: "".join(ch for ch in name if ch.isalnum()) for name in names}
    labels = {name: token_label(name) for name in names}
    widths = {name: len(labels[name]) for name in names}
    while True:
        owners: dict[str, list[str]] = {}
        for name, label in labels.items():
            owners.setdefault(label, []).append(name)
        clashing = [name for group in owners.values() if len(group) > 1 for name in group]
        grown = [name for name in clashing if widths[name] < len(compact[name])]
        if not grown:
            return labels
        for name in grown:
            widths[name] += 1
            labels[name] = compact[name][: widths[name]]
