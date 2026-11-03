from __future__ import annotations

import itertools
import random
from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusforge.services import world
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import Region, is_hidden
from cygnusforge.services.geometry import clearance, contains, overlaps
from tests.conftest import content_library
from tests.test_campaigns import _create_character, _new_campaign, _signed_in
from tests.test_character_creation import _cairn_campaign, _complete_form
from tests.test_world import TABLES, _state

LIBRARY = content_library()
CAIRN = LIBRARY.rule_sets["cairn2e"]
SQUARE_OFF_THE_MAP = [[20.0, 20.0], [21.0, 20.0], [21.0, 21.0], [20.0, 21.0]]
INSIDE_WEST_LAKE = [[-5.8, -1.1], [-5.4, -1.1], [-5.4, -0.7], [-5.8, -0.7]]


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


def _seeded_names() -> set[str]:
    return {region.name.lower() for region in LIBRARY.world_templates["twin-lakes"].regions}


def test_bundled_twin_lakes_regions_are_seeded(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    regions = _state(app, campaign_id).regions
    assert set(regions) == _seeded_names()
    assert len(regions) == 12
    assert all(isinstance(region, Region) for region in regions.values())
    assert all(region.terrain in CAIRN.world_terrain for region in regions.values())
    assert regions["west lake"].terrain == "lake"
    assert regions["east lake"].terrain == "lake"


def test_every_region_is_drawn_but_undiscovered_ones_stay_unnamed(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    view = world.map_view(_state(app, campaign_id), CAIRN.world_terrain)
    assert len(view.regions) == 12
    assert all(region.name is None and region.text is None for region in view.regions)
    kinds = {region.color for region in view.regions}
    assert CAIRN.world_terrain["open"].color in kinds
    assert CAIRN.world_terrain["lake"].color in kinds


def test_a_discovered_place_inside_a_region_reveals_it(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    _tools(app, campaign_id).create_location(
        name="Lake Shore", description="A shore.", discovered=True, x=-5.59, y=-0.86
    )
    view = world.map_view(_state(app, campaign_id), CAIRN.world_terrain)
    assert {region.name for region in view.regions if region.name} == {"West Lake"}
    assert view.regions[[region.name for region in view.regions].index("West Lake")].text


def test_map_view_draws_each_region_in_its_terrain_colour_and_text(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    _tools(app, campaign_id).create_region("Far Mere", "lake", SQUARE_OFF_THE_MAP, discovered=True)
    view = world.map_view(_state(app, campaign_id), CAIRN.world_terrain)
    by_name = {region.name: region for region in view.regions if region.name}
    assert set(by_name) == {"Far Mere"}
    lake = CAIRN.world_terrain["lake"]
    mere = by_name["Far Mere"]
    assert mere.color == lake.color
    assert mere.text == f"Far Mere (Lake). {lake.rule}"
    assert mere.pattern == "terrain-lake"
    assert all(0 <= x <= 100 and 0 <= y <= 100 for x, y in mere.points)
    assert {pattern.id for pattern in view.patterns} >= {"terrain-lake"}


def test_map_view_without_terrain_still_draws_regions_in_grey(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    _tools(app, campaign_id).create_region("Far Mere", "lake", SQUARE_OFF_THE_MAP, discovered=True)
    view = world.map_view(_state(app, campaign_id))
    mere = next(region for region in view.regions if region.name == "Far Mere")
    assert mere.color == "#888888"
    assert mere.text == "Far Mere"
    assert mere.pattern is None


def test_a_region_stretches_the_map_to_its_corners(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    before = world.map_view(_state(app, campaign_id), CAIRN.world_terrain).radius_km
    _tools(app, campaign_id).create_region("Far Mere", "lake", SQUARE_OFF_THE_MAP, discovered=True)
    after = world.map_view(_state(app, campaign_id), CAIRN.world_terrain).radius_km
    assert after > before
    assert after >= 20.0


def test_gm_creates_a_region_and_it_is_saved_for_the_campaign(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    result = _tools(app, campaign_id).create_region("Dark Mere", "lake", SQUARE_OFF_THE_MAP)
    assert result == {
        "created": "region",
        "name": "Dark Mere",
        "terrain": "lake",
        "discovered": False,
    }
    region = _state(app, campaign_id).regions["dark mere"]
    assert isinstance(region, Region)
    assert region.discovered is False
    assert region.points == tuple((x, y) for x, y in SQUARE_OFF_THE_MAP)


def test_a_gm_marked_discovered_region_is_on_the_map_at_once(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    _tools(app, campaign_id).create_region("Dark Mere", "lake", SQUARE_OFF_THE_MAP, discovered=True)
    assert _state(app, campaign_id).regions["dark mere"].discovered is True
    view = world.map_view(_state(app, campaign_id), CAIRN.world_terrain)
    assert [region.name for region in view.regions if region.name] == ["Dark Mere"]


def test_a_place_off_the_terrain_is_refused(client_factory: Callable[[], TestClient], app):
    campaign_id = _twin_lakes(client_factory)
    with pytest.raises(InvalidInput, match="is not on the map's terrain"):
        _tools(app, campaign_id).create_location(
            name="Blank Hut", description="A hut.", x=20.0, y=20.0
        )
    assert "blank hut" not in _state(app, campaign_id).places


def test_a_place_needs_ground_for_the_buffer_around_it(
    client_factory: Callable[[], TestClient], app
):
    campaign_id = _twin_lakes(client_factory)
    tools = _tools(app, campaign_id)
    tools.create_region("Far Mere", "lake", [[20, 20], [24, 20], [24, 24], [20, 24]])
    with pytest.raises(InvalidInput, match="is not on the map's terrain"):
        tools.create_location(name="Shore Hut", description="A hut.", x=23.8, y=22.0)
    created = tools.create_location(name="Mere Hut", description="A hut.", x=22.0, y=22.0)
    assert created == {"created": "location", "name": "Mere Hut", "discovered": False}


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


def test_an_overlapping_region_is_cut_to_the_open_ground(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    tools = _tools(app, campaign_id)
    tools.create_region("Far Mere", "lake", [[20, 20], [24, 20], [24, 24], [20, 24]])
    result = tools.create_region("West Field", "open", [[22, 18], [28, 18], [28, 22], [22, 22]])
    field = _state(app, campaign_id).regions["west field"]
    mere = _state(app, campaign_id).regions["far mere"]
    assert result["points"] == [[x, y] for x, y in field.points]
    assert not overlaps(field.points, mere.points)
    assert contains((26.0, 20.5), field.points)
    assert not contains((23.0, 21.0), field.points)
    # The cut follows the other region's edges, so the outline gains corners.
    assert len(field.points) == 6


def test_a_region_wholly_over_covered_ground_is_refused(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    with pytest.raises(InvalidInput, match="already covered, by 'West Lake'"):
        _tools(app, campaign_id).create_region("Second Pool", "lake", INSIDE_WEST_LAKE)
    assert "second pool" not in _state(app, campaign_id).regions


def test_a_region_that_would_keep_an_island_of_other_ground_is_refused(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    tools = _tools(app, campaign_id)
    tools.create_region("Sluice", "lake", [[22, 22], [23, 22], [23, 23], [22, 23]])
    with pytest.raises(InvalidInput, match="hole"):
        tools.create_region("Big Mere", "lake", [[20, 20], [26, 20], [26, 26], [20, 26]])
    assert "big mere" not in _state(app, campaign_id).regions


def test_a_cut_that_splits_keeps_the_largest_piece(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    tools = _tools(app, campaign_id)
    tools.create_region("Far Mere", "lake", [[22, 20], [24, 20], [24, 24], [22, 24]])
    result = tools.create_region(
        "Long Field", "open", [[18, 20.5], [26, 20.5], [26, 21.5], [18, 21.5]]
    )
    field = _state(app, campaign_id).regions["long field"]
    assert result["left_out"] == 1
    assert contains((20.0, 21.0), field.points)
    assert not contains((25.0, 21.0), field.points)


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
    assert _state(app, campaign_id).regions.keys() == _seeded_names()


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
    hidden_page = client.get(f"/campaigns/{campaign_id}").text
    assert hidden_page.count('class="map-region"') == 12
    assert 'data-tip="West Lake' not in hidden_page
    _tools(app, campaign_id).create_region("Far Mere", "lake", SQUARE_OFF_THE_MAP, discovered=True)
    page = client.get(f"/campaigns/{campaign_id}").text
    lake = CAIRN.world_terrain["lake"]
    assert 'class="map-region"' in page
    assert f'data-tip="Far Mere (Lake). {lake.rule}"' in page
    assert f'fill="{lake.color}"' in page
    assert page.count("data-follow") >= 1


def test_region_events_are_hidden_behind_show_hidden():
    """Region creation is GM bookkeeping, so it is hidden from the transcript by default."""
    assert is_hidden("region.added") is True


def test_bundled_regions_cover_the_whole_world_without_overlap(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    regions = list(_state(app, campaign_id).regions.values())
    for first, second in itertools.combinations(regions, 2):
        assert not overlaps(first.points, second.points), (first.name, second.name)
    ground = [region.points for region in regions]
    for i, j in itertools.product(range(-7, 8), repeat=2):
        assert clearance((i + 0.31, j + 0.31), ground) > 0, (i, j)


def test_every_seeded_place_on_the_map_has_ground_around_it(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    campaign_id = _twin_lakes(client_factory)
    state = _state(app, campaign_id)
    ground = [region.points for region in state.regions.values()]
    placed = [place for place in state.places.values() if place.position is not None]
    assert placed
    for place in placed:
        point = (place.position.x, place.position.y)
        assert clearance(point, ground) >= world.REGION_BUFFER_KM, place.name


def _clocked_tools(app: FastAPI, campaign_id: int) -> world.WorldTools:
    return world.WorldTools(
        engine=app.state.engine,
        campaign_id=campaign_id,
        tables=TABLES,
        rng=random.Random(0),
        character=CAIRN.character,
        plus="sum",
        clock_rules=CAIRN.clock,
    )


def test_the_map_panel_shows_the_day_and_part_of_the_day(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _cairn_campaign(client_factory)
    created = _create_character(
        client, campaign_id, name="Ada", fields=_complete_form(client, campaign_id)
    )
    assert created.status_code == 303
    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'class="time-strip"' in page
    assert "Day 1, Morning" in page
    _clocked_tools(app, campaign_id).advance_time(parts=2)
    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'class="time-strip night"' in page
    assert "Day 1, Night" in page
    _clocked_tools(app, campaign_id).advance_time(parts=1)
    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'class="time-strip"' in page
    assert "Day 2, Morning" in page
