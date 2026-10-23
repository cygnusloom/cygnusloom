from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import (
    ObjectPlaced,
    ObjectTaken,
    decode,
    events_since,
    load_state,
)
from cygnusforge.services.objects import visible_objects
from tests.test_combat import CAIRN, PC, YARD, _character, _fight
from tests.test_inventory import CAPACITY, _fill, _used

CURRENCY = CAIRN.marketplace.currency


def _gold(app: FastAPI, campaign_id: int, name: str = PC) -> int:
    return _character(app, campaign_id, name).features.get(CURRENCY, 0)


def _visible(app: FastAPI, campaign_id: int, place: str = YARD) -> list[str]:
    with Session(app.state.engine) as db:
        state = load_state(db, campaign_id)
    return [o.contents for o in visible_objects(state, place)]


def _events(app: FastAPI, campaign_id: int, kind: type) -> list:
    with Session(app.state.engine) as db:
        return [decode(row) for row in events_since(db, campaign_id) if row.kind == kind.kind]


def test_placing_gold_and_an_item_shows_them_in_the_place(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = _fight(client_factory)
    tools.place_object(YARD, "a coin purse under a flagstone", False, "a test", gold=12)
    tools.place_object(YARD, "a brass lamp", False, "a test", item="Lantern")
    assert _visible(app, campaign_id) == ["a coin purse under a flagstone", "a brass lamp"]


def test_placing_an_object_records_an_event(client_factory: Callable[[], TestClient]):
    app, campaign_id, tools = _fight(client_factory)
    tools.place_object(YARD, "a coin purse", False, "a test", gold=5)
    [placed] = _events(app, campaign_id, ObjectPlaced)
    assert (placed.place, placed.contents, placed.gold, placed.hidden) == (
        YARD,
        "a coin purse",
        5,
        False,
    )


def test_taking_gold_pays_the_character_and_removes_the_object(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = _fight(client_factory)
    before = _gold(app, campaign_id)
    tools.place_object(YARD, "a coin purse", False, "a test", gold=7)
    result = tools.take_object("a coin purse", PC, "a test")
    assert result["gold"] == 7
    assert _gold(app, campaign_id) == before + 7
    assert _visible(app, campaign_id) == []


def test_taking_an_item_puts_it_in_the_inventory(client_factory: Callable[[], TestClient]):
    app, campaign_id, tools = _fight(client_factory)
    before = _used(app, campaign_id)
    tools.place_object(YARD, "a brass lamp", False, "a test", item="Lantern")
    tools.take_object("a brass lamp", PC, "a test")
    assert _used(app, campaign_id) == before + 1
    assert any(i.name == "Lantern" for i in _character(app, campaign_id, PC).items)
    assert _visible(app, campaign_id) == []


def test_taking_an_object_records_an_event(client_factory: Callable[[], TestClient]):
    app, campaign_id, tools = _fight(client_factory)
    tools.place_object(YARD, "a coin purse", False, "a test", gold=3)
    tools.take_object("a coin purse", PC, "a test")
    [taken] = _events(app, campaign_id, ObjectTaken)
    assert (taken.name, taken.contents) == (PC, "a coin purse")


def test_an_item_is_refused_when_the_inventory_is_full(client_factory: Callable[[], TestClient]):
    app, campaign_id, tools = _fight(client_factory)
    _fill(app, campaign_id, tools)
    assert _used(app, campaign_id) == CAPACITY
    tools.place_object(YARD, "a brass lamp", False, "a test", item="Lantern")
    with pytest.raises(InvalidInput, match="has room for 0 slots"):
        tools.take_object("a brass lamp", PC, "a test")
    assert _visible(app, campaign_id) == ["a brass lamp"]


def test_a_hidden_object_is_not_listed_and_cannot_be_taken(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = _fight(client_factory)
    before = _gold(app, campaign_id)
    tools.place_object(YARD, "a secret cache", True, "a test", gold=50)
    assert _visible(app, campaign_id) == []
    with pytest.raises(InvalidInput, match="there is nothing here"):
        tools.take_object("a secret cache", PC, "a test")
    assert _gold(app, campaign_id) == before


def test_a_hidden_object_event_does_not_render(client_factory: Callable[[], TestClient]):
    app, campaign_id, tools = _fight(client_factory)
    tools.place_object(YARD, "a secret cache", True, "a test", gold=50)
    [placed] = _events(app, campaign_id, ObjectPlaced)
    assert placed.render() is None


def test_taking_an_object_not_in_the_characters_place_is_refused(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = _fight(client_factory)
    tools.create_location("Far Hall", "A hall elsewhere.")
    tools.place_object("Far Hall", "a coin purse", False, "a test", gold=4)
    with pytest.raises(InvalidInput, match="there is nothing here"):
        tools.take_object("a coin purse", PC, "a test")
    assert _visible(app, campaign_id, "Far Hall") == ["a coin purse"]


def test_an_object_is_taken_by_its_short_name_once(client_factory: Callable[[], TestClient]):
    app, campaign_id, tools = _fight(client_factory)
    before = _gold(app, campaign_id)
    tools.place_object(
        YARD,
        "a cracked clay jar with nine gold coins inside",
        False,
        "a test",
        gold=9,
        name="clay jar",
    )
    result = tools.take_object("clay jar", PC, "a test")
    assert result["object"] == "clay jar"
    assert _gold(app, campaign_id) == before + 9
    assert _visible(app, campaign_id) == []
    with pytest.raises(InvalidInput, match="there is nothing here"):
        tools.take_object("clay jar", PC, "a test")
    assert _gold(app, campaign_id) == before + 9


def test_an_object_is_taken_by_a_part_of_its_short_name(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = _fight(client_factory)
    tools.place_object(YARD, "a brass lamp", False, "a test", item="Lantern", name="brass lamp")
    tools.take_object("lamp", PC, "a test")
    assert any(i.name == "Lantern" for i in _character(app, campaign_id, PC).items)
    assert _visible(app, campaign_id) == []


def test_an_object_is_taken_by_its_full_description(client_factory: Callable[[], TestClient]):
    app, campaign_id, tools = _fight(client_factory)
    tools.place_object(YARD, "a coin purse under a flagstone", False, "a test", gold=2)
    tools.take_object("a coin purse under a flagstone", PC, "a test")
    assert _visible(app, campaign_id) == []


def test_a_take_that_matches_nothing_lists_what_is_here(
    client_factory: Callable[[], TestClient],
):
    app, campaign_id, tools = _fight(client_factory)
    tools.place_object(YARD, "a coin purse", False, "a test", gold=2, name="purse")
    with pytest.raises(InvalidInput, match="Here .* can take: purse"):
        tools.take_object("dagger", PC, "a test")
    with pytest.raises(InvalidInput, match="there is no 'p' here"):
        tools.take_object("p", PC, "a test")
    assert _visible(app, campaign_id) == ["a coin purse"]


def test_an_object_needs_a_known_place(client_factory: Callable[[], TestClient]):
    _, _, tools = _fight(client_factory)
    with pytest.raises(InvalidInput, match="no place named"):
        tools.place_object("Nowhere", "a coin purse", False, "a test", gold=4)


def test_an_object_needs_a_description(client_factory: Callable[[], TestClient]):
    _, _, tools = _fight(client_factory)
    with pytest.raises(InvalidInput, match="needs a description"):
        tools.place_object(YARD, "   ", False, "a test", gold=4)


def test_gold_is_bounded(client_factory: Callable[[], TestClient]):
    _, _, tools = _fight(client_factory)
    with pytest.raises(InvalidInput):
        tools.place_object(YARD, "a heap", False, "a test", gold=-1)
