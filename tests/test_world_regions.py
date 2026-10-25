from __future__ import annotations

import random
from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusforge.services import world
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import HIDDEN_KINDS, Region
from tests.conftest import content_library
from tests.test_campaigns import _new_campaign, _signed_in
from tests.test_world import TABLES, _state

LIBRARY = content_library()
CAIRN = LIBRARY.rule_sets["cairn2e"]
SQUARE_OFF_THE_MAP = [[20.0, 20.0], [21.0, 20.0], [21.0, 21.0], [20.0, 21.0]]
INSIDE_WEST_LAKE = [[-5.0, -1.0], [-4.0, -1.0], [-4.0, 0.0], [-5.0, 0.0]]


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


def test_bundled_twin_lakes_regions_are_seeded(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    regions = _state(app, campaign_id).regions
    assert set(regions) == {"west lake", "east lake"}
    assert all(isinstance(region, Region) for region in regions.values())
    assert {region.name for region in regions.values()} == {"West Lake", "East Lake"}
    assert all(region.terrain == "lake" for region in regions.values())


def test_map_view_draws_each_region_in_its_terrain_colour_and_text(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    view = world.map_view(_state(app, campaign_id), CAIRN.world_terrain)
    by_name = {region.name: region for region in view.regions}
    assert set(by_name) == {"West Lake", "East Lake"}
    lake = CAIRN.world_terrain["lake"]
    west = by_name["West Lake"]
    assert west.color == lake.color
    assert west.text == f"West Lake (Lake). {lake.rule}"
    assert all(0 <= x <= 100 and 0 <= y <= 100 for x, y in west.points)


def test_map_view_without_terrain_still_draws_regions_in_grey(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    view = world.map_view(_state(app, campaign_id))
    assert all(region.color == "#888888" for region in view.regions)
    assert all(region.text.startswith(region.name) for region in view.regions)


def test_a_region_stretches_the_map_to_its_corners(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    before = world.map_view(_state(app, campaign_id), CAIRN.world_terrain).radius_km
    _tools(app, campaign_id).create_region("Far Mere", "lake", SQUARE_OFF_THE_MAP)
    after = world.map_view(_state(app, campaign_id), CAIRN.world_terrain).radius_km
    assert after > before
    assert after >= 20.0


def test_gm_creates_a_region_and_it_is_saved_for_the_campaign(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    result = _tools(app, campaign_id).create_region("Dark Mere", "lake", SQUARE_OFF_THE_MAP)
    assert result == {"created": "region", "name": "Dark Mere", "terrain": "lake"}
    region = _state(app, campaign_id).regions["dark mere"]
    assert isinstance(region, Region)
    assert region.points == tuple((x, y) for x, y in SQUARE_OFF_THE_MAP)


def test_region_names_are_unique(client_factory: Callable[[], TestClient], app: FastAPI):
    campaign_id = _twin_lakes(client_factory)
    with pytest.raises(InvalidInput, match="already a region named 'west lake'"):
        _tools(app, campaign_id).create_region("west lake", "lake", SQUARE_OFF_THE_MAP)


def test_unknown_terrain_is_refused_and_lists_the_kinds(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    with pytest.raises(InvalidInput, match="'bog' is not a terrain kind.*lake"):
        _tools(app, campaign_id).create_region("Fen", "bog", SQUARE_OFF_THE_MAP)


def test_a_region_that_overlaps_another_is_refused(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    with pytest.raises(InvalidInput, match="overlaps 'West Lake'.*force=true"):
        _tools(app, campaign_id).create_region("Second Pool", "lake", INSIDE_WEST_LAKE)
    assert "second pool" not in _state(app, campaign_id).regions


def test_force_lets_a_region_overlap_another(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    _tools(app, campaign_id).create_region("Second Pool", "lake", INSIDE_WEST_LAKE, force=True)
    assert "second pool" in _state(app, campaign_id).regions


def test_region_shapes_must_be_valid_polygons(client_factory: Callable[[], TestClient], app):
    campaign_id = _twin_lakes(client_factory)
    tools = _tools(app, campaign_id)
    with pytest.raises(InvalidInput, match="the region"):
        tools.create_region("Line", "lake", [[0, 0], [1, 1]])
    with pytest.raises(InvalidInput, match="each point must be an"):
        tools.create_region("Words", "lake", [["a", 0], [1, 1], [1, 2]])
    with pytest.raises(InvalidInput, match="points must be a list"):
        tools.create_region("Many", "lake", [[0, 0]] * 201)
    bow_tie = [[0, 0], [2, 2], [2, 0], [0, 2]]
    with pytest.raises(InvalidInput, match="the region"):
        tools.create_region("Bow", "lake", bow_tie)
    assert _state(app, campaign_id).regions.keys() == {"west lake", "east lake"}


def test_force_must_be_true_or_false(client_factory: Callable[[], TestClient], app: FastAPI):
    campaign_id = _twin_lakes(client_factory)
    with pytest.raises(InvalidInput, match="force must be true or false"):
        _tools(app, campaign_id).create_region("Mere", "lake", SQUARE_OFF_THE_MAP, force="yes")


def test_the_campaign_page_draws_the_regions_with_their_tooltips(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client = _signed_in(client_factory)
    response = _new_campaign(client, world_template="twin-lakes", rule_set="cairn2e")
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/campaigns/{campaign_id}").text
    lake = CAIRN.world_terrain["lake"]
    assert 'class="map-region"' in page
    assert f'data-tip="West Lake (Lake). {lake.rule}"' in page
    assert f'fill="{lake.color}"' in page
    assert page.count("data-follow") >= 2


def test_region_events_are_visible_to_everyone():
    """Regions are drawn for every player, so their creation is not hidden from the transcript."""
    assert HIDDEN_KINDS["region.added"] is False
