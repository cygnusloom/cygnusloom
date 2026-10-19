"""Locations, characters, and notes: the structured world that a campaign's GM edits.

Functions take an open session and commit their own changes. Problems the model can fix are
raised as `InvalidInput`, and their messages are returned to the model as they are."""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from cygnusforge.content.library import WorldTemplate
from cygnusforge.db.models import Actor, Location, Note
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import (
    CAMPAIGN_WIDE,
    NPC_KIND,
    PC_KIND,
    ActorMoved,
    LocationAdded,
    NoteAdded,
    NpcAdded,
    PcAdded,
    named_entry,
    record,
)

MAX_NAME = 120
MAX_TEXT = 2000
MAX_QUIRKS = 10
NOTE_RESULTS = 10
QUIRKS_PER_NPC = 2


@dataclass(frozen=True)
class NoteView:
    subject: str
    text: str


@dataclass(frozen=True)
class WorldView:
    pc: Actor | None
    locations: list[Location]
    npcs: list[Actor]
    notes: list[NoteView]
    location_names: dict[int, str] = field(default_factory=dict)


def seed_campaign(
    db: Session,
    campaign_id: int,
    world: WorldTemplate,
    tables: Mapping[str, Sequence[str]],
    rng: random.Random,
    character_name: str,
) -> None:
    """Record a campaign's locations, the world's NPCs, and the player character as events."""
    names: dict[str, str] = {}
    for spot in world.locations:
        record(db, campaign_id, LocationAdded(name=spot.name, description=spot.description))
        names[spot.id] = spot.name
    for npc in world.npcs:
        event = _npc_event(
            tables,
            rng,
            name=npc.name,
            location=names[npc.location],
            description=npc.description,
            personality=npc.personality,
            accent=npc.accent,
            quirks=npc.quirks,
        )
        record(db, campaign_id, event)
    _ensure_free_name(db, campaign_id, character_name)
    record(db, campaign_id, PcAdded(name=character_name, location=names[world.start_location]))
    db.commit()


def create_location(db: Session, campaign_id: int, name: str, description: str) -> dict[str, Any]:
    name = _text(name, "name", MAX_NAME)
    _ensure_free_name(db, campaign_id, name)
    text = _text(description, "description", MAX_TEXT)
    record(db, campaign_id, LocationAdded(name=name, description=text))
    db.commit()
    return {"created": "location", "name": name}


def create_npc(
    db: Session,
    campaign_id: int,
    tables: Mapping[str, Sequence[str]],
    rng: random.Random,
    name: str,
    location: str,
    description: str,
    personality: str | None = None,
    accent: str | None = None,
    quirks: Sequence[str] | None = None,
) -> dict[str, Any]:
    name = _text(name, "name", MAX_NAME)
    _ensure_free_name(db, campaign_id, name)
    where = _location_named(db, campaign_id, location)
    event = _npc_event(
        tables,
        rng,
        name=name,
        location=where.name,
        description=_text(description, "description", MAX_TEXT),
        personality=_optional_text(personality, "personality"),
        accent=_optional_text(accent, "accent"),
        quirks=_quirks(quirks),
    )
    record(db, campaign_id, event)
    db.commit()
    return {
        "created": "npc",
        "name": event.name,
        "location": event.location,
        "personality": event.personality,
        "accent": event.accent,
        "quirks": list(event.quirks),
    }


def move_actor(db: Session, campaign_id: int, name: str, location: str) -> dict[str, str]:
    actor = _actor_named(db, campaign_id, name)
    where = _location_named(db, campaign_id, location)
    record(db, campaign_id, ActorMoved(name=actor.name, location=where.name))
    db.commit()
    return {"moved": actor.name, "location": where.name}


def add_note(
    db: Session, campaign_id: int, text: str, subject: str | None = None
) -> dict[str, str]:
    text = _text(text, "text", MAX_TEXT)
    subject_name: str | None = None
    label = _optional_text(subject, "subject")
    if label is not None:
        entry = named_entry(db, campaign_id, label)
        if entry is None:
            raise InvalidInput(f"there is no location or character named {label!r}")
        subject_name = entry.name
    record(db, campaign_id, NoteAdded(text=text, subject=subject_name))
    db.commit()
    return {"noted": True, "subject": subject_name if subject_name is not None else CAMPAIGN_WIDE}


def search_notes(db: Session, campaign_id: int, query: str) -> dict[str, list[dict[str, str]]]:
    """Notes whose text or subject name contains every word of `query`, newest first."""
    words = _text(query, "query", MAX_TEXT).casefold().split()
    view = snapshot(db, campaign_id)
    matches: list[dict[str, str]] = []
    for note in view.notes:
        haystack = f"{note.subject} {note.text}".casefold()
        if all(word in haystack for word in words):
            matches.append({"subject": note.subject, "text": note.text})
        if len(matches) == NOTE_RESULTS:
            break
    return {"notes": matches}


def speaker_profile(db: Session, campaign_id: int, name: str) -> str:
    """The NPC details a speech step needs. The NPC must be at the player character's location."""
    actor = _actor_named(db, campaign_id, name)
    if actor.kind != NPC_KIND:
        raise InvalidInput(f"{actor.name} is the player character and does not need speaking for")
    pc = _pc(db, campaign_id)
    if pc is None or actor.location_id != pc.location_id:
        raise InvalidInput(f"{actor.name} is not at the player character's location")
    view = snapshot(db, campaign_id)
    quirks = "; ".join(actor.quirk_list) or "none"
    return "\n".join(
        [
            f"Speaker: {actor.name}",
            f"Who they are: {actor.description}",
            f"Personality: {actor.personality}",
            f"Accent: {actor.accent}",
            f"Quirks: {quirks}",
            f"Location: {view.location_names[actor.location_id]}",
        ]
    )


def snapshot(db: Session, campaign_id: int) -> WorldView:
    locations = list(
        db.exec(select(Location).where(Location.campaign_id == campaign_id).order_by(Location.id))
    )
    actors = list(db.exec(select(Actor).where(Actor.campaign_id == campaign_id).order_by(Actor.id)))
    notes = list(
        db.exec(select(Note).where(Note.campaign_id == campaign_id).order_by(Note.id.desc()))
    )
    location_names = {loc.id: loc.name for loc in locations if loc.id is not None}
    actor_names = {actor.id: actor.name for actor in actors if actor.id is not None}
    note_views = [
        NoteView(
            subject=_subject(note, location_names, actor_names),
            text=note.text,
        )
        for note in notes
    ]
    return WorldView(
        pc=next((a for a in actors if a.kind == PC_KIND), None),
        locations=locations,
        npcs=[a for a in actors if a.kind == NPC_KIND],
        notes=note_views,
        location_names=location_names,
    )


def world_state_text(db: Session, campaign_id: int) -> str:
    """The current places and characters, for the GM's system prompt. Notes are searched, not sent."""
    view = snapshot(db, campaign_id)
    if view.pc is None:
        return ""
    lines = [
        f"Player character: {view.pc.name}, at {view.location_names[view.pc.location_id]}.",
        "Locations:",
    ]
    lines += [f"- {loc.name}: {loc.description}" for loc in view.locations]
    lines.append("NPCs:")
    if not view.npcs:
        lines.append("- none yet")
    for npc in view.npcs:
        where = view.location_names[npc.location_id]
        lines.append(f"- {npc.name}, at {where}: {npc.description}")
    return "\n".join(lines)


def _npc_event(
    tables: Mapping[str, Sequence[str]],
    rng: random.Random,
    *,
    name: str,
    location: str,
    description: str,
    personality: str | None,
    accent: str | None,
    quirks: Sequence[str] | None,
) -> NpcAdded:
    """The event that adds an NPC. A trait that is not given is picked at random from its table,
    so the picked value is stored in the event and replays do not roll again."""
    trait_personality = personality or rng.choice(tables["personality"])
    trait_accent = accent or rng.choice(tables["accent"])
    if quirks is None:
        pool = tables["quirk"]
        trait_quirks = rng.sample(list(pool), min(QUIRKS_PER_NPC, len(pool)))
    else:
        trait_quirks = list(quirks)
    return NpcAdded(
        name=name,
        location=location,
        description=description,
        personality=trait_personality,
        accent=trait_accent,
        quirks=trait_quirks,
    )


def _subject(note: Note, location_names: Mapping[int, str], actor_names: Mapping[int, str]) -> str:
    if note.location_id is not None:
        return location_names.get(note.location_id, CAMPAIGN_WIDE)
    if note.actor_id is not None:
        return actor_names.get(note.actor_id, CAMPAIGN_WIDE)
    return CAMPAIGN_WIDE


def _pc(db: Session, campaign_id: int) -> Actor | None:
    return db.exec(
        select(Actor).where(Actor.campaign_id == campaign_id, Actor.kind == PC_KIND)
    ).first()


def _ensure_free_name(db: Session, campaign_id: int, name: str) -> None:
    if named_entry(db, campaign_id, name) is not None:
        raise InvalidInput(f"the name {name!r} is already used by a location or character")


def _location_named(db: Session, campaign_id: int, name: str) -> Location:
    entry = named_entry(db, campaign_id, name)
    if not isinstance(entry, Location):
        raise InvalidInput(f"there is no location named {name!r}")
    return entry


def _actor_named(db: Session, campaign_id: int, name: str) -> Actor:
    entry = named_entry(db, campaign_id, name)
    if not isinstance(entry, Actor):
        raise InvalidInput(f"there is no character named {name!r}")
    return entry


def _text(value: object, label: str, limit: int) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > limit:
        raise InvalidInput(f"{label} must be 1 to {limit} characters")
    return text


def _optional_text(value: object, label: str) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _text(value, label, MAX_NAME)


def _quirks(value: Sequence[str] | None) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str) or len(value) > MAX_QUIRKS:
        raise InvalidInput(f"quirks must be a list of up to {MAX_QUIRKS} short texts")
    return [_text(item, "each quirk", MAX_NAME) for item in value]
