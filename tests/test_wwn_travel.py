"""Worlds Without Number overland travel, using the book's ten-hour march, terrain speeds,
supplies, foraging, and privation rules. The campaign and character helpers come from
``tests/test_wwn_advancement.py``."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.rules.wwn import travel
from cygnusloom.services import clock as clock_service
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import load_state, name_key
from tests.test_travel import _place
from tests.test_wwn_advancement import NAME, WWN, _campaign, _world_tools

PARTS = len(WWN.clock.parts)
HPP = WWN.clock.hours_per_part


def _ctx(tools: Any) -> Any:
    return SimpleNamespace(world=tools)


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
    assert call.function is not None
    assert call.world_method is None
    assert call.takes_context is True
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


def test_wwn_travel_uses_the_source_speeds_and_advances_ten_hours_a_day(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id, tools = _setup(client_factory)
    before = _hours(app, campaign_id)

    result = travel(
        NAME,
        "Far",
        "plains",
        road=True,
        weather="clear",
        days=2,
        travelers=3,
        reason="the king's road",
        ctx=_ctx(tools),
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

    result = travel(
        NAME,
        "Far",
        terrain,
        road=road,
        weather=weather,
        days=1,
        ctx=_ctx(tools),
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

    result = travel(NAME, "Far", terrain, ctx=_ctx(tools))

    assert result["forage"]["difficulty"] == difficulty


def test_wwn_travel_refuses_bad_routes_and_weather(
    client_factory: Callable[[], TestClient],
) -> None:
    _, _, tools = _setup(client_factory)

    with pytest.raises(InvalidInput, match="terrain"):
        travel(NAME, "Far", "moon", ctx=_ctx(tools))
    with pytest.raises(InvalidInput, match="weather"):
        travel(NAME, "Far", "plains", weather="storm", ctx=_ctx(tools))
    with pytest.raises(InvalidInput, match="days"):
        travel(NAME, "Far", "plains", days=0, ctx=_ctx(tools))
    with pytest.raises(InvalidInput, match="travelers"):
        travel(NAME, "Far", "plains", travelers=0, ctx=_ctx(tools))


def test_overland_move_falls_back_when_a_rule_set_has_no_wilderness(
    client_factory: Callable[[], TestClient],
) -> None:
    app, campaign_id, tools = _setup(client_factory)

    result = tools.move_actor(NAME, "Far", mode="overland")

    assert result["location"] == "Far"
    assert "does not track wilderness" in result["note"]
    assert _location(app, campaign_id) == "Far"
