from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusforge.db.models import Campaign
from cygnusforge.services.campaigns import campaign_transcript
from cygnusforge.services.events import (
    EVENT_TYPES,
    HIDDEN_KINDS,
    LocationAdded,
    LocationDiscovered,
    NoteAdded,
    is_hidden,
    record,
)
from tests.test_campaigns import _campaign_id


def test_every_event_kind_is_classified_once():
    assert set(HIDDEN_KINDS) == set(EVENT_TYPES)


def test_gm_bookkeeping_is_hidden_and_what_the_pc_perceives_is_visible():
    assert is_hidden("note.added")
    assert is_hidden("turn.state_step")
    assert is_hidden("npc.added")
    assert is_hidden("combat.debug_stat_set")
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


def test_the_show_hidden_checkbox_sits_in_the_model_settings_dialog(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _campaign_id(client_factory)
    page = client.get(f"/campaigns/{campaign_id}").text

    dialog_start = page.index('<dialog id="dialog-model"')
    dialog_end = page.index("</dialog>", dialog_start)
    checkbox = page.index("data-show-hidden>")
    assert dialog_start < checkbox < dialog_end


def test_the_stylesheet_hides_hidden_lines_until_the_checkbox_shows_them(
    client_factory: Callable[[], TestClient],
):
    client, _ = _campaign_id(client_factory)
    css = client.get("/static/style.css").text
    assert ".transcript:not(.show-hidden) .hidden-event { display: none; }" in css
