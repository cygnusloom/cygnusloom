from __future__ import annotations

import random
from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusloom.services import world
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import WorldMapStyle, is_hidden
from cygnusloom.services.views import map_view
from tests.conftest import content_library
from tests.test_campaigns import _new_campaign, _signed_in
from tests.test_world import TABLES, _state

LIBRARY = content_library()
CAIRN = LIBRARY.rule_sets["cairn2e"]
SQUARE = [[20.0, 20.0], [21.0, 20.0], [21.0, 21.0], [20.0, 21.0]]


def _twin_lakes(client_factory: Callable[[], TestClient]) -> int:
    client = _signed_in(client_factory)
    response = _new_campaign(client, world_template="twin-lakes", rule_set="cairn2e")
    return int(response.headers["location"].rsplit("/", 1)[1])


def _tools(app: FastAPI, campaign_id: int) -> world.WorldTools:
    return world.WorldTools(
        engine=app.state.engine,
        campaign_id=campaign_id,
        tables=TABLES,
        rng=random.Random(0),
        character=CAIRN.character,
        plus="sum",
        world_terrain=CAIRN.world_terrain,
    )


def test_the_world_map_style_changes_and_replays(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    tools = _tools(app, campaign_id)
    result = tools.update_world_map(
        palette="midnight",
        ambient="night",
        background_color="#101820",
        line_color="#8899aa",
        show_clouds=False,
        show_contours=False,
    )
    expected = WorldMapStyle(
        palette="midnight",
        ambient="night",
        background_color="#101820",
        line_color="#8899aa",
        show_clouds=False,
        show_contours=False,
    )
    assert _state(app, campaign_id).world_map_style == expected
    assert result["palette"] == "midnight"
    assert result["show_clouds"] is False
    assert map_view(_state(app, campaign_id), CAIRN.world_terrain).style == expected


def test_the_world_map_style_rejects_unknown_choices(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    tools = _tools(app, campaign_id)
    with pytest.raises(InvalidInput, match="palette must be one of parchment"):
        tools.update_world_map(palette="neon")
    with pytest.raises(InvalidInput, match="ambient must be one of"):
        tools.update_world_map(ambient="stormy")
    with pytest.raises(InvalidInput, match="background_color must be a hex colour"):
        tools.update_world_map(background_color="blue")
    with pytest.raises(InvalidInput, match="show_clouds must be true or false"):
        tools.update_world_map(show_clouds="yes")


def test_a_region_can_have_its_own_colour_texture_and_label(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    tools = _tools(app, campaign_id)
    tools.create_region(
        "Ember Waste",
        "desert",
        SQUARE,
        discovered=True,
        color="#a03020",
        pattern="lava",
        ink="#ffcc44",
        label="The Cinder Waste",
    )
    view = map_view(_state(app, campaign_id), CAIRN.world_terrain)
    region = next(region for region in view.regions if region.name == "The Cinder Waste")
    assert region.color == "#a03020"
    assert region.pattern == "custom-lava-ffcc44"
    assert region.text == f"Ember Waste (Desert). {CAIRN.world_terrain['desert'].rule}"
    assert region.label_left is not None and region.label_top is not None
    pattern = next(pattern for pattern in view.patterns if pattern.id == region.pattern)
    assert (pattern.shape, pattern.ink) == ("lava", "#ffcc44")


def test_update_region_reshapes_and_restyles_a_region(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    tools = _tools(app, campaign_id)
    tools.create_region("Far Mere", "lake", SQUARE)
    extended = [[19.0, 19.0], [23.0, 19.0], [23.0, 23.0], [19.0, 23.0]]
    tools.update_region(
        "Far Mere",
        terrain="meadow",
        points=extended,
        discovered=True,
        color="#88aa44",
        label="The Long Meadow",
    )
    state = _state(app, campaign_id)
    region = state.regions["far mere"]
    assert region.terrain == "meadow"
    assert region.points == tuple((x, y) for x, y in extended)
    assert region.discovered is True
    assert region.color == "#88aa44"
    assert region.label == "The Long Meadow"
    view = map_view(state, CAIRN.world_terrain)
    drawn = next(item for item in view.regions if item.name == "The Long Meadow")
    assert drawn.color == "#88aa44"


def test_update_region_can_reset_its_colour_and_remove_its_texture(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    tools = _tools(app, campaign_id)
    tools.create_region(
        "Far Mere",
        "lake",
        SQUARE,
        discovered=True,
        color="#123456",
        pattern="lava",
        label="Old Name",
    )
    tools.update_region("Far Mere", color="", pattern="", label="")
    region = _state(app, campaign_id).regions["far mere"]
    assert region.color == ""
    assert region.pattern == ""
    assert region.label is None
    view = map_view(_state(app, campaign_id), CAIRN.world_terrain)
    drawn = next(item for item in view.regions if item.name == "Far Mere")
    assert drawn.color == CAIRN.world_terrain["lake"].color
    assert drawn.pattern is None


def test_update_region_refuses_an_overlapping_outline(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    tools = _tools(app, campaign_id)
    tools.create_region(
        "Far Mere", "lake", [[20.0, 20.0], [22.0, 20.0], [22.0, 22.0], [20.0, 22.0]]
    )
    tools.create_region(
        "Near Mere", "lake", [[24.0, 20.0], [26.0, 20.0], [26.0, 22.0], [24.0, 22.0]]
    )
    with pytest.raises(InvalidInput, match="overlaps the region 'Far Mere'"):
        tools.update_region(
            "Near Mere", points=[[21.0, 20.0], [23.0, 20.0], [23.0, 22.0], [21.0, 22.0]]
        )
    assert _state(app, campaign_id).regions["near mere"].points == (
        (24.0, 20.0),
        (26.0, 20.0),
        (26.0, 22.0),
        (24.0, 22.0),
    )


def test_a_place_can_choose_and_clear_its_map_icon(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    tools = _tools(app, campaign_id)
    tools.create_region(
        "Far Mere", "lake", [[20.0, 20.0], [24.0, 20.0], [24.0, 24.0], [20.0, 24.0]]
    )
    tools.create_location(
        "Mere Academy",
        "A school on the shore.",
        discovered=True,
        x=22.0,
        y=22.0,
        icon="academy",
    )
    state = _state(app, campaign_id)
    assert state.places["mere academy"].icon == "academy"
    marker = next(
        item for item in map_view(state, CAIRN.world_terrain).markers if item.name == "Mere Academy"
    )
    assert marker.icon == "academy"
    tools.update_location("Mere Academy", "The same school.", icon="")
    assert _state(app, campaign_id).places["mere academy"].icon is None
    with pytest.raises(InvalidInput, match="there is no icon 'spaceship'"):
        tools.create_location("Starport", "A port.", icon="spaceship")


def test_map_options_lists_the_world_terrain_palettes_and_place_icons(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    options = _tools(app, campaign_id).map_options()
    world_options = options["world"]
    assert "river" in {terrain["id"] for terrain in world_options["terrains"]}
    assert "midnight" in world_options["palettes"]
    assert "night" in world_options["ambients"]
    assert "academy" in {icon["id"] for icon in world_options["place_icons"]}


def test_a_smaller_region_draws_over_a_larger_one_and_keeps_one_label(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    tools = _tools(app, campaign_id)
    tools.create_region(
        "Broad Ground",
        "meadow",
        [[20.0, 20.0], [24.0, 20.0], [24.0, 24.0], [20.0, 24.0]],
        discovered=True,
        force=True,
    )
    tools.create_region(
        "Small Mere",
        "lake",
        [[21.0, 21.0], [22.0, 21.0], [22.0, 22.0], [21.0, 22.0]],
        discovered=True,
        force=True,
    )
    view = map_view(_state(app, campaign_id), CAIRN.world_terrain)
    names = [region.name for region in view.regions if region.name]
    assert names.index("Small Mere") > names.index("Broad Ground")
    labelled = [region for region in view.regions if region.label_left is not None]
    assert len(labelled) == 1


def test_world_map_events_are_hidden_from_the_transcript():
    assert is_hidden("map.world_style_changed") is True
    assert is_hidden("region.updated") is True


def test_the_campaign_page_draws_the_world_map_look_and_region_label(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client = _signed_in(client_factory)
    response = _new_campaign(client, world_template="twin-lakes", rule_set="cairn2e")
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    tools = _tools(app, campaign_id)
    tools.update_world_map(
        palette="midnight",
        ambient="night",
        background_color="#101820",
    )
    tools.create_region(
        "Ember Waste",
        "desert",
        SQUARE,
        discovered=True,
        label="The Cinder Waste",
    )
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "palette-midnight" in page
    assert "ambient-night" in page
    assert "--world-bg: #101820" in page
    assert 'class="map-region-label"' in page
    assert "The Cinder Waste" in page
