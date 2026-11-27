"""Modern map styles: roads on the world chart, and the local map's board styles.

The world map is ground plus the roads across it, so a zombie apocalypse or a modern city
reads as a road atlas rather than a parchment chart. A local map likewise chooses a board
style, so a city block or a tunnel can be drawn as a floor plan instead of a miniature board.
"""

from __future__ import annotations

import pytest

from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import load_state, name_key
from cygnusloom.services.views import map_view
from tests.factories import db, rules_for
from tests.test_hordes import _campaign

RULE_SET = "hordes"
# A road that bends through the map's middle, in kilometers from the center.
ROAD = [[-4.0, 0.0], [0.0, 0.5], [4.0, 1.0]]
BASIN = [[-10.0, -8.0], [10.0, -8.0], [10.0, 8.0], [-10.0, 8.0]]


def _basin(tools) -> None:
    """Ground for a road to run over."""
    tools.create_region("The Basin", "farmland", BASIN, discovered=True)


def test_a_road_is_laid_and_drawn_on_the_map(client_factory):
    client, campaign_id, tools = _campaign(client_factory)
    rules = rules_for(RULE_SET)
    _basin(tools)
    created = tools.create_road("Route 9", "highway", ROAD)
    assert created == {
        "created": "road",
        "name": "Route 9",
        "kind": "highway",
        "discovered": False,
    }
    with db(client.app) as session:
        state = load_state(session, campaign_id)
    assert state.roads[name_key("Route 9")].points == ((-4.0, 0.0), (0.0, 0.5), (4.0, 1.0))

    # An undiscovered road with no discovered place on it stays off the player's map.
    hidden = map_view(state, rules.world_terrain, roads=rules.roads)
    assert hidden.roads == []

    tools.update_road("Route 9", discovered=True)
    with db(client.app) as session:
        state = load_state(session, campaign_id)
    view = map_view(state, rules.world_terrain, roads=rules.roads)
    assert [road.name for road in view.roads] == ["Route 9"]
    drawn = view.roads[0]
    assert drawn.color == rules.roads["highway"].color
    assert drawn.width == rules.roads["highway"].width
    assert drawn.dashed is False
    assert len(drawn.points) == 3
    assert drawn.label_left is not None and drawn.label_top is not None
    assert drawn.text.startswith("Route 9 (Highway).")


def test_a_road_appears_once_a_discovered_place_stands_on_it(client_factory):
    client, campaign_id, tools = _campaign(client_factory)
    rules = rules_for(RULE_SET)
    _basin(tools)
    tools.create_location("Quiet Farm", "A farm at the end of the lane.", x=-4, y=0)
    tools.create_road("Old Lane", "track", ROAD)
    with db(client.app) as session:
        state = load_state(session, campaign_id)
    assert map_view(state, rules.world_terrain, roads=rules.roads).roads == []

    tools.discover_location("Quiet Farm")
    with db(client.app) as session:
        state = load_state(session, campaign_id)
    view = map_view(state, rules.world_terrain, roads=rules.roads)
    assert [road.name for road in view.roads] == ["Old Lane"]
    assert view.roads[0].dashed is True


def test_road_tools_refuse_bad_input(client_factory):
    _client, _campaign_id, tools = _campaign(client_factory)
    _basin(tools)
    with pytest.raises(InvalidInput, match="is not a road kind"):
        tools.create_road("Boulevard", "escalator", ROAD)
    with pytest.raises(InvalidInput, match="at least two points"):
        tools.create_road("Stub", "street", [[0.0, 0.0]])
    with pytest.raises(InvalidInput, match="repeat the same point"):
        tools.create_road("Loop", "street", [[0.0, 0.0], [0.0, 0.0], [1.0, 0.0]])

    tools.create_road("Route 9", "highway", ROAD)
    with pytest.raises(InvalidInput, match="already a road named"):
        tools.create_road("Route 9", "street", ROAD)
    with pytest.raises(InvalidInput, match="there is no road named"):
        tools.update_road("Route 10", discovered=True)
    with pytest.raises(InvalidInput, match="at least two points"):
        tools.update_road("Route 9", points=[[1.0, 1.0]])


def test_the_world_chart_can_change_and_refuses_an_unknown_style(client_factory):
    _client, _campaign_id, tools = _campaign(client_factory)
    result = tools.update_world_map(chart="blueprint", palette="blueprint", ambient="night")
    assert result["chart"] == "blueprint"
    assert result["palette"] == "blueprint"
    with pytest.raises(InvalidInput, match="chart must be one of chart, atlas, blueprint"):
        tools.update_world_map(chart="watercolor")


def test_a_local_map_can_be_restyled(client_factory):
    client, campaign_id, tools = _campaign(client_factory)
    tools.create_location("Redgate Depot", "A depot by the rails.", discovered=True)
    tools.create_local_map(
        "Redgate Depot local map",
        "Redgate Depot",
        "A loading floor of shelving and a glassed office.",
        "warehouse",
    )
    tools.update_local_map("Redgate Depot local map", style="blueprint")
    with db(client.app) as session:
        entry = load_state(session, campaign_id).maps[name_key("Redgate Depot local map")]
    assert entry.style == "blueprint"
    with pytest.raises(InvalidInput, match="style must be one of board, plan, blueprint"):
        tools.update_local_map("Redgate Depot local map", style="hologram")


def test_the_world_map_page_draws_the_roads(client_factory):
    client, campaign_id, tools = _campaign(client_factory)
    _basin(tools)
    tools.create_location("Nine Bridge", "A rail bridge over the narrows.", discovered=True, x=4, y=1)
    tools.create_road("Route 9", "highway", ROAD, discovered=True)
    body = client.get(f"/campaigns/{campaign_id}").text
    assert 'class="map-road"' in body
    assert "Route 9" in body
    assert "Route 9 (Highway)." in body
