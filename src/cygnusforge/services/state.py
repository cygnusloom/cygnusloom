"""The campaign state a rule set and the engine read: characters, places, maps, and turns.

`load_state` in `cygnusforge.services.events` folds the event log into these dataclasses. They are
plain state models; the event classes and persistence live in `events`."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cygnusforge.llm.base import ToolRequest
from cygnusforge.services.tools import Ask

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
    icon: str | None = None
    # What the player sees of a discovered place. The full description stays GM-only.
    summary: str | None = None
    # The ids of rules attached to the place, whose text reaches the GM while the player is here.
    rules: tuple[str, ...] = ()
    # Authored overland routes from this place, by destination place name key. A route gives the
    # path, distance, terrain, and vast of an overland move that leads there.
    routes: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class Region:
    """An area of one kind of ground on the world map. Its points are outline corners in
    kilometers from the map's center, in order around the edge. It is shown on the player's map
    once it is discovered, or once a discovered place lies inside it."""

    name: str
    terrain: str
    points: tuple[tuple[float, float], ...]
    discovered: bool = False


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
class Character:
    kind: str
    name: str
    location: str | None
    description: str
    personality: str
    accent: str
    quirks: list[str]
    # How the GM refers to the character, such as "she/her". None if it was never recorded.
    pronouns: str | None = None
    # The character options, resolved when the character was created. See content/character.py.
    stats: dict[str, int] = field(default_factory=dict)
    templates: dict[str, str] = field(default_factory=dict)
    features: dict[str, int] = field(default_factory=dict)
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
    # The character is dead. The body stays where the character fell, with its gear.
    dead: bool = False
    # The character has left the story, and the player plays someone else. It can come back later.
    retired: bool = False

    @property
    def out_of_play(self) -> bool:
        """Whether the player can no longer act as this character: dead, or left the story."""
        return self.dead or self.retired


def current(character: Character, stat: str) -> int:
    """A stat's score less the harm it has taken, never below 0."""
    return max(0, character.stats.get(stat, 0) - character.harm.get(stat, 0))


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
    abandoned: bool = False


@dataclass(frozen=True)
class Note:
    # The id of the note.added event that wrote it, so it stays the same across replays.
    id: int
    subject: str
    text: str


@dataclass(frozen=True)
class KnownNpc:
    """What the player character knows of an NPC: the details the player has seen or been told,
    how the NPC stands with the player, and where the player last knew the NPC to be. The GM's
    full record of the NPC may hold more than this."""

    name: str
    description: str = ""
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
    # Why the last step stopped, when it stopped with an error and has not been retried yet.
    failed: str | None = None
    # The NPC whose encounter turn this reply plays out, or None for a reply to the player.
    actor: str | None = None


@dataclass(frozen=True)
class CellMark:
    """What is on one cell of a map: a terrain id and an object id, each "" for none."""

    terrain: str = ""
    object: str = ""


def marks_from_list(items: Iterable[Mapping[str, Any]]) -> dict[tuple[int, int], CellMark]:
    """Turn event payload marks, a list of {"column", "row", "terrain", "object"}, into a dict."""
    return {
        (int(item["column"]), int(item["row"])): CellMark(
            str(item.get("terrain", "")), str(item.get("object", ""))
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


@dataclass
class Encounter:
    """An encounter in progress on a map. Participants take turns in the order they joined."""

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
    # The side each participant fights on, as given when they joined. Keys are name_key.
    sides: dict[str, str] = field(default_factory=dict)
    # Whether round one starts with each player character rolling initiative.
    rolls_initiative: bool = False
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


@dataclass(frozen=True)
class WorldObject:
    """Something a place holds, such as currency, an item, or anything the GM describes. It is not
    an actor. A hidden object is neither listed nor takeable."""

    id: int
    place: str
    contents: str
    currency: int = 0
    item: str = ""
    item_size: str = ""
    item_slots: int = 0
    hidden: bool = False
    name: str = ""


@dataclass
class CampaignState:
    """The campaign as its log says it is now. Keys of `places` and `characters` are the names
    without case, so places and characters share one namespace."""

    places: dict[str, Place] = field(default_factory=dict)
    characters: dict[str, Character] = field(default_factory=dict)
    # World objects by the id of the event that placed them.
    objects: dict[int, WorldObject] = field(default_factory=dict)
    notes: list[Note] = field(default_factory=list)
    # What the player character knows of each met NPC, by name without case.
    known: dict[str, KnownNpc] = field(default_factory=dict)
    # The journal the player reads, oldest first.
    journal: str = ""
    # The event that began the last journal step. The next one writes from the log after it.
    journal_through: int = 0
    # Options shared by the whole campaign, keyed by field id. Empty until they are chosen.
    options: dict[str, str] = field(default_factory=dict)
    # Long-running efforts by name without case, such as projects, research, and faction turns.
    projects: dict[str, Project] = field(default_factory=dict)
    summary: str = ""
    through: int = 0
    turn: Turn | None = None
    # Regions of the world map by name without case.
    regions: dict[str, Region] = field(default_factory=dict)
    # Encounter maps by name without case, and the encounter in progress, if any.
    maps: dict[str, EncounterMap] = field(default_factory=dict)
    encounter: Encounter | None = None
    clock: Clock = field(default_factory=Clock)
    rests: list[RestRecord] = field(default_factory=list)
    # Travel minutes not yet a whole hour on the clock. They carry over to the next move.
    travel_minutes: int = 0
    # The day of the last weather roll, and how many catastrophe weather results in a row came before it.
    weather_day: int | None = None
    weather_streak: int = 0
    start_season: str | None = None
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
        return self.characters.get(key)

    def pc(self) -> Character | None:
        """The player's character: the living one if there is one, otherwise the dead one."""
        pcs = [c for c in self.characters.values() if c.kind == PC_KIND and not c.hireling]
        return next((c for c in pcs if not c.out_of_play), next(iter(pcs), None))


def speaker_name(state: CampaignState) -> str | None:
    """Whose turn the player's next message speaks for: the acting PC or hireling in an encounter,
    else the player character. None when there is no player character."""
    encounter = state.encounter
    if encounter is not None and encounter.acting is not None:
        acting = state.characters.get(name_key(encounter.acting))
        if acting is not None and acting.kind == PC_KIND:
            return acting.name
    pc = state.pc()
    return pc.name if pc is not None else None


def name_key(name: str) -> str:
    return name.strip().casefold()


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
    state.known[name_key(npc.name)] = KnownNpc(
        name=npc.name,
        description=before.description if before is not None else "",
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


def _side_key(encounter: Encounter, name: str) -> str:
    return name_key(encounter.sides.get(name_key(name), ""))


def group_by_side(encounter: Encounter, keep_turn: bool = True) -> None:
    """Keep the order grouped by side: the party first, then the other sides in the order they
    first appear. With `keep_turn`, the participant whose turn is next keeps it."""
    if not encounter.order:
        return
    next_name = encounter.order[encounter.up] if keep_turn else None
    first: dict[str, int] = {}
    for index, name in enumerate(encounter.order):
        first.setdefault(_side_key(encounter, name), index)

    def rank(name: str) -> tuple[int, int]:
        side = _side_key(encounter, name)
        return (0 if side == name_key(PARTY_SIDE) else 1, first[side])

    encounter.order.sort(key=rank)
    if next_name is not None:
        encounter.up = encounter.order.index(next_name)


def _next_round(encounter: Encounter) -> None:
    encounter.up = 0
    encounter.round += 1
    group_by_side(encounter, keep_turn=False)


def _skip_defeated(encounter: Encounter, state: CampaignState) -> None:
    """Pass the turn over participants who are out of the fight. It stays put when everyone is."""
    if all(out_of_fight(state.characters[name_key(name)]) for name in encounter.order):
        return
    while out_of_fight(state.characters[name_key(encounter.order[encounter.up])]):
        encounter.up += 1
        if encounter.up >= len(encounter.order):
            _next_round(encounter)


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


