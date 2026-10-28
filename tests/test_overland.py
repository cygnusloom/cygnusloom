from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusforge.content.library import Wilderness
from cygnusforge.services import clock as clock_service
from cygnusforge.services import wilderness
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import load_state
from cygnusforge.services.wilderness import plan_overland
from tests.test_combat import CAIRN, PC
from tests.test_travel import HPP, PARTS, _place
from tests.test_wilderness_service import _fix_roll, _pc_items, _pc_tools
from tests.test_world import _db

# Spring weather rolls from wilderness.json: 1-2 Nice, 4 Unpleasant, 6 Extreme.
NICE = 1
UNPLEASANT = 4
EXTREME = 6


def _wilderness() -> Wilderness:
    assert CAIRN.wilderness is not None
    return CAIRN.wilderness


def _route(**overrides: object) -> dict[str, object]:
    route: dict[str, object] = {"path": "roads", "distance": "short", "terrain": "easy"}
    route.update(overrides)
    return route


def _absolute_hours(app: FastAPI, campaign_id: int) -> int:
    with _db(app) as db:
        clock = load_state(db, campaign_id).clock
    return clock_service.absolute(clock, PARTS, HPP)


def _fatigue_count(app: FastAPI, campaign_id: int) -> int:
    return sum(1 for item in _pc_items(app, campaign_id) if item.kind == "fatigue")


def _start_at(tools) -> None:
    _place(tools, "Start", 0, 0)
    _place(tools, "Far", 4, 0)
    tools.move_actor(PC, "Start", mode="in_dungeon")


def _force_lost(monkeypatch: pytest.MonkeyPatch, *results: bool) -> None:
    """Keep the real lost roll and its event, but force the outcome of each watch in order.
    Once the queue is empty every watch is not lost."""
    real = wilderness.roll_lost
    queue = list(results)

    def forced(db, campaign_id, path, rules, rng, **kwargs):
        rolled = real(db, campaign_id, path, rules, rng, **kwargs)
        return {**rolled, "lost": queue.pop(0) if queue else False}

    monkeypatch.setattr(wilderness, "roll_lost", forced)


def test_a_road_route_in_nice_weather_takes_one_watch(
    client_factory: Callable[[], TestClient], monkeypatch: pytest.MonkeyPatch
):
    _fix_roll(monkeypatch, NICE)
    app, campaign_id, tools = _pc_tools(client_factory)
    _start_at(tools)
    before = _absolute_hours(app, campaign_id)
    result = tools.move_actor(PC, "Far", mode="overland", route=_route())
    assert result["location"] == "Far"
    assert result["watches"] == 1
    assert result["lost_watches"] == 0
    assert len(result["journey"]) == 1
    assert _absolute_hours(app, campaign_id) - before == HPP


def test_each_watch_of_travel_moves_the_clock_one_part(
    client_factory: Callable[[], TestClient], monkeypatch: pytest.MonkeyPatch
):
    _fix_roll(monkeypatch, NICE)
    _force_lost(monkeypatch)
    app, campaign_id, tools = _pc_tools(client_factory)
    _start_at(tools)
    before = _absolute_hours(app, campaign_id)
    result = tools.move_actor(
        PC, "Far", mode="overland", route=_route(path="trails", distance="medium")
    )
    assert result["watches"] == 3
    assert _absolute_hours(app, campaign_id) - before == 3 * HPP


def test_a_lost_watch_is_followed_by_a_recovery_watch_with_no_progress(
    client_factory: Callable[[], TestClient], monkeypatch: pytest.MonkeyPatch
):
    _fix_roll(monkeypatch, NICE)
    _force_lost(monkeypatch, True, False)
    app, campaign_id, tools = _pc_tools(client_factory)
    _start_at(tools)
    before = _absolute_hours(app, campaign_id)
    result = tools.move_actor(
        PC, "Far", mode="overland", route=_route(path="trails", distance="medium")
    )
    assert [watch["progress"] for watch in result["journey"]] == [True, False, True, True]
    assert result["lost_watches"] == 1
    assert len(result["journey"]) == result["watches"] + result["lost_watches"]
    assert _absolute_hours(app, campaign_id) - before == len(result["journey"]) * HPP


def test_a_long_wilderness_route_adds_distance_vast_and_terrain(
    client_factory: Callable[[], TestClient], monkeypatch: pytest.MonkeyPatch
):
    _fix_roll(monkeypatch, NICE)
    _force_lost(monkeypatch)
    _app, _campaign_id, tools = _pc_tools(client_factory)
    _start_at(tools)
    result = tools.move_actor(
        PC,
        "Far",
        mode="overland",
        route=_route(path="wilderness", distance="long", terrain="perilous", vast=1),
    )
    assert result["terrain"] == "perilous"
    # wilderness 2 + long 3 + vast 1 + perilous 2
    assert result["watches"] == 8


def test_deprivation_makes_the_ground_one_step_harder():
    route = _route(path="trails", distance="medium")
    assert plan_overland(_wilderness(), route, "Nice", False).watches == 3
    deprived = plan_overland(_wilderness(), route, "Nice", True)
    assert deprived.terrain == "tough"
    # trails 1 + medium 2 + tough 1
    assert deprived.watches == 4


def test_an_unpleasant_day_needs_a_weather_choice(
    client_factory: Callable[[], TestClient], monkeypatch: pytest.MonkeyPatch
):
    _fix_roll(monkeypatch, UNPLEASANT, NICE)
    _app, _campaign_id, tools = _pc_tools(client_factory)
    _start_at(tools)
    with pytest.raises(InvalidInput, match="choose weather_choice"):
        tools.move_actor(PC, "Far", mode="overland", route=_route())


def test_fatigue_as_the_weather_choice_costs_fatigue_and_no_watch(
    client_factory: Callable[[], TestClient], monkeypatch: pytest.MonkeyPatch
):
    _fix_roll(monkeypatch, UNPLEASANT, NICE)
    _force_lost(monkeypatch)
    app, campaign_id, tools = _pc_tools(client_factory)
    _start_at(tools)
    before = _fatigue_count(app, campaign_id)
    result = tools.move_actor(PC, "Far", mode="overland", route=_route(weather_choice="fatigue"))
    assert result["watches"] == 1
    assert result["fatigue"] == 1
    assert _fatigue_count(app, campaign_id) == before + 1


def test_watch_as_the_weather_choice_costs_a_watch_and_no_fatigue(
    client_factory: Callable[[], TestClient], monkeypatch: pytest.MonkeyPatch
):
    _fix_roll(monkeypatch, UNPLEASANT, NICE)
    _force_lost(monkeypatch)
    app, campaign_id, tools = _pc_tools(client_factory)
    _start_at(tools)
    before = _fatigue_count(app, campaign_id)
    result = tools.move_actor(PC, "Far", mode="overland", route=_route(weather_choice="watch"))
    assert result["watches"] == 2
    assert result["fatigue"] == 0
    assert _fatigue_count(app, campaign_id) == before


def test_extreme_weather_costs_fatigue_a_watch_and_a_step_with_no_choice(
    client_factory: Callable[[], TestClient], monkeypatch: pytest.MonkeyPatch
):
    _fix_roll(monkeypatch, EXTREME, NICE)
    _force_lost(monkeypatch)
    _app, _campaign_id, tools = _pc_tools(client_factory)
    _start_at(tools)
    result = tools.move_actor(PC, "Far", mode="overland", route=_route())
    assert result["fatigue"] == 1
    assert result["terrain"] == "tough"
    # extreme 1 + short 1 + tough 1
    assert result["watches"] == 3


def test_an_overland_move_without_a_route_is_refused(client_factory: Callable[[], TestClient]):
    _app, _campaign_id, tools = _pc_tools(client_factory)
    _start_at(tools)
    with pytest.raises(InvalidInput, match="needs a route"):
        tools.move_actor(PC, "Far", mode="overland")


def test_an_unknown_route_key_or_value_is_refused(client_factory: Callable[[], TestClient]):
    _app, _campaign_id, tools = _pc_tools(client_factory)
    _start_at(tools)
    with pytest.raises(InvalidInput, match="no path named 'lanes'"):
        tools.move_actor(PC, "Far", mode="overland", route=_route(path="lanes"))
    with pytest.raises(InvalidInput, match="has no speed"):
        tools.move_actor(PC, "Far", mode="overland", route={**_route(), "speed": "fast"})


def test_a_vast_route_beyond_the_maximum_is_refused():
    with pytest.raises(InvalidInput, match="vast must be a whole number from 0 to 2"):
        plan_overland(_wilderness(), _route(vast=3), "Nice", False)


def test_catastrophic_weather_refuses_overland_travel():
    with pytest.raises(InvalidInput, match="no overland travel is possible in Catastrophic"):
        plan_overland(_wilderness(), _route(), "Catastrophic", False)


def test_a_refused_overland_move_leaves_the_place_and_clock_alone(
    client_factory: Callable[[], TestClient], monkeypatch: pytest.MonkeyPatch
):
    _fix_roll(monkeypatch, UNPLEASANT, NICE)
    app, campaign_id, tools = _pc_tools(client_factory)
    _start_at(tools)
    before = _absolute_hours(app, campaign_id)
    with pytest.raises(InvalidInput):
        tools.move_actor(PC, "Far", mode="overland", route=_route())
    assert _absolute_hours(app, campaign_id) == before


def test_an_unknown_move_mode_is_refused(client_factory: Callable[[], TestClient]):
    _app, _campaign_id, tools = _pc_tools(client_factory)
    _start_at(tools)
    with pytest.raises(InvalidInput, match="no move mode named 'sprint'"):
        tools.move_actor(PC, "Far", mode="sprint")


def test_a_dungeon_move_takes_no_clock_time(client_factory: Callable[[], TestClient]):
    app, campaign_id, tools = _pc_tools(client_factory)
    _start_at(tools)
    before = _absolute_hours(app, campaign_id)
    result = tools.move_actor(PC, "Far", mode="in_dungeon")
    assert result["location"] == "Far"
    assert _absolute_hours(app, campaign_id) == before


def _twin_lakes_pc_at(tools, place: str) -> None:
    """Put the PC in a Twin Lakes place by an ordinary in-settlement move."""
    tools.move_actor(PC, place)


def test_an_authored_route_is_used_when_the_gm_gives_no_route(
    client_factory: Callable[[], TestClient], monkeypatch: pytest.MonkeyPatch
):
    _fix_roll(monkeypatch, NICE)
    _force_lost(monkeypatch)
    app, campaign_id, tools = _pc_tools(client_factory)
    _twin_lakes_pc_at(tools, "Isthmus Town")
    before = _absolute_hours(app, campaign_id)
    result = tools.move_actor(PC, "Dunston", mode="overland", route=None)
    assert result["location"] == "Dunston"
    # trails 1 + short 1 + easy 0
    assert result["watches"] == 2
    assert result["terrain"] == "easy"
    assert _absolute_hours(app, campaign_id) - before == 2 * HPP


def test_an_authored_route_applies_from_a_place_inside_another(
    client_factory: Callable[[], TestClient], monkeypatch: pytest.MonkeyPatch
):
    _fix_roll(monkeypatch, NICE)
    _force_lost(monkeypatch)
    _app, _campaign_id, tools = _pc_tools(client_factory)
    _twin_lakes_pc_at(tools, "Isthmus Town")
    _twin_lakes_pc_at(tools, "The Stuffed Inn")
    result = tools.move_actor(PC, "Dunston", mode="overland", route=None)
    assert result["location"] == "Dunston"
    assert result["watches"] == 2


def test_an_authored_route_overrides_the_gm_route_but_keeps_the_weather_choice(
    client_factory: Callable[[], TestClient], monkeypatch: pytest.MonkeyPatch
):
    _fix_roll(monkeypatch, UNPLEASANT, NICE)
    _force_lost(monkeypatch)
    app, campaign_id, tools = _pc_tools(client_factory)
    _twin_lakes_pc_at(tools, "Isthmus Town")
    gm_route = _route(path="wilderness", distance="long", terrain="perilous", vast=2)
    result = tools.move_actor(
        PC,
        "Dunston",
        mode="overland",
        route={**gm_route, "weather_choice": "watch"},
    )
    assert result["terrain"] == "easy"
    # The authored trails 1 + short 1 + easy 0, plus one watch for the chosen watch in bad weather.
    assert result["watches"] == 3
    assert result["fatigue"] == 0
    assert _fatigue_count(app, campaign_id) == 0
