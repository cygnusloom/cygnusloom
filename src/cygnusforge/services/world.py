"""Places, characters, and notes: the world that a campaign's GM edits.

Writes check the campaign's current state, record events, and commit. Reads work on a state that
is already folded from the log. Problems the model can fix are raised as `InvalidInput`, and their
messages are returned to the model as they are."""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine
from sqlmodel import Session

from cygnusforge.content.library import WorldTemplate
from cygnusforge.services.common import InvalidInput, session_scope
from cygnusforge.services.events import (
    CAMPAIGN_WIDE,
    NPC_KIND,
    ActorMoved,
    CampaignState,
    Character,
    LocationAdded,
    LocationRemoved,
    LocationUpdated,
    Note,
    NoteAdded,
    NoteRemoved,
    NoteUpdated,
    NpcAdded,
    NpcRemoved,
    NpcUpdated,
    PcAdded,
    Place,
    load_state,
    record,
)

MAX_NAME = 120
MAX_TEXT = 2000
MAX_QUIRKS = 10
QUIRKS_PER_NPC = 2


@dataclass(frozen=True)
class WorldView:
    pc: Character | None
    locations: list[Place]
    npcs: list[Character]
    notes: list[Note]


@dataclass(frozen=True)
class WorldTools:
    """The world as the rule set's tools use it. Each call runs in its own session and commits."""

    engine: Engine
    campaign_id: int
    tables: Mapping[str, Sequence[str]]
    rng: random.Random

    def create_location(self, name: str, description: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return create_location(db, self.campaign_id, name, description)

    def create_npc(
        self,
        name: str,
        location: str,
        description: str,
        personality: str | None = None,
        accent: str | None = None,
        quirks: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return create_npc(
                db,
                self.campaign_id,
                self.tables,
                self.rng,
                name,
                location,
                description,
                personality,
                accent,
                quirks,
            )

    def move_actor(self, name: str, location: str) -> dict[str, str]:
        with session_scope(self.engine) as db:
            return move_actor(db, self.campaign_id, name, location)

    def update_location(self, name: str, description: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return update_location(db, self.campaign_id, name, description)

    def delete_location(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return delete_location(db, self.campaign_id, name)

    def update_npc(
        self,
        name: str,
        description: str | None = None,
        personality: str | None = None,
        accent: str | None = None,
        quirks: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return update_npc(db, self.campaign_id, name, description, personality, accent, quirks)

    def delete_npc(self, name: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return delete_npc(db, self.campaign_id, name)

    def add_note(self, text: str, subject: str | None = None) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return add_note(db, self.campaign_id, text, subject)

    def update_note(self, note_id: int, text: str) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return update_note(db, self.campaign_id, note_id, text)

    def delete_note(self, note_id: int) -> dict[str, Any]:
        with session_scope(self.engine) as db:
            return delete_note(db, self.campaign_id, note_id)

    def search_notes(self, query: str) -> dict[str, list[dict[str, Any]]]:
        with session_scope(self.engine) as db:
            return search_notes(load_state(db, self.campaign_id), query)

    def speaker_profile(self, name: str) -> str:
        with session_scope(self.engine) as db:
            return speaker_profile(load_state(db, self.campaign_id), name)


def seed_campaign(
    db: Session,
    campaign_id: int,
    world: WorldTemplate,
    tables: Mapping[str, Sequence[str]],
    rng: random.Random,
    character_name: str,
) -> None:
    """Record a campaign's places, the world's NPCs, and the player character as events."""
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
    _ensure_free_name(load_state(db, campaign_id), character_name)
    record(db, campaign_id, PcAdded(name=character_name, location=names[world.start_location]))
    db.commit()


def create_location(db: Session, campaign_id: int, name: str, description: str) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    name = _text(name, "name", MAX_NAME)
    _ensure_free_name(state, name)
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
    state = load_state(db, campaign_id)
    name = _text(name, "name", MAX_NAME)
    _ensure_free_name(state, name)
    where = _place_named(state, location)
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


def update_location(db: Session, campaign_id: int, name: str, description: str) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    where = _place_named(state, name)
    text = _text(description, "description", MAX_TEXT)
    record(db, campaign_id, LocationUpdated(name=where.name, description=text))
    db.commit()
    return {"updated": "location", "name": where.name}


def delete_location(db: Session, campaign_id: int, name: str) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    where = _place_named(state, name)
    occupants = [c.name for c in state.characters.values() if c.location == where.name]
    if occupants:
        raise InvalidInput(
            f"{where.name} still has {', '.join(occupants)} there; move them elsewhere first"
        )
    record(db, campaign_id, LocationRemoved(name=where.name))
    db.commit()
    return {"deleted": "location", "name": where.name}


def update_npc(
    db: Session,
    campaign_id: int,
    name: str,
    description: str | None = None,
    personality: str | None = None,
    accent: str | None = None,
    quirks: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Change the NPC's details. Only the given fields change; quirks, if given, replace the list."""
    state = load_state(db, campaign_id)
    npc = _npc_named(state, name)
    if all(value is None for value in (description, personality, accent, quirks)):
        raise InvalidInput("give at least one of description, personality, accent, or quirks")
    event = NpcUpdated(
        name=npc.name,
        description=npc.description
        if description is None
        else _text(description, "description", MAX_TEXT),
        personality=npc.personality
        if personality is None
        else _text(personality, "personality", MAX_NAME),
        accent=npc.accent if accent is None else _text(accent, "accent", MAX_NAME),
        quirks=list(npc.quirks) if quirks is None else _quirks(quirks) or [],
    )
    record(db, campaign_id, event)
    db.commit()
    return {"updated": "npc", "name": npc.name}


def delete_npc(db: Session, campaign_id: int, name: str) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    npc = _npc_named(state, name)
    record(db, campaign_id, NpcRemoved(name=npc.name))
    db.commit()
    return {"deleted": "npc", "name": npc.name}


def move_actor(db: Session, campaign_id: int, name: str, location: str) -> dict[str, str]:
    state = load_state(db, campaign_id)
    actor = _character_named(state, name)
    where = _place_named(state, location)
    record(db, campaign_id, ActorMoved(name=actor.name, location=where.name))
    db.commit()
    return {"moved": actor.name, "location": where.name}


def add_note(
    db: Session, campaign_id: int, text: str, subject: str | None = None
) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    text = _text(text, "text", MAX_TEXT)
    subject_name: str | None = None
    label = _optional_text(subject, "subject")
    if label is not None:
        entry = state.named(label)
        if entry is None:
            raise InvalidInput(f"there is no location or character named {label!r}")
        subject_name = entry.name
    note_id = record(db, campaign_id, NoteAdded(text=text, subject=subject_name))
    db.commit()
    return {
        "noted": True,
        "id": note_id,
        "subject": subject_name if subject_name is not None else CAMPAIGN_WIDE,
    }


def update_note(db: Session, campaign_id: int, note_id: int, text: str) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    _note_with_id(state, note_id)
    record(db, campaign_id, NoteUpdated(note_id=note_id, text=_text(text, "text", MAX_TEXT)))
    db.commit()
    return {"updated": "note", "id": note_id}


def delete_note(db: Session, campaign_id: int, note_id: int) -> dict[str, Any]:
    state = load_state(db, campaign_id)
    note = _note_with_id(state, note_id)
    record(db, campaign_id, NoteRemoved(note_id=note_id, text=note.text))
    db.commit()
    return {"deleted": "note", "id": note_id}


def search_notes(state: CampaignState, query: str) -> dict[str, list[dict[str, Any]]]:
    """Notes whose text or subject name contains every word of `query`, newest first."""
    words = _text(query, "query", MAX_TEXT).casefold().split()
    matches: list[dict[str, Any]] = []
    for note in reversed(state.notes):
        haystack = f"{note.subject} {note.text}".casefold()
        if all(word in haystack for word in words):
            matches.append({"id": note.id, "subject": note.subject, "text": note.text})
    return {"notes": matches}


def speaker_profile(state: CampaignState, name: str) -> str:
    """The NPC details a speech step needs. The NPC must be at the player character's location."""
    npc = _character_named(state, name)
    if npc.kind != NPC_KIND:
        raise InvalidInput(f"{npc.name} is the player character and does not need speaking for")
    pc = state.pc()
    if pc is None or npc.location != pc.location:
        raise InvalidInput(f"{npc.name} is not at the player character's location")
    quirks = "; ".join(npc.quirks) or "none"
    return "\n".join(
        [
            f"Speaker: {npc.name}",
            f"Who they are: {npc.description}",
            f"Personality: {npc.personality}",
            f"Accent: {npc.accent}",
            f"Quirks: {quirks}",
            f"Location: {npc.location}",
        ]
    )


def snapshot(state: CampaignState) -> WorldView:
    return WorldView(
        pc=state.pc(),
        locations=list(state.places.values()),
        npcs=[c for c in state.characters.values() if c.kind == NPC_KIND],
        notes=list(reversed(state.notes)),
    )


def world_state_text(state: CampaignState) -> str:
    """The current places and characters, for the GM's system prompt. Notes are searched, not sent."""
    view = snapshot(state)
    if view.pc is None:
        return ""
    lines = [
        f"Player character: {view.pc.name}, at {view.pc.location}.",
        "Locations:",
    ]
    lines += [f"- {place.name}: {place.description}" for place in view.locations]
    lines.append("NPCs:")
    if not view.npcs:
        lines.append("- none yet")
    for npc in view.npcs:
        lines.append(f"- {npc.name}, at {npc.location}: {npc.description}")
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


def _ensure_free_name(state: CampaignState, name: str) -> None:
    if state.named(name) is not None:
        raise InvalidInput(f"the name {name!r} is already used by a location or character")


def _place_named(state: CampaignState, name: str) -> Place:
    entry = state.named(name)
    if not isinstance(entry, Place):
        raise InvalidInput(f"there is no location named {name!r}")
    return entry


def _character_named(state: CampaignState, name: str) -> Character:
    entry = state.named(name)
    if not isinstance(entry, Character):
        raise InvalidInput(f"there is no character named {name!r}")
    return entry


def _npc_named(state: CampaignState, name: str) -> Character:
    npc = _character_named(state, name)
    if npc.kind != NPC_KIND:
        raise InvalidInput(f"{npc.name} is the player character, not an NPC")
    return npc


def _note_with_id(state: CampaignState, note_id: int) -> Note:
    note = next((n for n in state.notes if n.id == note_id), None)
    if note is None:
        raise InvalidInput(f"there is no note with id {note_id}")
    return note


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
