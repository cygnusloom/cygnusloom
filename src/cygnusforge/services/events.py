"""A campaign's event log: everything that happened, in order, and the tables derived from it.

Each change to the world or the chat is an event appended with `record`. The tables the rest of
the app reads (locations, characters, notes, and the chat summary) are projections: each event's
`apply` updates them. A rollback removes the events after a point and `rebuild` replays what is
left, so every projection returns to the state it had at that point.

To add an event, subclass `GameEvent` as a frozen dataclass with a unique `kind`, a `channel`,
an `apply` if it changes projections, and a `render` if it appears in the Log. The fields are
the JSON payload. Store resolved values (names, rolled traits) rather than the inputs used to
compute them, so that replaying the log gives the same result. Refer to places and characters by
name, which is unique within a campaign and never changes."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import delete, func
from sqlmodel import Session, select

from cygnusforge.db.models import Actor, Campaign, Event, Location, Note, TurnContext

PC_KIND = "pc"
NPC_KIND = "npc"
CAMPAIGN_WIDE = "campaign-wide"


class Channel(StrEnum):
    TRANSCRIPT = "transcript"
    LOG = "log"
    INTERNAL = "internal"


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

    def apply(self, db: Session, campaign_id: int) -> None:
        """Update the projections this event affects. Most events affect none."""

    def render(self) -> str:
        raise NotImplementedError(f"{type(self).__name__} has no Log text")


def record(db: Session, campaign_id: int, event: GameEvent) -> int:
    """Append an event to the campaign's log, apply it, and return its id."""
    row = Event(
        campaign_id=campaign_id,
        channel=event.channel.value,
        kind=event.kind,
        payload=json.dumps(asdict(event)),
    )
    db.add(row)
    db.flush()
    if row.id is None:
        raise RuntimeError("the event was not assigned an id")
    event.apply(db, campaign_id)
    return row.id


def decode(row: Event) -> GameEvent:
    return EVENT_TYPES[row.kind](**json.loads(row.payload))


def events_since(
    db: Session, campaign_id: int, after: int = 0, channel: Channel | None = None
) -> list[Event]:
    statement = select(Event).where(Event.campaign_id == campaign_id, Event.id > after)
    if channel is not None:
        statement = statement.where(Event.channel == channel.value)
    return list(db.exec(statement.order_by(Event.id)))


def last_event_id(db: Session, campaign_id: int) -> int:
    """The id of the campaign's newest event, or 0 when it has none."""
    newest = db.exec(select(func.max(Event.id)).where(Event.campaign_id == campaign_id)).one()
    return newest or 0


def truncate(db: Session, campaign_id: int, after: int) -> None:
    """Remove the campaign's events after `after` and rebuild the projections from the rest."""
    later = select(Event.id).where(Event.campaign_id == campaign_id, Event.id > after)
    db.exec(delete(TurnContext).where(TurnContext.event_id.in_(later)))
    db.exec(delete(Event).where(Event.campaign_id == campaign_id, Event.id > after))
    rebuild(db, campaign_id)


def rebuild(db: Session, campaign_id: int) -> None:
    """Clear the campaign's projections and replay its whole log to refill them."""
    db.exec(delete(Note).where(Note.campaign_id == campaign_id))
    db.exec(delete(Actor).where(Actor.campaign_id == campaign_id))
    db.exec(delete(Location).where(Location.campaign_id == campaign_id))
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise LookupError(f"there is no campaign {campaign_id}")
    campaign.context_summary = ""
    campaign.summary_through = 0
    db.add(campaign)
    for row in events_since(db, campaign_id):
        decode(row).apply(db, campaign_id)


def named_entry(db: Session, campaign_id: int, name: str) -> Location | Actor | None:
    """The location or character with this name, ignoring case. Both share one namespace."""
    key = name.strip().casefold()
    locations = db.exec(select(Location).where(Location.campaign_id == campaign_id)).all()
    actors = db.exec(select(Actor).where(Actor.campaign_id == campaign_id)).all()
    for entry in [*locations, *actors]:
        if entry.name.casefold() == key:
            return entry
    return None


def _location(db: Session, campaign_id: int, name: str) -> Location:
    entry = named_entry(db, campaign_id, name)
    if isinstance(entry, Location):
        return entry
    raise LookupError(f"the log refers to a missing location {name!r}")


def _actor(db: Session, campaign_id: int, name: str) -> Actor:
    entry = named_entry(db, campaign_id, name)
    if isinstance(entry, Actor):
        return entry
    raise LookupError(f"the log refers to a missing character {name!r}")


@dataclass(frozen=True)
class LocationAdded(GameEvent):
    kind: ClassVar[str] = "location.added"

    name: str
    description: str

    def apply(self, db: Session, campaign_id: int) -> None:
        db.add(Location(campaign_id=campaign_id, name=self.name, description=self.description))

    def render(self) -> str:
        return f"New location: {self.name}."


@dataclass(frozen=True)
class PcAdded(GameEvent):
    kind: ClassVar[str] = "pc.added"

    name: str
    location: str

    def apply(self, db: Session, campaign_id: int) -> None:
        db.add(
            Actor(
                campaign_id=campaign_id,
                kind=PC_KIND,
                name=self.name,
                location_id=_location(db, campaign_id, self.location).id,
                description="",
                personality="",
                accent="",
                quirks="[]",
            )
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

    def apply(self, db: Session, campaign_id: int) -> None:
        db.add(
            Actor(
                campaign_id=campaign_id,
                kind=NPC_KIND,
                name=self.name,
                location_id=_location(db, campaign_id, self.location).id,
                description=self.description,
                personality=self.personality,
                accent=self.accent,
                quirks=json.dumps(self.quirks),
            )
        )

    def render(self) -> str:
        return f"New character: {self.name}, at {self.location}."


@dataclass(frozen=True)
class ActorMoved(GameEvent):
    kind: ClassVar[str] = "actor.moved"

    name: str
    location: str

    def apply(self, db: Session, campaign_id: int) -> None:
        actor = _actor(db, campaign_id, self.name)
        actor.location_id = _location(db, campaign_id, self.location).id
        db.add(actor)

    def render(self) -> str:
        return f"{self.name} moved to {self.location}."


@dataclass(frozen=True)
class NoteAdded(GameEvent):
    kind: ClassVar[str] = "note.added"

    text: str
    subject: str | None = None

    def apply(self, db: Session, campaign_id: int) -> None:
        note = Note(campaign_id=campaign_id, text=self.text)
        if self.subject is not None:
            entry = named_entry(db, campaign_id, self.subject)
            if isinstance(entry, Location):
                note.location_id = entry.id
            elif isinstance(entry, Actor):
                note.actor_id = entry.id
            else:
                raise LookupError(f"the log refers to a missing subject {self.subject!r}")
        db.add(note)

    def render(self) -> str:
        if self.subject is None:
            return f"Note: {self.text}"
        return f"Note about {self.subject}: {self.text}"


@dataclass(frozen=True)
class ToolActivity(GameEvent):
    kind: ClassVar[str] = "tool.activity"

    text: str

    def render(self) -> str:
        return self.text


@dataclass(frozen=True)
class ChatPlayer(GameEvent):
    kind: ClassVar[str] = "chat.player"
    channel: ClassVar[Channel] = Channel.TRANSCRIPT

    content: str

    def render(self) -> str:
        return self.content


@dataclass(frozen=True)
class ChatGM(GameEvent):
    kind: ClassVar[str] = "chat.gm"
    channel: ClassVar[Channel] = Channel.TRANSCRIPT

    content: str
    model: str

    def render(self) -> str:
        return self.content


@dataclass(frozen=True)
class SummaryUpdated(GameEvent):
    """The chat up to and including event `through` is now represented by `summary`."""

    kind: ClassVar[str] = "summary.updated"
    channel: ClassVar[Channel] = Channel.INTERNAL

    summary: str
    through: int

    def apply(self, db: Session, campaign_id: int) -> None:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None:
            raise LookupError(f"there is no campaign {campaign_id}")
        campaign.context_summary = self.summary
        campaign.summary_through = self.through
        db.add(campaign)
