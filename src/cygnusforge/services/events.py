"""A campaign's event log: the only stored state of a campaign.

Every change to the world, the chat, and the GM's turn is an event appended with `record`. There
are no tables of current state. `load_state` folds the log into a `CampaignState`, the same way
each time: a rollback removes events, so the next fold no longer sees them.

To add an event, subclass `GameEvent` as a frozen dataclass with a unique `kind`, a `channel`, an
`apply` that changes the state, and a `render` if it appears in the Log. The fields are the JSON
payload. Store resolved values (names, rolled traits, the text the model was given) rather than
the inputs used to compute them, so replaying the log gives the same result. Refer to places and
characters by name, which is unique within a campaign and never changes."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, fields, replace
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import delete, func
from sqlmodel import Session, select

from cygnusforge.db.models import Event
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


@dataclass(frozen=True)
class Item:
    """Something a character carries. `size` is the rule set's item size, empty for Fatigue, and
    `slots` is what it takes from the inventory. `kind` marks special items such as Fatigue."""

    name: str
    size: str
    slots: int
    kind: str = ""
    # A spellbook's spell text, or a relic's recharge rule.
    text: str = ""
    # A relic's uses left, and the most it holds.
    uses: int = 0
    max_uses: int = 0
    # The clock when a relic was last used. It cannot recharge until the clock moves on.
    spent_on: Clock | None = None


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
    # The character options, resolved when the character was created. See content/character.py.
    stats: dict[str, int] = field(default_factory=dict)
    templates: dict[str, str] = field(default_factory=dict)
    features: dict[str, int] = field(default_factory=dict)
    fields: dict[str, str] = field(default_factory=dict)
    # What each hit absorbs. Monsters have it in their statblock; player characters get it from gear.
    armor: int = 0
    # A monster's attacks, by name, as dice formulas such as "1d8+1d8". Empty for player characters.
    attacks: dict[str, str] = field(default_factory=dict)
    # The name of the bestiary statblock a monster was built from, such as "Wolf". Empty if none.
    creature: str = ""
    # Harm taken so far, by stat id. A stat's current value is its score less its harm, at least 0.
    harm: dict[str, int] = field(default_factory=dict)
    # The stat id of a save the character must make after harm overflowed, or None.
    save_due: str | None = None
    defeated: bool = False
    # The ids of the conditions the character has, and the names of their scars, in the order gained.
    conditions: list[str] = field(default_factory=list)
    scars: list[str] = field(default_factory=list)
    # Ran from the fight, or broke and fled. Cleared when the encounter ends.
    fled: bool = False
    # Gave up the fight and laid down arms. Cleared when the encounter ends.
    surrendered: bool = False
    # A debug buff: no harm reaches the character. Set from the debug panel.
    invincible: bool = False
    # A detachment is a large group the Warden fights as one unit. See SPEC.md, Phase 2.
    detachment: bool = False
    # A detachment whose Critical Damage save failed. The Warden must decide it is defeated or flees.
    critical_due: bool = False
    # What the character carries. Fatigue is an item of the rule set's fatigue kind.
    items: list[Item] = field(default_factory=list)
    # The campaign day a deprivation began, or None while the character is not deprived.
    deprived_since: int | None = None
    deprivation_cause: str = ""


def current(character: Character, stat: str) -> int:
    """A stat's score less the harm it has taken, never below 0."""
    return max(0, character.stats.get(stat, 0) - character.harm.get(stat, 0))


def defeat_status(character: Character, defeat: Mapping[str, str]) -> str:
    """Where a defeat leaves a character, in the rule set's words: `defeat` maps `player_character`
    and `npc` to a label. A character with no label in `defeat` reads as just defeated."""
    key = "player_character" if character.kind == PC_KIND else "npc"
    return defeat.get(key, "defeated")


def out_of_fight(character: Character) -> bool:
    """Whether a character can no longer act in the encounter: defeated, fled, or surrendered."""
    return character.defeated or character.fled or character.surrendered


@dataclass(frozen=True)
class Note:
    # The id of the note.added event that wrote it, so it stays the same across replays.
    id: int
    subject: str
    text: str


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
class EncounterMap:
    """A grid of cells set in a location. Cells are as wide as the rule set's grid says; a rule set
    without one has no scale, and its cells only place participants relative to each other."""

    name: str
    location: str
    columns: int
    rows: int
    description: str


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


@dataclass(frozen=True)
class RestRecord:
    """A rest the party took. `full` rests are uninterrupted rests that restore the party."""

    day: int
    part: str
    length: str
    full: bool
    interrupted: bool
    reason: str


@dataclass
class CampaignState:
    """The campaign as its log says it is now. Keys of `places` and `characters` are the names
    without case, so places and characters share one namespace."""

    places: dict[str, Place] = field(default_factory=dict)
    characters: dict[str, Character] = field(default_factory=dict)
    notes: list[Note] = field(default_factory=list)
    # Options shared by the whole campaign, keyed by field id. Empty until they are chosen.
    options: dict[str, str] = field(default_factory=dict)
    summary: str = ""
    through: int = 0
    turn: Turn | None = None
    # Encounter maps by name without case, and the encounter in progress, if any.
    maps: dict[str, EncounterMap] = field(default_factory=dict)
    encounter: Encounter | None = None
    clock: Clock = field(default_factory=Clock)
    rests: list[RestRecord] = field(default_factory=list)

    def named(self, name: str) -> Place | Character | None:
        key = name_key(name)
        if key in self.places:
            return self.places[key]
        return self.characters.get(key)

    def pc(self) -> Character | None:
        return next((c for c in self.characters.values() if c.kind == PC_KIND), None)


def name_key(name: str) -> str:
    return name.strip().casefold()


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


EVENT_TYPES: dict[str, type[GameEvent]] = {}


class GameEvent:
    kind: ClassVar[str]
    channel: ClassVar[Channel] = Channel.LOG

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        kind = cls.__dict__.get("kind")
        if not kind:
            raise TypeError(f"{cls.__name__} must set a kind")
        if kind in EVENT_TYPES:
            raise TypeError(f"event kind {kind!r} is already registered")
        EVENT_TYPES[kind] = cls

    def apply(self, state: CampaignState, event_id: int) -> None:
        """Change the state this event affects. Most events affect none."""

    def render(self) -> str | None:
        """The Log text, or None when the Log does not show this event."""
        raise NotImplementedError(f"{type(self).__name__} has no Log text")


def record(db: Session, campaign_id: int, event: GameEvent) -> int:
    """Append an event to the campaign's log and return its id."""
    newest = db.exec(select(func.max(Event.seq)).where(Event.campaign_id == campaign_id)).one()
    row = Event(
        campaign_id=campaign_id,
        seq=(newest or 0) + 1,
        channel=event.channel.value,
        kind=event.kind,
        payload=json.dumps(asdict(event)),
    )
    db.add(row)
    db.flush()
    if row.id is None:
        raise RuntimeError("the event was not assigned an id")
    return row.id


def decode(row: Event) -> GameEvent:
    """The event a row stores. Payload keys the event no longer has are ignored."""
    cls = EVENT_TYPES[row.kind]
    known = {f.name for f in fields(cls)}
    payload = json.loads(row.payload)
    return cls(**{key: value for key, value in payload.items() if key in known})


def load_state(db: Session, campaign_id: int, before: int | None = None) -> CampaignState:
    """Fold the campaign's log into its state. With `before`, only the events before that id."""
    statement = select(Event).where(Event.campaign_id == campaign_id)
    if before is not None:
        statement = statement.where(Event.id < before)
    state = CampaignState()
    for row in db.exec(statement.order_by(Event.seq)):
        event = decode(row)
        event.apply(state, row.id or 0)
        if state.turn is not None and not isinstance(event, TurnFailed):
            state.turn.failed = None
    return state


def events_since(
    db: Session, campaign_id: int, after: int = 0, channel: Channel | None = None
) -> list[Event]:
    statement = select(Event).where(Event.campaign_id == campaign_id, Event.id > after)
    if channel is not None:
        statement = statement.where(Event.channel == channel.value)
    return list(db.exec(statement.order_by(Event.seq)))


def last_event_id(db: Session, campaign_id: int) -> int:
    """The id of the campaign's newest event, or 0 when it has none."""
    newest = db.exec(select(func.max(Event.id)).where(Event.campaign_id == campaign_id)).one()
    return newest or 0


def truncate(db: Session, campaign_id: int, after: int) -> None:
    """Remove the campaign's events after `after`."""
    db.exec(delete(Event).where(Event.campaign_id == campaign_id, Event.id > after))


def _open_turn(state: CampaignState) -> Turn:
    if state.turn is None:
        raise RuntimeError("the log has a turn event with no turn open")
    return state.turn


def _parts(items: list[dict[str, str]]) -> list[ContextPart]:
    return [ContextPart(**item) for item in items]


@dataclass(frozen=True)
class LocationAdded(GameEvent):
    kind: ClassVar[str] = "location.added"

    name: str
    description: str
    discovered: bool = False
    x: float | None = None
    y: float | None = None
    parent: str | None = None

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.places[name_key(self.name)] = Place(
            self.name, self.description, self.discovered, _position(self.x, self.y), self.parent
        )

    def render(self) -> str:
        return f"New location: {self.name}."


@dataclass(frozen=True)
class PcAdded(GameEvent):
    kind: ClassVar[str] = "pc.added"

    name: str
    location: str | None = None
    stats: dict[str, int] = field(default_factory=dict)
    templates: dict[str, str] = field(default_factory=dict)
    features: dict[str, int] = field(default_factory=dict)
    fields: dict[str, str] = field(default_factory=dict)

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.characters[name_key(self.name)] = Character(
            kind=PC_KIND,
            name=self.name,
            location=self.location,
            description="",
            personality="",
            accent="",
            quirks=[],
            stats=dict(self.stats),
            templates=dict(self.templates),
            features=dict(self.features),
            fields=dict(self.fields),
        )

    def render(self) -> str:
        if self.location is None:
            return f"{self.name} is ready to begin, not yet placed at a location."
        return f"{self.name} begins at {self.location}."


@dataclass(frozen=True)
class CampaignOptionsChosen(GameEvent):
    """The options every character in the campaign shares. Chosen once, before the player
    character, and given to the GM through the state text rather than the chat."""

    kind: ClassVar[str] = "campaign.options"
    channel: ClassVar[Channel] = Channel.INTERNAL

    options: dict[str, str]

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.options = dict(self.options)


@dataclass(frozen=True)
class NpcAdded(GameEvent):
    kind: ClassVar[str] = "npc.added"

    name: str
    location: str
    description: str
    personality: str
    accent: str
    quirks: list[str]
    stats: dict[str, int] = field(default_factory=dict)
    templates: dict[str, str] = field(default_factory=dict)
    features: dict[str, int] = field(default_factory=dict)
    fields: dict[str, str] = field(default_factory=dict)
    armor: int = 0
    attacks: dict[str, str] = field(default_factory=dict)
    creature: str = ""
    detachment: bool = False

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.characters[name_key(self.name)] = Character(
            kind=NPC_KIND,
            name=self.name,
            location=self.location,
            description=self.description,
            personality=self.personality,
            accent=self.accent,
            quirks=list(self.quirks),
            stats=dict(self.stats),
            templates=dict(self.templates),
            features=dict(self.features),
            fields=dict(self.fields),
            armor=self.armor,
            attacks=dict(self.attacks),
            creature=self.creature,
            detachment=self.detachment,
        )

    def render(self) -> str:
        return f"New character: {self.name}, at {self.location}."


@dataclass(frozen=True)
class ActorMoved(GameEvent):
    kind: ClassVar[str] = "actor.moved"

    name: str
    location: str | None

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.characters[name_key(self.name)].location = self.location
        place = state.places.get(name_key(self.location)) if self.location else None
        if place is not None:
            state.places[name_key(place.name)] = replace(place, visited_day=state.clock.day)

    def render(self) -> str:
        if self.location is None:
            return f"{self.name} went into the wind."
        return f"{self.name} moved to {self.location}."


@dataclass(frozen=True)
class NoteAdded(GameEvent):
    kind: ClassVar[str] = "note.added"

    text: str
    # The name of the place or character the note is about, or None for the whole campaign.
    subject: str | None = None

    def apply(self, state: CampaignState, event_id: int) -> None:
        subject = CAMPAIGN_WIDE if self.subject is None else self.subject
        state.notes.append(Note(id=event_id, subject=subject, text=self.text))

    def render(self) -> str:
        if self.subject is None:
            return f"Note: {self.text}"
        return f"Note about {self.subject}: {self.text}"


@dataclass(frozen=True)
class LocationUpdated(GameEvent):
    """A location's details after an update. The position is the resolved one, and None if the
    place has none. Discovery is not changed here; `LocationDiscovered` does that."""

    kind: ClassVar[str] = "location.updated"

    name: str
    description: str
    x: float | None = None
    y: float | None = None

    def apply(self, state: CampaignState, event_id: int) -> None:
        key = name_key(self.name)
        before = state.places[key]
        state.places[key] = replace(
            before,
            name=self.name,
            description=self.description,
            position=_position(self.x, self.y),
        )

    def render(self) -> str:
        return f"Location updated: {self.name}."


@dataclass(frozen=True)
class LocationDiscovered(GameEvent):
    kind: ClassVar[str] = "location.discovered"

    name: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        key = name_key(self.name)
        state.places[key] = replace(state.places[key], discovered=True)

    def render(self) -> str:
        return f"Discovered: {self.name}."


@dataclass(frozen=True)
class LocationRemoved(GameEvent):
    kind: ClassVar[str] = "location.removed"

    name: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        del state.places[name_key(self.name)]

    def render(self) -> str:
        return f"Location removed: {self.name}."


@dataclass(frozen=True)
class NpcUpdated(GameEvent):
    """An NPC's details after an update. The values are the resolved ones, not the changes."""

    kind: ClassVar[str] = "npc.updated"

    name: str
    description: str
    personality: str
    accent: str
    quirks: list[str]

    def apply(self, state: CampaignState, event_id: int) -> None:
        npc = state.characters[name_key(self.name)]
        npc.description = self.description
        npc.personality = self.personality
        npc.accent = self.accent
        npc.quirks = list(self.quirks)

    def render(self) -> str:
        return f"Character updated: {self.name}."


@dataclass(frozen=True)
class NpcRemoved(GameEvent):
    kind: ClassVar[str] = "npc.removed"

    name: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        del state.characters[name_key(self.name)]

    def render(self) -> str:
        return f"Character removed: {self.name}."


@dataclass(frozen=True)
class NoteUpdated(GameEvent):
    kind: ClassVar[str] = "note.updated"

    note_id: int
    text: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.notes = [
            Note(id=note.id, subject=note.subject, text=self.text)
            if note.id == self.note_id
            else note
            for note in state.notes
        ]

    def render(self) -> str:
        return f"Note updated: {self.text}"


@dataclass(frozen=True)
class NoteRemoved(GameEvent):
    kind: ClassVar[str] = "note.removed"

    note_id: int
    # Kept for the Log, which shows the note that was removed.
    text: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.notes = [note for note in state.notes if note.id != self.note_id]

    def render(self) -> str:
        return f"Note removed: {self.text}"


@dataclass(frozen=True)
class ToolRan(GameEvent):
    """A rule-set tool finished during a turn. Its messages and context are what the model is
    sent next. `activity` is the Log text for the tool, if it has any."""

    kind: ClassVar[str] = "tool.ran"

    request_id: str
    messages: list[dict[str, Any]]
    context: list[dict[str, str]]
    activity: str | None = None

    def apply(self, state: CampaignState, event_id: int) -> None:
        turn = _open_turn(state)
        request = turn.queue.pop(0)
        if request.id != self.request_id:
            raise RuntimeError(f"tool {self.request_id!r} ran out of order")
        turn.messages.extend(self.messages)
        turn.context.extend(_parts(self.context))
        turn.prompt = None

    def render(self) -> str | None:
        return self.activity


@dataclass(frozen=True)
class SpeechWritten(GameEvent):
    """An NPC's line, written by a speech step during a tool run. The narration reads the lines
    in order. An empty `dialogue` is kept in the context only. A `replace` line takes the place of
    the speaker's queued line, if there is one."""

    kind: ClassVar[str] = "speech.written"
    channel: ClassVar[Channel] = Channel.INTERNAL

    speaker: str
    dialogue: str
    replace: bool
    context: list[dict[str, str]]

    def apply(self, state: CampaignState, event_id: int) -> None:
        turn = _open_turn(state)
        turn.context.extend(_parts(self.context))
        if not self.dialogue:
            return
        line = {"speaker": self.speaker, "dialogue": self.dialogue}
        index = next(
            (
                i
                for i, queued in enumerate(turn.speech)
                if queued["speaker"].casefold() == self.speaker.casefold()
            ),
            None,
        )
        if self.replace and index is not None:
            turn.speech[index] = line
        else:
            turn.speech.append(line)


@dataclass(frozen=True)
class ChatPlayer(GameEvent):
    """The player's message. A message sent while no turn is open starts one; a message sent while
    a turn is open answers its prompt."""

    kind: ClassVar[str] = "chat.player"
    channel: ClassVar[Channel] = Channel.TRANSCRIPT

    content: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        if state.turn is None:
            state.turn = Turn(opener=event_id, text=self.content)

    def render(self) -> str:
        return self.content


@dataclass(frozen=True)
class OpeningStarted(GameEvent):
    """The campaign's opening: the GM's first turn, which begins before the player acts. It is
    recorded with the player character, so the campaign is never left without its opening."""

    kind: ClassVar[str] = "turn.opening"
    channel: ClassVar[Channel] = Channel.INTERNAL

    def apply(self, state: CampaignState, event_id: int) -> None:
        if state.turn is None:
            state.turn = Turn(opener=event_id, text="", opening=True)


@dataclass(frozen=True)
class ChatGM(GameEvent):
    """The GM's reply. The final reply closes the turn, and the turn's context is what this reply
    was written from. A reply that is not final is an out-of-character reply that the story
    continues after."""

    kind: ClassVar[str] = "chat.gm"
    channel: ClassVar[Channel] = Channel.TRANSCRIPT

    content: str
    model: str
    final: bool = True

    def apply(self, state: CampaignState, event_id: int) -> None:
        if self.final:
            state.turn = None
        else:
            _open_turn(state).ooc = False

    def render(self) -> str:
        return self.content


@dataclass(frozen=True)
class TurnFailed(GameEvent):
    """A turn step stopped with an error. The turn stays open so it can be continued."""

    kind: ClassVar[str] = "turn.failed"

    error: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        if state.turn is not None:
            state.turn.failed = self.error

    def render(self) -> str:
        return f"The GM stopped: {self.error}."


@dataclass(frozen=True)
class SummaryUpdated(GameEvent):
    """The chat up to and including event `through` is now represented by `summary`."""

    kind: ClassVar[str] = "summary.updated"
    channel: ClassVar[Channel] = Channel.INTERNAL

    summary: str
    through: int

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.summary = self.summary
        state.through = self.through


@dataclass(frozen=True)
class TurnBegun(GameEvent):
    """The model's context for the open turn, once it is set up."""

    kind: ClassVar[str] = "turn.begun"
    channel: ClassVar[Channel] = Channel.INTERNAL

    head: list[dict[str, str]]
    context: list[dict[str, str]]
    messages: list[dict[str, Any]]
    round_start: int

    def apply(self, state: CampaignState, event_id: int) -> None:
        turn = _open_turn(state)
        turn.begun = True
        turn.head = _parts(self.head)
        turn.context = _parts(self.context)
        turn.messages = list(self.messages)
        turn.round_start = self.round_start


@dataclass(frozen=True)
class StateStepMade(GameEvent):
    """One state-management step of the open turn. With `requests`, they are queued to run.
    Without, the turn moves on to the narration, and any NPC dialogue is passed to it."""

    kind: ClassVar[str] = "turn.state_step"
    channel: ClassVar[Channel] = Channel.INTERNAL

    requests: list[dict[str, str]]
    messages: list[dict[str, Any]]
    context: list[dict[str, str]]

    def apply(self, state: CampaignState, event_id: int) -> None:
        turn = _open_turn(state)
        turn.rounds += 1
        turn.messages.extend(self.messages)
        turn.context.extend(_parts(self.context))
        turn.queue = [ToolRequest(**request) for request in self.requests]
        if not self.requests:
            turn.narrating = True
            turn.speech = []


@dataclass(frozen=True)
class OocStarted(GameEvent):
    """The open turn's player message is out of character. The out-of-character step replies
    next, and the story is narrated after that reply only when `narrate` is set."""

    kind: ClassVar[str] = "turn.ooc"
    channel: ClassVar[Channel] = Channel.INTERNAL

    narrate: bool

    def apply(self, state: CampaignState, event_id: int) -> None:
        turn = _open_turn(state)
        turn.ooc = True
        turn.ooc_narrate = self.narrate


@dataclass(frozen=True)
class PromptShown(GameEvent):
    """The open turn is waiting for the player to roll or type a reply."""

    kind: ClassVar[str] = "turn.prompted"
    channel: ClassVar[Channel] = Channel.INTERNAL

    text: str
    dice: str
    details: str | None = None

    def apply(self, state: CampaignState, event_id: int) -> None:
        _open_turn(state).prompt = Ask(self.text, self.dice, self.details)


@dataclass(frozen=True)
class MapCreated(GameEvent):
    kind: ClassVar[str] = "map.created"

    name: str
    location: str
    columns: int
    rows: int
    description: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.maps[name_key(self.name)] = EncounterMap(
            self.name, self.location, self.columns, self.rows, self.description
        )

    def render(self) -> str:
        return (
            f"New encounter map: {self.name}, at {self.location} "
            f"({self.columns} by {self.rows} cells)."
        )


@dataclass(frozen=True)
class MapUpdated(GameEvent):
    """A map's size, description, name, or location changes. An empty `new_name` or `location`
    keeps the current one. An encounter running on the map follows it."""

    kind: ClassVar[str] = "map.updated"

    name: str
    columns: int
    rows: int
    description: str
    new_name: str = ""
    location: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        key = name_key(self.name)
        before = state.maps.pop(key)
        name = self.new_name or before.name
        location = self.location or before.location
        state.maps[name_key(name)] = EncounterMap(
            name, location, self.columns, self.rows, self.description
        )
        encounter = state.encounter
        if encounter is not None and name_key(encounter.map) == key:
            encounter.map = name
            encounter.location = location

    def render(self) -> str:
        if self.new_name:
            return f"Encounter map updated: {self.name} is now {self.new_name}."
        return f"Encounter map updated: {self.name}."


@dataclass(frozen=True)
class MapDeleted(GameEvent):
    kind: ClassVar[str] = "map.deleted"

    name: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.maps.pop(name_key(self.name), None)

    def render(self) -> str:
        return f"Encounter map deleted: {self.name}."


@dataclass(frozen=True)
class EncounterStarted(GameEvent):
    kind: ClassVar[str] = "encounter.started"

    map: str
    location: str
    rolls_initiative: bool = False

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.encounter = Encounter(
            map=self.map, location=self.location, rolls_initiative=self.rolls_initiative
        )

    def render(self) -> str:
        return f"Encounter begins at {self.map}, {self.location}."


@dataclass(frozen=True)
class EncounterEnded(GameEvent):
    """The encounter is over. `reason` says why it ended early, such as a side being down."""

    kind: ClassVar[str] = "encounter.ended"

    reason: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        for character in state.characters.values():
            character.fled = False
            character.surrendered = False
            character.critical_due = False
        state.encounter = None

    def render(self) -> str:
        if self.reason:
            return f"{self.reason} The encounter is over."
        return "The encounter is over."


@dataclass(frozen=True)
class MoraleChecked(GameEvent):
    """A side's WIL save for morale, made by its leader. A failed save makes the side flee.
    `tag` is "first" for the first casualty or "half" for half the side."""

    kind: ClassVar[str] = "morale.checked"

    side: str
    tag: str
    leader: str
    stat: str
    roll: int
    score: int
    passed: bool

    def apply(self, state: CampaignState, event_id: int) -> None:
        if state.encounter is not None:
            state.encounter.morale.setdefault(self.side, []).append(self.tag)

    def render(self) -> str:
        label = MORALE_LABELS.get(self.tag, self.tag)
        result = "holds" if self.passed else "breaks"
        return (
            f"The {self.side} side checks morale at the {label}: {self.leader} rolls {self.roll} "
            f"on a {self.stat.upper()} save of {self.score} and the side {result}."
        )


@dataclass(frozen=True)
class CharacterFled(GameEvent):
    """A character has left the fight. `reason` is "morale" or "retreat"."""

    kind: ClassVar[str] = "character.fled"

    name: str
    reason: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.characters[name_key(self.name)].fled = True

    def render(self) -> str:
        if self.reason == "morale":
            return f"{self.name} breaks and flees."
        return f"{self.name} gets clear of the fight."


@dataclass(frozen=True)
class CharacterSurrendered(GameEvent):
    """A character has given up the fight and laid down arms."""

    kind: ClassVar[str] = "character.surrendered"

    name: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.characters[name_key(self.name)].surrendered = True

    def render(self) -> str:
        return f"{self.name} surrenders."


@dataclass(frozen=True)
class RetreatRolled(GameEvent):
    """A DEX save to run from the fight. Unlike SaveRolled, a failure does not defeat anyone; the
    character just stays in the fight."""

    kind: ClassVar[str] = "retreat.rolled"

    name: str
    stat: str
    roll: int
    score: int
    passed: bool

    def render(self) -> str:
        if self.passed:
            outcome = "and gets clear."
        else:
            outcome = "and cannot get clear."
        return (
            f"{self.name} rolls {self.roll} on a {self.stat.upper()} save of {self.score} {outcome}"
        )


@dataclass(frozen=True)
class CharacterRecovered(GameEvent):
    """A player character brought back from critical damage. The GM says how they survived and
    what it cost them."""

    kind: ClassVar[str] = "character.recovered"

    name: str
    explanation: str
    consequence: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.characters[name_key(self.name)].defeated = False

    def render(self) -> str:
        return f"{self.name} comes to. {self.explanation} {self.consequence}"


@dataclass(frozen=True)
class ParticipantJoined(GameEvent):
    kind: ClassVar[str] = "encounter.joined"

    name: str
    side: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        encounter = state.encounter
        if encounter is None:
            return
        encounter.order.append(state.characters[name_key(self.name)].name)
        encounter.sides[name_key(self.name)] = self.side
        # In round one the order is set by initiative, so a late joiner waits at the end of it.
        if encounter.round > 1:
            group_by_side(encounter)

    def render(self) -> str:
        return f"{self.name} joins the {self.side} side of the encounter."


@dataclass(frozen=True)
class ParticipantLeft(GameEvent):
    kind: ClassVar[str] = "encounter.left"

    name: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        encounter = state.encounter
        if encounter is None:
            return
        key = name_key(self.name)
        index = next(i for i, name in enumerate(encounter.order) if name_key(name) == key)
        del encounter.order[index]
        encounter.cells.pop(key, None)
        encounter.sides.pop(key, None)
        encounter.initiative.pop(key, None)
        if encounter.acting is not None and name_key(encounter.acting) == key:
            encounter.acting = None
            encounter.moved = 0
        if index < encounter.up:
            encounter.up -= 1
        if encounter.order and encounter.up >= len(encounter.order):
            _next_round(encounter)

    def render(self) -> str:
        return f"{self.name} leaves the encounter."


@dataclass(frozen=True)
class ParticipantPlaced(GameEvent):
    """A participant is put on a cell. `cost` is the cells moved, which only counts against the
    movement allowance when the participant is taking their turn; placing them otherwise is free."""

    kind: ClassVar[str] = "encounter.placed"

    name: str
    column: int
    row: int
    cost: int = 0

    def apply(self, state: CampaignState, event_id: int) -> None:
        encounter = state.encounter
        if encounter is None:
            return
        key = name_key(self.name)
        encounter.cells[key] = (self.column, self.row)
        if encounter.acting is not None and name_key(encounter.acting) == key:
            encounter.moved += self.cost

    def render(self) -> str:
        return f"{self.name} moves to column {self.column + 1}, row {self.row + 1}."


@dataclass(frozen=True)
class EncounterTurnBegun(GameEvent):
    """A participant's turn starts. An NPC's turn opens a GM reply that plays it out, from the
    instruction recorded here. A player character's turn opens nothing: the player acts in chat
    and ends the turn with a button."""

    kind: ClassVar[str] = "encounter.turn_begun"

    name: str
    instruction: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        encounter = state.encounter
        if encounter is None:
            return
        character = state.characters[name_key(self.name)]
        encounter.acting = character.name
        encounter.moved = 0
        if character.kind == NPC_KIND and state.turn is None:
            state.turn = Turn(opener=event_id, text=self.instruction, actor=character.name)

    def render(self) -> str:
        return f"{self.name}'s turn begins."


@dataclass(frozen=True)
class EncounterTurnEnded(GameEvent):
    kind: ClassVar[str] = "encounter.turn_ended"

    name: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        encounter = state.encounter
        if encounter is None:
            return
        encounter.acting = None
        encounter.moved = 0
        encounter.up += 1
        if encounter.up >= len(encounter.order):
            _next_round(encounter)
        _skip_defeated(encounter, state)

    def render(self) -> str:
        return f"{self.name}'s turn ends."


@dataclass(frozen=True)
class InitiativeRolled(GameEvent):
    """A player character's DEX save for initiative. Passing acts before the opponents and failing
    acts after them."""

    kind: ClassVar[str] = "encounter.initiative_rolled"

    name: str
    passed: bool

    def apply(self, state: CampaignState, event_id: int) -> None:
        encounter = state.encounter
        if encounter is None:
            return
        encounter.initiative[name_key(self.name)] = self.passed

    def render(self) -> str:
        result = "passes" if self.passed else "fails"
        return f"{self.name} {result} the initiative save."


@dataclass(frozen=True)
class InitiativeSettled(GameEvent):
    """The round one order, once every player character has rolled for initiative."""

    kind: ClassVar[str] = "encounter.initiative_settled"

    order: list[str]

    def apply(self, state: CampaignState, event_id: int) -> None:
        encounter = state.encounter
        if encounter is None:
            return
        encounter.order = list(self.order)
        encounter.up = 0
        encounter.settled = True

    def render(self) -> str:
        return "Initiative is set: " + ", ".join(self.order) + "."


@dataclass(frozen=True)
class Struck(GameEvent):
    """A hit on a character after armor. `harm` is what each stat loses: hit points take what they
    can, and any overflow comes off the stat named by `save`, which the target must then save
    against."""

    kind: ClassVar[str] = "combat.struck"

    name: str
    attacker: str
    weapon: str
    raw: int
    armor: int
    harm: dict[str, int]
    save: str | None = None
    modifier: str = ""
    rolled: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        for stat, amount in self.harm.items():
            character.harm[stat] = character.harm.get(stat, 0) + amount
        if self.save is not None:
            character.save_due = self.save

    def render(self) -> str:
        taken = sum(self.harm.values())
        weapon = f"{self.weapon} ({self.modifier})" if self.modifier else self.weapon
        text = f"{self.attacker} strikes {self.name} with the {weapon} for {self.raw}"
        if self.armor:
            text += f", {self.armor} absorbed by armor"
        text += f"; {self.name} takes {taken} harm."
        if self.rolled:
            text += f" {self.rolled}"
        if self.save is not None:
            text += f" A {self.save.upper()} save is due."
        return text


@dataclass(frozen=True)
class SaveRolled(GameEvent):
    """A d20 save against a stat. A failed save defeats the character, leaving it `status`. A
    failed Critical Damage save on a detachment leaves it to the Warden's decision instead."""

    kind: ClassVar[str] = "combat.save_rolled"

    name: str
    stat: str
    roll: int
    score: int
    passed: bool
    status: str = ""
    critical: bool = False

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        if character.save_due == self.stat:
            character.save_due = None
        if not self.passed:
            if self.critical:
                character.critical_due = True
            else:
                character.defeated = True

    def render(self) -> str:
        result = "succeeds" if self.passed else "fails"
        text = f"{self.name} rolls {self.roll} on a {self.stat.upper()} save of {self.score} and {result}."
        if not self.passed and self.critical:
            text += (
                f" {self.name} is broken, and the Warden decides whether it is defeated or flees."
            )
        elif not self.passed:
            text += f" {self.name} is {self.status}."
        return text


@dataclass(frozen=True)
class DetachmentResolved(GameEvent):
    """The Warden's decision after a detachment's Critical Damage save failed. `outcome` is
    "defeat", leaving it `status`, or "flee", breaking it up so its members run from the fight."""

    kind: ClassVar[str] = "combat.detachment_resolved"

    name: str
    outcome: str
    status: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        character.critical_due = False
        if self.outcome == "defeat":
            character.defeated = True
        else:
            character.fled = True

    def render(self) -> str:
        if self.outcome == "defeat":
            return f"{self.name} is {self.status}."
        return f"{self.name} breaks up, and its members flee the fight."


@dataclass(frozen=True)
class TablesRolled(GameEvent):
    """The Warden rolled on one or more dice tables. Each roll records its table, name, roll, and
    result."""

    kind: ClassVar[str] = "dice.tables_rolled"

    rolls: list[dict[str, Any]]

    def apply(self, state: CampaignState, event_id: int) -> None:
        return None

    def render(self) -> str:
        return " ".join(
            f"{roll['name']}: rolls {roll['roll']} and gets {roll['result']}."
            for roll in self.rolls
        )


@dataclass(frozen=True)
class ArmorSet(GameEvent):
    kind: ClassVar[str] = "combat.armor_set"

    name: str
    armor: int

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.characters[name_key(self.name)].armor = self.armor

    def render(self) -> str:
        return f"{self.name}'s armor is now {self.armor}."


@dataclass(frozen=True)
class InvincibilitySet(GameEvent):
    """A debug buff on a player character. While it holds, no harm reaches the character. Turning
    it on cancels a save the character owes, since no harm can make it due."""

    kind: ClassVar[str] = "combat.invincibility_set"
    channel: ClassVar[Channel] = Channel.INTERNAL

    name: str
    active: bool

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        character.invincible = self.active
        if self.active:
            character.save_due = None

    def render(self) -> str:
        return f"{self.name} is {'invincible' if self.active else 'no longer invincible'}."


@dataclass(frozen=True)
class DebugStatSet(GameEvent):
    """A debug setting of a player character's current score for one stat, by recording the harm
    that leaves it at `remaining`. Internal, so the GM does not see it."""

    kind: ClassVar[str] = "combat.debug_stat_set"
    channel: ClassVar[Channel] = Channel.INTERNAL

    name: str
    stat: str
    remaining: int

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        character.harm[self.stat] = max(0, character.stats.get(self.stat, 0) - self.remaining)

    def render(self) -> str:
        return f"{self.name}'s {self.stat.upper()} is set to {self.remaining}."


@dataclass(frozen=True)
class EffectNoted(GameEvent):
    """A rule effect with no mechanical change, such as a location roll or a choice the GM narrates."""

    kind: ClassVar[str] = "effect.noted"

    name: str
    text: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        return None

    def render(self) -> str:
        return f"{self.name}: {self.text}"


@dataclass(frozen=True)
class MaxChanged(GameEvent):
    """A stat's maximum score changed from `before` to `after`. `cause` says what changed it."""

    kind: ClassVar[str] = "character.max_changed"

    name: str
    stat: str
    before: int
    after: int
    cause: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.characters[name_key(self.name)].stats[self.stat] = self.after

    def render(self) -> str:
        text = f"{self.name}'s maximum {self.stat.upper()} goes from {self.before} to {self.after}"
        return f"{text} ({self.cause})." if self.cause else f"{text}."


@dataclass(frozen=True)
class ConditionAdded(GameEvent):
    kind: ClassVar[str] = "condition.added"

    name: str
    condition: str
    cause: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.characters[name_key(self.name)].conditions.append(self.condition)

    def render(self) -> str:
        text = f"{self.name} gains the condition {self.condition}"
        return f"{text} ({self.cause})." if self.cause else f"{text}."


@dataclass(frozen=True)
class ConditionEnded(GameEvent):
    kind: ClassVar[str] = "condition.ended"

    name: str
    condition: str
    how: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        conditions = state.characters[name_key(self.name)].conditions
        if self.condition in conditions:
            conditions.remove(self.condition)

    def render(self) -> str:
        return f"{self.name} no longer has the condition {self.condition} (ended by {self.how})."


@dataclass(frozen=True)
class ScarGained(GameEvent):
    """A scar from the Scars table. `roll` is the table entry it came from."""

    kind: ClassVar[str] = "scar.gained"

    name: str
    scar: str
    roll: int
    text: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.characters[name_key(self.name)].scars.append(self.scar)

    def render(self) -> str:
        text = f"{self.name} gains the scar {self.scar} (Scars table {self.roll})."
        return f"{text} {self.text}" if self.text else text


@dataclass(frozen=True)
class Harmed(GameEvent):
    """Harm taken outside combat, which takes no armor. `defeats` is set when it took the stat that
    decides defeat to 0; `status` is where that leaves the character."""

    kind: ClassVar[str] = "combat.harmed"

    name: str
    stat: str
    taken: int
    cause: str = ""
    defeats: bool = False
    status: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        if self.taken:
            character.harm[self.stat] = character.harm.get(self.stat, 0) + self.taken
        if self.defeats:
            character.defeated = True

    def render(self) -> str:
        text = f"{self.name} loses {self.taken} {self.stat.upper()}"
        if self.cause:
            text += f" from {self.cause}"
        text += "."
        if self.defeats:
            text += f" {self.name} is {self.status}."
        return text


@dataclass(frozen=True)
class CharacterSaved(GameEvent):
    """A save made outside a fight, such as one a scar calls for. Unlike SaveRolled, a failure does
    not defeat the character; the effects of a pass or fail are recorded as their own events."""

    kind: ClassVar[str] = "character.saved"

    name: str
    stat: str
    roll: int
    score: int
    passed: bool
    cause: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        return None

    def render(self) -> str:
        result = "succeeds" if self.passed else "fails"
        text = f"{self.name} rolls {self.roll} on a {self.stat.upper()} save of {self.score} and {result}"
        return f"{text} ({self.cause})." if self.cause else f"{text}."


@dataclass(frozen=True)
class ClockAdvanced(GameEvent):
    """The campaign clock moved to a new day and part. A player character in a place is there
    at the new time, so the place counts as visited."""

    kind: ClassVar[str] = "clock.advanced"

    day: int
    part: int
    part_name: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.clock = Clock(day=self.day, part=self.part)
        for character in state.characters.values():
            if character.kind != PC_KIND or character.location is None:
                continue
            key = name_key(character.location)
            if key in state.places:
                state.places[key] = replace(state.places[key], visited_day=self.day)

    def render(self) -> str:
        return f"It is day {self.day}, {self.part_name}."


@dataclass(frozen=True)
class RestTaken(GameEvent):
    kind: ClassVar[str] = "rest.taken"

    length: str
    full: bool
    interrupted: bool
    reason: str
    day: int
    part: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.rests.append(
            RestRecord(
                day=self.day,
                part=self.part,
                length=self.length,
                full=self.full,
                interrupted=self.interrupted,
                reason=self.reason,
            )
        )

    def render(self) -> str:
        interrupted = ", interrupted" if self.interrupted else ""
        return f"The party rests ({self.length}{interrupted}): {self.reason}."


@dataclass(frozen=True)
class RestRecovered(GameEvent):
    """A character recovers the stats a rest restores. Recovery clears their harm in those stats."""

    kind: ClassVar[str] = "rest.recovered"

    name: str
    restores: list[str]

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        for stat in self.restores:
            character.harm.pop(stat, None)

    def render(self) -> str:
        return f"{self.name} recovers from the rest."


@dataclass(frozen=True)
class DeprivationSet(GameEvent):
    """A character becomes deprived of a crucial need, or stops being deprived. A character who is
    already deprived keeps the day the deprivation began."""

    kind: ClassVar[str] = "character.deprivation_set"

    name: str
    deprived: bool
    cause: str
    day: int

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        if self.deprived:
            if character.deprived_since is None:
                character.deprived_since = self.day
            character.deprivation_cause = self.cause
        else:
            character.deprived_since = None
            character.deprivation_cause = ""

    def render(self) -> str:
        if self.deprived:
            return f"{self.name} is deprived: {self.cause}."
        return f"{self.name} is no longer deprived."


@dataclass(frozen=True)
class ItemAdded(GameEvent):
    kind: ClassVar[str] = "character.item_added"

    name: str
    item: str
    size: str
    slots: int
    kind_of_item: str = ""
    cause: str = ""
    text: str = ""
    uses: int = 0
    max_uses: int = 0

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        character.items.append(
            Item(
                name=self.item,
                size=self.size,
                slots=self.slots,
                kind=self.kind_of_item,
                text=self.text,
                uses=self.uses,
                max_uses=self.max_uses,
            )
        )

    def render(self) -> str:
        text = f"{self.name} gains {self.item}"
        return f"{text} ({self.cause})." if self.cause else f"{text}."


@dataclass(frozen=True)
class ItemUsesSet(GameEvent):
    """Sets a relic's uses left, after a use or a recharge. `cause` says which."""

    kind: ClassVar[str] = "character.item_uses_set"

    name: str
    item: str
    uses: int
    cause: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        for index, held in enumerate(character.items):
            if name_key(held.name) == name_key(self.item):
                spent_on = state.clock if self.cause == "used" else None
                character.items[index] = replace(held, uses=self.uses, spent_on=spent_on)
                return

    def render(self) -> str:
        text = f"{self.name}'s {self.item} has {self.uses} use{'' if self.uses == 1 else 's'} left"
        return f"{text} ({self.cause})." if self.cause else f"{text}."


@dataclass(frozen=True)
class SpellCast(GameEvent):
    """A spell cast from a spellbook or a scroll. The cost, if any, is recorded as its own event."""

    kind: ClassVar[str] = "magic.spell_cast"

    name: str
    spell: str
    source: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        return None

    def render(self) -> str:
        where = "from a spellbook" if self.source == "spellbook" else "from a scroll"
        return f"{self.name} casts {self.spell} {where}."


@dataclass(frozen=True)
class ItemRemoved(GameEvent):
    """Removes the first item of the character with this name, and of this kind if one is given."""

    kind: ClassVar[str] = "character.item_removed"

    name: str
    item: str
    kind_of_item: str = ""

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        for index, held in enumerate(character.items):
            if name_key(held.name) != name_key(self.item):
                continue
            if self.kind_of_item and held.kind != self.kind_of_item:
                continue
            del character.items[index]
            return

    def render(self) -> str:
        return f"{self.name} loses {self.item}."


@dataclass(frozen=True)
class CharacterDrained(GameEvent):
    """Harm takes a stat to 0 outside combat, such as when a full inventory drains HP. It is not
    overflow, so no save is owed."""

    kind: ClassVar[str] = "character.drained"

    name: str
    stat: str
    cause: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        character.harm[self.stat] = character.stats.get(self.stat, 0)

    def render(self) -> str:
        return f"{self.name}'s {self.stat.upper()} drops to 0 ({self.cause})."
