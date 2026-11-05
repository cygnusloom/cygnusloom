from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusloom.services.common import InvalidInput
from cygnusloom.services.world import WorldTools
from tests.test_campaigns import _create_character
from tests.test_character_creation import _cairn_campaign, _complete_form
from tests.test_combat import CAIRN, LIBRARY, PC
from tests.test_combat import _tools as _combat_tools
from tests.test_world import _state

TRAVEL = CAIRN.travel


def _tools(app: FastAPI, campaign_id: int) -> WorldTools:
    return replace(_combat_tools(app, campaign_id), travel=TRAVEL)


PARTS = len(CAIRN.clock.parts)
HPP = CAIRN.clock.hours_per_part


def _pc_campaign(client_factory: Callable[[], TestClient]) -> tuple[FastAPI, int, WorldTools]:
    client, campaign_id = _cairn_campaign(client_factory)
    created = _create_character(
        client, campaign_id, name=PC, fields=_complete_form(client, campaign_id)
    )
    assert created.status_code == 303
    return client.app, campaign_id, _tools(client.app, campaign_id)


def _place(tools: WorldTools, name: str, x: float | None = None, y: float | None = None) -> None:
    tools.create_location(name, f"The {name}.", discovered=True, x=x, y=y)


def _hours(app: FastAPI, campaign_id: int) -> int:
    clock = _state(app, campaign_id).clock
    return (clock.day - 1) * PARTS * HPP + clock.part * HPP + clock.hour


def test_a_four_kilometre_leg_on_foot_takes_an_hour(client_factory):
    app, campaign_id, tools = _pc_campaign(client_factory)
    _place(tools, "Start", 0, 0)
    _place(tools, "Four Out", 4, 0)
    tools.move_actor(PC, "Start")
    before = _hours(app, campaign_id)

    result = tools.move_actor(PC, "Four Out")

    assert result["minutes"] == 60
    assert result["speed"] == "on_foot"
    assert _hours(app, campaign_id) - before == 1


def test_isthmus_town_to_dunston_on_foot_takes_about_an_hour(client_factory):
    _, _, tools = _pc_campaign(client_factory)
    tools.move_actor(PC, "Isthmus Town")

    result = tools.move_actor(PC, "Dunston")

    assert abs(result["minutes"] - 60) <= 1


def test_a_road_leg_is_faster_than_a_foot_leg_over_the_same_distance(client_factory):
    _, _, tools = _pc_campaign(client_factory)
    _place(tools, "Start", 0, 0)
    _place(tools, "Far", 6, 0)
    tools.move_actor(PC, "Start")
    on_foot = tools.move_actor(PC, "Far")
    tools.move_actor(PC, "Start")

    by_road = tools.move_actor(PC, "Far", travel="road")

    assert by_road["minutes"] < on_foot["minutes"]
    assert by_road["minutes"] == 60


def test_an_unknown_travel_mode_is_refused(client_factory):
    app, campaign_id, tools = _pc_campaign(client_factory)
    _place(tools, "Start", 0, 0)
    _place(tools, "Far", 4, 0)
    tools.move_actor(PC, "Start")
    before = _hours(app, campaign_id)

    with pytest.raises(InvalidInput, match="teleport"):
        tools.move_actor(PC, "Far", travel="teleport")

    assert _state(app, campaign_id).characters[PC.casefold()].location == "Start"
    assert _hours(app, campaign_id) == before


def test_a_move_from_a_place_inside_another_uses_the_parents_position(client_factory):
    _, _, tools = _pc_campaign(client_factory)
    tools.move_actor(PC, "Isthmus Town")
    tools.move_actor(PC, "The Stuffed Inn")

    result = tools.move_actor(PC, "Dunston")

    assert result["minutes"] != TRAVEL.default_leg_minutes
    assert abs(result["minutes"] - 60) <= 1


def test_a_leg_without_positions_takes_the_default_leg(client_factory):
    _, _, tools = _pc_campaign(client_factory)
    _place(tools, "Hall")
    _place(tools, "Antechamber")
    tools.move_actor(PC, "Hall")

    result = tools.move_actor(PC, "Antechamber")

    assert result["minutes"] == TRAVEL.default_leg_minutes == 10


def test_minutes_carry_until_they_make_an_hour(client_factory):
    app, campaign_id, tools = _pc_campaign(client_factory)
    _place(tools, "Start", 0, 0)
    _place(tools, "Two Out", 2, 0)
    tools.move_actor(PC, "Start")
    before = _hours(app, campaign_id)

    first = tools.move_actor(PC, "Two Out")
    assert first["minutes"] == 30
    assert _hours(app, campaign_id) == before
    assert _state(app, campaign_id).travel_minutes == 40

    second = tools.move_actor(PC, "Start")
    assert second["minutes"] == 30
    assert _hours(app, campaign_id) - before == 1
    assert _state(app, campaign_id).travel_minutes == 10


def test_the_move_actor_schema_offers_the_table_modes():
    schema = LIBRARY.rule_sets["cairn2e"].tool_calls["move_actor"].parameters["properties"]
    travel = schema["travel"]

    assert travel["enum"] == sorted(TRAVEL.modes)
    assert travel["default"] == "on_foot"


def test_a_move_without_a_travel_table_charges_nothing(client_factory):
    app, campaign_id, tools = _pc_campaign(client_factory)
    _place(tools, "Start", 0, 0)
    _place(tools, "Far", 4, 0)
    tools.move_actor(PC, "Start")
    plain = replace(_tools(app, campaign_id), travel=None)
    before = _hours(app, campaign_id)

    assert plain.move_actor(PC, "Far") == {"moved": PC, "location": "Far"}
    assert _hours(app, campaign_id) == before
