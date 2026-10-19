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
from dataclasses import asdict, dataclass, field, fields
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
class Place:
    name: str
    description: str


@dataclass
class Character:
    kind: str
    name: str
    location: str
    description: str
    personality: str
    accent: str
    quirks: list[str]
    # The character options, resolved when the character was created. See content/character.py.
    stats: dict[str, int] = field(default_factory=dict)
    templates: dict[str, str] = field(default_factory=dict)
    features: dict[str, int] = field(default_factory=dict)
    fields: dict[str, str] = field(default_factory=dict)


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
    begun: bool = False
    head: list[ContextPart] = field(default_factory=list)
    context: list[ContextPart] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    round_start: int = 0
    rounds: int = 0
    queue: list[ToolRequest] = field(default_factory=list)
    narrating: bool = False
    prompt: Ask | None = None
    # NPC lines written this turn that the narration has not seen yet: {"speaker", "dialogue"}.
    speech: list[dict[str, str]] = field(default_factory=list)
    # Why the last step stopped, when it stopped with an error and has not been retried yet.
    failed: str | None = None


@dataclass
class CampaignState:
    """The campaign as its log says it is now. Keys of `places` and `characters` are the names
    without case, so places and characters share one namespace."""

    places: dict[str, Place] = field(default_factory=dict)
    characters: dict[str, Character] = field(default_factory=dict)
    notes: list[Note] = field(default_factory=list)
    summary: str = ""
    through: int = 0
    turn: Turn | None = None

    def named(self, name: str) -> Place | Character | None:
        key = name_key(name)
        if key in self.places:
            return self.places[key]
        return self.characters.get(key)

    def pc(self) -> Character | None:
        return next((c for c in self.characters.values() if c.kind == PC_KIND), None)


def name_key(name: str) -> str:
    return name.strip().casefold()


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

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.places[name_key(self.name)] = Place(self.name, self.description)

    def render(self) -> str:
        return f"New location: {self.name}."


@dataclass(frozen=True)
class PcAdded(GameEvent):
    kind: ClassVar[str] = "pc.added"

    name: str
    location: str
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
        return f"{self.name} begins at {self.location}."


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
    kind: ClassVar[str] = "location.updated"

    name: str
    description: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.places[name_key(self.name)] = Place(self.name, self.description)

    def render(self) -> str:
        return f"Location updated: {self.name}."


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
class ChatGM(GameEvent):
    """The GM's reply. It closes the turn, and the turn's context is what this reply was written
    from."""

    kind: ClassVar[str] = "chat.gm"
    channel: ClassVar[Channel] = Channel.TRANSCRIPT

    content: str
    model: str

    def apply(self, state: CampaignState, event_id: int) -> None:
        state.turn = None

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
class PromptShown(GameEvent):
    """The open turn is waiting for the player to roll or type a reply."""

    kind: ClassVar[str] = "turn.prompted"
    channel: ClassVar[Channel] = Channel.INTERNAL

    text: str
    dice: str
    details: str | None = None

    def apply(self, state: CampaignState, event_id: int) -> None:
        _open_turn(state).prompt = Ask(self.text, self.dice, self.details)
