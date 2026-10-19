from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusforge.db.models import Campaign, Event, Location, Note
from cygnusforge.services.campaigns import campaign_log, list_messages
from cygnusforge.services.events import (
    EVENT_TYPES,
    Channel,
    GameEvent,
    LocationAdded,
    NoteAdded,
    NpcAdded,
    decode,
    last_event_id,
    named_entry,
    rebuild,
    record,
    truncate,
)
from tests.test_campaigns import _campaign_id, _send


def test_a_kind_is_required():
    with pytest.raises(TypeError, match="must set a kind"):

        class Unkinded(GameEvent):
            pass


def test_kinds_are_unique():
    with pytest.raises(TypeError, match="already registered"):

        class Duplicate(GameEvent):
            kind = "location.added"

    assert EVENT_TYPES["location.added"] is LocationAdded


def test_recorded_events_decode_to_what_was_recorded(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    event = NpcAdded(
        name="Pell",
        location="Test Ledge",
        description="A tall ferryman.",
        personality="Calm",
        accent="Soft",
        quirks=["hums", "counts coins"],
    )
    with Session(app.state.engine) as db:
        record(db, campaign_id, LocationAdded(name="Test Ledge", description="A ledge."))
        event_id = record(db, campaign_id, event)
        db.commit()
    with Session(app.state.engine) as db:
        row = db.get(Event, event_id)

    assert decode(row) == event
    assert row.channel == Channel.LOG


def test_truncate_removes_later_events_and_rebuilds_the_projections(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    with Session(app.state.engine) as db:
        record(db, campaign_id, LocationAdded(name="Test Ledge", description="A ledge."))
        mark = last_event_id(db, campaign_id)
        record(db, campaign_id, LocationAdded(name="Far Ledge", description="Further out."))
        record(db, campaign_id, NoteAdded(text="Check the ropes.", subject="Far Ledge"))
        db.commit()

    with Session(app.state.engine) as db:
        truncate(db, campaign_id, mark)
        db.commit()

    with Session(app.state.engine) as db:
        names = {
            row.name for row in db.exec(select(Location).where(Location.campaign_id == campaign_id))
        }
        notes = db.exec(select(Note).where(Note.campaign_id == campaign_id)).all()
        assert "Test Ledge" in names
        assert "Far Ledge" not in names
        assert notes == []
        assert last_event_id(db, campaign_id) == mark


def test_rebuild_replays_the_log_to_the_same_projections(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    with Session(app.state.engine) as db:
        record(db, campaign_id, LocationAdded(name="Test Ledge", description="A ledge."))
        record(db, campaign_id, NoteAdded(text="A bird nests here.", subject="Test Ledge"))
        db.commit()
    with Session(app.state.engine) as db:
        before = {
            (row.name, row.description)
            for row in db.exec(select(Location).where(Location.campaign_id == campaign_id))
        }
        rebuild(db, campaign_id)
        db.commit()
    with Session(app.state.engine) as db:
        after = {
            (row.name, row.description)
            for row in db.exec(select(Location).where(Location.campaign_id == campaign_id))
        }
        notes = db.exec(select(Note).where(Note.campaign_id == campaign_id)).all()

    assert after == before
    assert [note.text for note in notes] == ["A bird nests here."]


def test_named_entries_are_found_regardless_of_case(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    with Session(app.state.engine) as db:
        record(db, campaign_id, LocationAdded(name="Test Ledge", description="A ledge."))
        db.commit()
    with Session(app.state.engine) as db:
        entry = named_entry(db, campaign_id, "  test LEDGE ")
        assert isinstance(entry, Location)
        assert entry.name == "Test Ledge"
        assert named_entry(db, campaign_id, "Nowhere") is None


def test_chat_is_in_the_transcript_and_not_in_the_log(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "Hello, is anyone there?")

    with Session(app.state.engine) as db:
        owner_id = db.get(Campaign, campaign_id).owner_id
    log = campaign_log(app.state.engine, owner_id, campaign_id)
    assert not any("Hello, is anyone there?" in line for line in log)
    assert list_messages(app.state.engine, campaign_id)[0].content == "Hello, is anyone there?"
