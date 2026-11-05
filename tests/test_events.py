from __future__ import annotations

import json
from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.db.models import Campaign, Event
from cygnusloom.services.campaigns import campaign_log, list_messages
from cygnusloom.services.events import (
    EVENT_TYPES,
    Channel,
    ChatPlayer,
    GameEvent,
    LocationAdded,
    NoteAdded,
    NpcAdded,
    Place,
    ResourceChanged,
    StatChanged,
    decode,
    last_event_id,
    load_state,
    record,
    truncate,
)
from tests.test_campaigns import _campaign_id, _send


def test_generic_payloads_apply_to_character_state():
    from cygnusloom.services.events import CampaignState, Character, ConditionChanged

    state = CampaignState()
    state.characters["hero"] = Character(
        kind="pc",
        name="Hero",
        location=None,
        description="",
        personality="",
        accent="",
        quirks=[],
        stats={"hp": 10, "str": 10},
        counters={"xp": 0},
    )
    StatChanged(name="Hero", stat="hp", before=10, after=7, cause="a test").apply(state, 1)
    ResourceChanged(name="Hero", resource="xp", before=0, after=3, cause="a test").apply(state, 2)
    ConditionChanged(name="Hero", condition="prone", active=True, cause="a test").apply(state, 3)
    hero = state.characters["hero"]
    assert hero.harm["hp"] == 3
    assert hero.counters["xp"] == 3
    assert hero.conditions == ["prone"]


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


def test_npc_pronouns_round_trip_and_old_events_load_without_them(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    event = NpcAdded(
        name="Pell",
        location="Test Ledge",
        description="A ferryman.",
        personality="Calm",
        accent="Soft",
        quirks=["hums"],
        pronouns="he/him",
    )
    with Session(app.state.engine) as db:
        record(db, campaign_id, LocationAdded(name="Test Ledge", description="A ledge."))
        event_id = record(db, campaign_id, event)
        db.commit()
    with Session(app.state.engine) as db:
        row = db.get(Event, event_id)
    assert decode(row) == event

    legacy = json.loads(row.payload)
    legacy.pop("pronouns")
    row.payload = json.dumps(legacy)
    assert decode(row).pronouns is None


def test_a_player_message_keeps_the_character_it_spoke_for(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    with Session(app.state.engine) as db:
        spoken = record(db, campaign_id, ChatPlayer(content="Lead on.", speaker="Bram"))
        legacy = record(db, campaign_id, ChatPlayer(content="Hello."))
        db.commit()
    with Session(app.state.engine) as db:
        assert decode(db.get(Event, spoken)).speaker == "Bram"
        row = db.get(Event, legacy)
        payload = json.loads(row.payload)
        payload.pop("speaker")
        row.payload = json.dumps(payload)
        db.commit()
    with Session(app.state.engine) as db:
        assert decode(db.get(Event, legacy)).speaker is None
    messages = {m.id: m for m in list_messages(app.state.engine, campaign_id)}
    assert messages[spoken].speaker == "Bram"
    assert messages[legacy].speaker is None


def test_truncate_removes_later_events_and_the_state_follows(
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
        state = load_state(db, campaign_id)
        assert state.named("Test Ledge") is not None
        assert state.named("Far Ledge") is None
        assert state.notes == []
        assert last_event_id(db, campaign_id) == mark


def test_state_before_an_event_folds_only_the_events_before_it(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    with Session(app.state.engine) as db:
        record(db, campaign_id, LocationAdded(name="Test Ledge", description="A ledge."))
        record(db, campaign_id, NoteAdded(text="A bird nests here.", subject="Test Ledge"))
        later = record(db, campaign_id, LocationAdded(name="Far Ledge", description="Further."))
        db.commit()

    with Session(app.state.engine) as db:
        before = load_state(db, campaign_id, before=later)
        now = load_state(db, campaign_id)

    assert before.named("Far Ledge") is None
    assert [note.text for note in before.notes] == ["A bird nests here."]
    assert now.named("Far Ledge") is not None
    assert [note.text for note in now.notes] == ["A bird nests here."]


def test_named_entries_are_found_regardless_of_case(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    with Session(app.state.engine) as db:
        record(db, campaign_id, LocationAdded(name="Test Ledge", description="A ledge."))
        db.commit()
    with Session(app.state.engine) as db:
        state = load_state(db, campaign_id)
    entry = state.named("  test LEDGE ")
    assert isinstance(entry, Place)
    assert entry.name == "Test Ledge"
    assert state.named("Nowhere") is None


def test_chat_is_in_the_transcript_and_not_in_the_log(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "Hello, is anyone there?")

    with Session(app.state.engine) as db:
        owner_id = db.get(Campaign, campaign_id).owner_id
    log = campaign_log(app.state.engine, owner_id, campaign_id)
    assert not any("Hello, is anyone there?" in line for line in log)
    assert list_messages(app.state.engine, campaign_id)[1].content == "Hello, is anyone there?"
