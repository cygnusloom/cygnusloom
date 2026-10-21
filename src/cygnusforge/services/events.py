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
from collections.abc import Iterable
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
    # Harm taken so far, by stat id. A stat's current value is its score less its harm, at least 0.
    harm: dict[str, int] = field(default_factory=dict)
    # The stat id of a save the character must make after harm overflowed, or None.
    save_due: str | None = None
    defeated: bool = False


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
        )

    def render(self) -> str:
        return f"New character: {self.name}, at {self.location}."


@dataclass(frozen=True)
class ActorMoved(GameEvent):
    kind: ClassVar[str] = "actor.moved"

    name: str
    location: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.characters[name_key(self.name)].location = self.location

    def render(self) -> str:
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
        state.places[key] = Place(
            self.name, self.description, before.discovered, _position(self.x, self.y), before.parent
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
    kind: ClassVar[str] = "encounter.ended"

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.encounter = None

    def render(self) -> str:
        return "The encounter is over."


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
    source: str
    raw: int
    armor: int
    harm: dict[str, int]
    save: str | None = None

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        for stat, amount in self.harm.items():
            character.harm[stat] = character.harm.get(stat, 0) + amount
        if self.save is not None:
            character.save_due = self.save

    def render(self) -> str:
        taken = sum(self.harm.values())
        text = f"{self.source} hits {self.name} for {self.raw}"
        if self.armor:
            text += f", {self.armor} absorbed by armor"
        text += f"; {self.name} takes {taken} harm."
        if self.save is not None:
            text += f" A {self.save.upper()} save is due."
        return text


@dataclass(frozen=True)
class SaveRolled(GameEvent):
    """A d20 save against a stat. A failed save defeats the character."""

    kind: ClassVar[str] = "combat.save_rolled"

    name: str
    stat: str
    roll: int
    score: int
    passed: bool

    def apply(self, state: CampaignState, event_id: int) -> None:
        character = state.characters[name_key(self.name)]
        if character.save_due == self.stat:
            character.save_due = None
        if not self.passed:
            character.defeated = True

    def render(self) -> str:
        result = "succeeds" if self.passed else "fails"
        text = f"{self.name} rolls {self.roll} on a {self.stat.upper()} save of {self.score} and {result}."
        if not self.passed:
            text += f" {self.name} is defeated."
        return text


@dataclass(frozen=True)
class ArmorSet(GameEvent):
    kind: ClassVar[str] = "combat.armor_set"

    name: str
    armor: int

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.characters[name_key(self.name)].armor = self.armor

    def render(self) -> str:
        return f"{self.name}'s armor is now {self.armor}."
