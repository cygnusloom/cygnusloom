"""Tests for generic timers: item use starts one, the clock ends it, and the GM can start or end
one directly."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.services.events import TimerStarted, decode, events_since, load_state
from tests.test_combat import PC, _fight
from tests.test_wwn_combat import PC as WWN_PC
from tests.test_wwn_combat import WWN, _wwn_campaign


def _timers(app, campaign_id: int, name: str = PC) -> dict[str, object]:
    with Session(app.state.engine) as db:
        character = load_state(db, campaign_id).characters[name.casefold()]
    return character.timers


def test_using_a_cairn_torch_starts_a_light_timer(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id, tools = _fight(client_factory, rolled=False)
    tools.give_item(PC, "Torch")

    result = tools.use_item(PC, "Torch")

    assert result["timer"]["item"] == "Torch"
    timer = _timers(app, campaign_id)["torch"]
    assert (timer.label, timer.until_day) == ("Light", 1)
    assert timer.until_text.startswith("Day 1, ")


def test_a_redundant_start_for_the_same_ending_is_ignored(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id, tools = _fight(client_factory, rolled=False)
    tools.give_item(PC, "Torch")
    tools.use_item(PC, "Torch")

    again = tools.set_timer(PC, "Torch", hours=1, label="torch burning")

    assert again["already_running"] is True
    assert _timers(app, campaign_id)["torch"].label == "Light"
    with Session(app.state.engine) as db:
        started = [
            decode(row) for row in events_since(db, campaign_id) if row.kind == TimerStarted.kind
        ]
    assert len(started) == 1


def test_the_clock_ends_an_expired_timer(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id, tools = _fight(client_factory, rolled=False)
    tools.give_item(PC, "Torch")
    tools.use_item(PC, "Torch")

    clock = tools.advance_time(hours=1)

    assert clock["timers_ended"] == [{"character": PC, "item": "Torch", "label": "Light"}]
    assert _timers(app, campaign_id) == {}


def test_a_shop_item_timer_starts_without_a_matching_feature(
    client_factory: Callable[[], TestClient],
) -> None:
    client, campaign_id, tools = _wwn_campaign(client_factory)
    tools = replace(tools, marketplace=WWN.marketplace)
    tools.give_item(WWN_PC, "Torch")
    tools.ready_item(WWN_PC, "Torch")

    result = tools.use_item(WWN_PC, "Torch")

    assert result["timer"]["item"] == "Torch"
    timer = _timers(client.app, campaign_id, WWN_PC)["torch"]
    assert timer.label == "Light"


def test_the_gm_can_start_and_end_a_timer(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id, tools = _fight(client_factory, rolled=False)

    started = tools.set_timer(PC, "Poison", hours=3, label="Poisoned")
    timer = _timers(app, campaign_id)["poison"]
    assert (started["hours"], timer.label) == (3, "Poisoned")

    ended = tools.set_timer(PC, "Poisoned", action="end")

    assert ended["ended"] is True
    assert _timers(app, campaign_id) == {}


def test_forced_end_of_a_missing_timer_notes_that_it_was_absent(
    client_factory: Callable[[], TestClient],
) -> None:
    _app, _campaign_id, tools = _fight(client_factory, rolled=False)

    result = tools.set_timer(PC, "Poison", action="end", force=True)

    assert result == {"character": PC, "ended": False, "note": "no timer 'Poison'"}
