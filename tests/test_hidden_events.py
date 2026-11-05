from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.db.models import Campaign
from cygnusloom.services.campaigns import campaign_transcript
from cygnusloom.services.events import (
    EVENT_TYPES,
    GameEvent,
    LocationAdded,
    LocationDiscovered,
    NoteAdded,
    is_hidden,
    record,
)
from tests.test_campaigns import _campaign_id


def test_every_registered_event_kind_has_a_visibility_decision():
    for kind, event_type in EVENT_TYPES.items():
        assert event_type.kind == kind
        assert isinstance(event_type.hidden, bool)


def test_is_hidden_reads_the_visibility_from_the_event_class():
    for kind, event_type in EVENT_TYPES.items():
        assert is_hidden(kind) is event_type.hidden


def test_an_unknown_event_kind_raises():
    with pytest.raises(KeyError):
        is_hidden("event.does_not_exist")


def test_a_new_event_with_no_visibility_choice_defaults_to_visible():
    @dataclass(frozen=True)
    class NewEvent(GameEvent):
        kind: ClassVar[str] = "test.default_visibility"
        detail: str = ""

    try:
        assert NewEvent.hidden is False
        assert is_hidden("test.default_visibility") is False
    finally:
        del EVENT_TYPES["test.default_visibility"]


def test_gm_bookkeeping_is_hidden_and_what_the_pc_perceives_is_visible():
    assert EVENT_TYPES["npc.known"].hidden is True
    assert is_hidden("note.added")
    assert is_hidden("turn.state_step")
    assert is_hidden("npc.added")
    assert is_hidden("npc.known")
    assert is_hidden("combat.debug_stat_set")
    assert is_hidden("debug.injection_set")
    assert is_hidden("debug.injection_cleared")
    assert is_hidden("debug.injection_ran")
    assert is_hidden("narration.undone")
    assert is_hidden("turn.review_step")
    assert EVENT_TYPES["chat.gm"].hidden is False
    assert not is_hidden("chat.gm")
    assert not is_hidden("chat.player")
    assert not is_hidden("location.discovered")
    assert not is_hidden("actor.moved")
    assert not is_hidden("tool.ran")


def test_the_transcript_flags_hidden_log_lines(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = _campaign_id(client_factory)
    with Session(app.state.engine) as db:
        record(db, campaign_id, LocationAdded(name="Far Ledge", description="A ledge."))
        record(db, campaign_id, LocationDiscovered(name="Far Ledge"))
        record(db, campaign_id, NoteAdded(text="Check the ropes.", subject="Far Ledge"))
        db.commit()
        owner_id = db.get(Campaign, campaign_id).owner_id

    flags = {
        item.content: item.hidden
        for item in campaign_transcript(app.state.engine, owner_id, campaign_id)
        if item.role == "log"
    }
    assert flags["Discovered: Far Ledge."] is False
    assert flags["Note about Far Ledge: Check the ropes."] is True


def test_hidden_lines_are_marked_in_the_page_and_the_checkbox_is_unchecked(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    with Session(app.state.engine) as db:
        record(db, campaign_id, LocationAdded(name="Far Ledge", description="A ledge."))
        record(db, campaign_id, LocationDiscovered(name="Far Ledge"))
        record(db, campaign_id, NoteAdded(text="Check the ropes.", subject="Far Ledge"))
        db.commit()

    page = client.get(f"/campaigns/{campaign_id}").text

    assert '<input type="checkbox" data-show-hidden> Show hidden events' in page
    assert '<div class="log-line">Discovered: Far Ledge.</div>' in page
    assert '<div class="log-line hidden-event">Note about Far Ledge: Check the ropes.</div>' in page
    assert "data-transcript" in page
    assert 'data-gm-state="waiting"' in page


def test_the_show_hidden_checkbox_sits_in_the_settings_dialog(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    page = client.get(f"/campaigns/{campaign_id}").text

    dialog_start = page.index('<dialog id="dialog-settings"')
    dialog_end = page.index("</dialog>", dialog_start)
    checkbox = page.index("data-show-hidden>")
    assert dialog_start < checkbox < dialog_end


def test_the_stylesheet_hides_hidden_lines_until_the_checkbox_shows_them(
    client_factory: Callable[[], TestClient],
):
    client, _ = _campaign_id(client_factory)
    css = client.get("/static/style.css").text
    assert ".transcript:not(.show-hidden) .hidden-event { display: none; }" in css


def test_the_show_hidden_checkbox_starts_unchecked_and_is_never_stored(
    client_factory: Callable[[], TestClient],
):
    client, _ = _campaign_id(client_factory)
    script = client.get("/static/turn_stream.js").text
    assert "showHidden.checked = false;" in script
    assert "localStorage" not in script
    assert "sessionStorage.setItem(SHOW" not in script
