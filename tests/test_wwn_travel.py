"""Worlds Without Number overland travel, using the book's ten-hour march, terrain speeds,
supplies, foraging, and privation rules. The campaign and character helpers come from
``tests/test_wwn_advancement.py``."""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.content.library import JourneyEvent
from cygnusloom.services import clock as clock_service
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import (
    ActorMoved,
    ClockAdvanced,
    RollMade,
    TablesRolled,
    decode,
    events_since,
    last_event_id,
    load_state,
    name_key,
)
from tests.test_travel import _place
from tests.test_wwn_advancement import NAME, WWN, _campaign, _world_tools

PARTS = len(WWN.clock.parts)
HPP = WWN.clock.hours_per_part


def _hours(app: FastAPI, campaign_id: int) -> int:
    with Session(app.state.engine) as db:
        clock = load_state(db, campaign_id).clock
    return clock_service.absolute(clock, PARTS, HPP)


def _location(app: FastAPI, campaign_id: int) -> str | None:
    with Session(app.state.engine) as db:
        return load_state(db, campaign_id).characters[name_key(NAME)].location


def _setup(client_factory: Callable[[], TestClient]):
    app, campaign_id = _campaign(client_factory)
    tools = _world_tools(app, campaign_id)
    _place(tools, "Start")
    _place(tools, "Far")
    tools.move_actor(NAME, "Start")
    return app, campaign_id, tools


def test_wwn_offers_the_source_travel_tool() -> None:
    call = WWN.tool_calls["travel"]
    assert call.world_method == "travel"
    assert call.function is None
    assert call.takes_context is False
    properties = call.parameters["properties"]
    assert properties["terrain"]["enum"] == [
        "plains",
        "light_forest",
        "desert",
        "dense_forest",
        "rugged_hills",
        "swamp",
        "mountains",
        "broken_highland",
    ]
    assert properties["weather"]["enum"] == ["clear", "foul", "deep_snow"]


def test_wwn_overland_travel_is_terrain_based() -> None:
    assert WWN.capabilities.travel == "terrain"


def test_wwn_travel_uses_the_source_speeds_and_advances_ten_hours_a_day(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id, tools = _setup(client_factory)
    before = _hours(app, campaign_id)

    result = tools.travel(
        NAME,
        "Far",
        "plains",
        road=True,
        weather="clear",
        days=2,
        travelers=3,
        reason="the king's road",
    )

    assert result["speed_mph"] == 6.0
    assert result["miles_per_day"] == 60.0
    assert result["miles"] == 120.0
    assert result["hours"] == 20
    assert result["supplies"]["food"] == 6
    assert result["supplies"]["water"] == 6
    assert result["supplies"]["firewood"] == 8
    assert result["forage"]["takes"] == "half a day"
    assert result["forage"]["check"] == "Wis/Survive"
    assert result["privation"]["water"]
    assert result["move"]["location"] == "Far"
    assert _location(app, campaign_id) == "Far"
    assert _hours(app, campaign_id) - before == 20


@pytest.mark.parametrize(
    ("terrain", "road", "weather", "speed", "miles_per_day"),
    [
        ("plains", False, "clear", 3.0, 30.0),
        ("light_forest", False, "foul", 1.0, 10.0),
        ("mountains", False, "deep_snow", 0.05, 0.5),
        ("swamp", True, "clear", 2.0, 20.0),
    ],
)
def test_wwn_travel_applies_road_and_weather_modifiers(
    client_factory: Callable[[], TestClient],
    terrain: str,
    road: bool,
    weather: str,
    speed: float,
    miles_per_day: float,
) -> None:
    _, _, tools = _setup(client_factory)

    result = tools.travel(
        NAME,
        "Far",
        terrain,
        road=road,
        weather=weather,
        days=1,
    )

    assert result["speed_mph"] == speed
    assert result["miles_per_day"] == miles_per_day


@pytest.mark.parametrize(
    ("terrain", "difficulty"),
    [
        ("dense_forest", 8),
        ("plains", 9),
        ("desert", 12),
        ("broken_highland", 14),
    ],
)
def test_wwn_foraging_uses_the_source_difficulties(
    client_factory: Callable[[], TestClient], terrain: str, difficulty: int
) -> None:
    _, _, tools = _setup(client_factory)

    result = tools.travel(NAME, "Far", terrain)

    assert result["forage"]["difficulty"] == difficulty


def test_wwn_travel_refuses_bad_routes_and_weather(
    client_factory: Callable[[], TestClient],
) -> None:
    _, _, tools = _setup(client_factory)

    with pytest.raises(InvalidInput, match="terrain"):
        tools.travel(NAME, "Far", "moon")
    with pytest.raises(InvalidInput, match="weather"):
        tools.travel(NAME, "Far", "plains", weather="storm")
    with pytest.raises(InvalidInput, match="days"):
        tools.travel(NAME, "Far", "plains", days=0)
    with pytest.raises(InvalidInput, match="travelers"):
        tools.travel(NAME, "Far", "plains", travelers=0)


def test_overland_move_falls_back_when_a_rule_set_has_no_wilderness(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id, tools = _setup(client_factory)

    result = tools.move_actor(NAME, "Far", mode="overland")

    assert result["location"] == "Far"
    assert "does not track wilderness" in result["note"]
    assert _location(app, campaign_id) == "Far"


class _NoDraws(random.Random):
    """An RNG that fails the test if anything draws from it."""

    def randint(self, low: int, high: int) -> int:
        raise AssertionError("no journey event is configured, so no die should be rolled")


class _ScriptedRandom(random.Random):
    """An RNG that returns scripted die results, and fails if too many are asked for."""

    def __init__(self, *values: int):
        super().__init__(0)
        self.values = list(values)

    def randint(self, low: int, high: int) -> int:
        value = self.values.pop(0)
        assert low <= value <= high, f"a die of {low} to {high} cannot show {value}"
        return value


def _events_after(app: FastAPI, campaign_id: int, after: int) -> list[Any]:
    with Session(app.state.engine) as db:
        return [decode(row) for row in events_since(db, campaign_id, after)]


def _mark(app: FastAPI, campaign_id: int) -> int:
    with Session(app.state.engine) as db:
        return last_event_id(db, campaign_id)


def _event_tools(tools: Any, event: JourneyEvent, rng: random.Random) -> Any:
    assert WWN.journey is not None
    return replace(tools, journey=replace(WWN.journey, events=(event,)), rng=rng)


def test_wwn_travel_moves_then_advances_the_clock_once_without_drawing(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id, tools = _setup(client_factory)
    mark = _mark(app, campaign_id)

    result = replace(tools, rng=_NoDraws(0)).travel(NAME, "Far", "plains", days=2)

    assert list(result) == [
        "character",
        "destination",
        "terrain",
        "road",
        "weather",
        "days",
        "travelers",
        "reason",
        "speed_mph",
        "miles_per_day",
        "miles",
        "hours",
        "move",
        "clock",
        "supplies",
        "forage",
        "privation",
        "events",
    ]
    assert result["events"] == []
    assert [event.kind for event in _events_after(app, campaign_id, mark)] == [
        ActorMoved.kind,
        ClockAdvanced.kind,
    ]


def test_a_day_event_rolls_once_per_matching_travel_day(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id, tools = _setup(client_factory)
    event = JourneyEvent(
        on="day",
        table="dungeon_event",
        terrain=("plains",),
        weather=("clear",),
        reason="an ambush on the road",
    )
    rng = _ScriptedRandom(5, 5, 5)
    mark = _mark(app, campaign_id)

    result = _event_tools(tools, event, rng).travel(NAME, "Far", "plains", days=3)

    assert len(result["events"]) == 3
    assert [entry["table"] for entry in result["events"]] == ["dungeon_event"] * 3
    assert [entry["reason"] for entry in result["events"]] == ["an ambush on the road"] * 3
    assert rng.values == []
    kinds = [decoded.kind for decoded in _events_after(app, campaign_id, mark)]
    assert kinds == [
        ActorMoved.kind,
        TablesRolled.kind,
        TablesRolled.kind,
        TablesRolled.kind,
        ClockAdvanced.kind,
    ]


def test_a_day_event_does_not_roll_when_its_when_filters_do_not_match(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id, tools = _setup(client_factory)
    event = JourneyEvent(on="day", table="dungeon_event", terrain=("plains",))
    mark = _mark(app, campaign_id)

    result = _event_tools(tools, event, _ScriptedRandom()).travel(NAME, "Far", "mountains")

    assert result["events"] == []
    assert [decoded.kind for decoded in _events_after(app, campaign_id, mark)] == [
        ActorMoved.kind,
        ClockAdvanced.kind,
    ]


def test_a_failed_event_chance_records_the_check_and_skips_the_table(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id, tools = _setup(client_factory)
    event = JourneyEvent(on="day", table="dungeon_event", chance=1)
    mark = _mark(app, campaign_id)

    result = _event_tools(tools, event, _ScriptedRandom(6)).travel(NAME, "Far", "plains")

    assert result["events"] == []
    kinds = [decoded.kind for decoded in _events_after(app, campaign_id, mark)]
    assert kinds == [ActorMoved.kind, RollMade.kind, ClockAdvanced.kind]


def test_a_passed_event_chance_rolls_the_table(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id, tools = _setup(client_factory)
    event = JourneyEvent(on="day", table="dungeon_event", chance=1)
    mark = _mark(app, campaign_id)

    result = _event_tools(tools, event, _ScriptedRandom(1, 7)).travel(NAME, "Far", "plains")

    assert len(result["events"]) == 1
    assert result["events"][0]["roll"] == 7
    kinds = [decoded.kind for decoded in _events_after(app, campaign_id, mark)]
    assert kinds == [ActorMoved.kind, RollMade.kind, TablesRolled.kind, ClockAdvanced.kind]
