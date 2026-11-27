"""Threats in play: a single monster is a statblock, and a scale threat stands on the map.

Hordes does not roll for its monsters, so a threat's statblock carries a harm capacity, armor,
tags, and moves rather than hit points and attack dice. The player reads it in the same dialog
a character or monster opens, and a threat big enough to be a scale of its own, such as a horde,
is a token on the world map at the place it holds.
"""

from __future__ import annotations

from cygnusloom.services import threats as threats_service
from cygnusloom.services.events import load_state
from cygnusloom.services.views import map_view
from tests.factories import db, rules_for
from tests.test_hordes import _campaign

RULE_SET = "hordes"
BASIN = [[-10.0, -8.0], [10.0, -8.0], [10.0, 8.0], [-10.0, 8.0]]


def _two_places(tools) -> None:
    """Ground, the player's place, and a second place six kilometers east."""
    tools.create_region("The Basin", "farmland", BASIN, discovered=True)
    tools.create_location("North Gate", "A gate in the chain-link fence.", discovered=True, x=0, y=0)
    tools.create_location("The Mill", "A mill by the river.", discovered=True, x=6, y=0)
    tools.move_actor("Ada", "North Gate")


def _view(client, campaign_id):
    rules = rules_for(RULE_SET)
    with db(client.app) as session:
        state = load_state(session, campaign_id)
    return map_view(state, rules.world_terrain, roads=rules.roads, threats=rules.threats)


def test_a_single_monster_is_a_statblock_in_the_location(client_factory):
    client, campaign_id, tools = _campaign(client_factory)
    _two_places(tools)
    tools.create_threat("walker", name="the walker", location="North Gate")

    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'data-statblock-name="the walker"' in page
    # A single monster is not a token on the map.
    assert 'class="map-marker threat"' not in page
    assert _view(client, campaign_id).threats == []

    block = client.get(f"/campaigns/{campaign_id}/statblock", params={"name": "the walker"})
    assert block.status_code == 200


def test_a_monster_at_another_place_is_not_in_the_roster(client_factory):
    _client, campaign_id, tools = _campaign(client_factory)
    _two_places(tools)
    tools.create_threat("walker", name="the walker", location="The Mill")

    page = _client.get(f"/campaigns/{campaign_id}").text
    assert 'data-statblock-name="the walker"' not in page


def test_a_horde_stands_on_the_map_and_not_in_the_roster(client_factory):
    client, campaign_id, tools = _campaign(client_factory)
    _two_places(tools)
    tools.create_threat("horde", name="the tide", magnitude=2, location="The Mill")

    page = client.get(f"/campaigns/{campaign_id}").text
    # The map panel and the full Map view each draw it where it is.
    assert page.count('class="map-marker threat"') == 2
    assert 'data-tip="the tide (horde, magnitude 2, 6 km east, 0 km north)"' in page
    assert "&times;2" in page
    assert 'data-statblock-name="the tide"' not in page

    view = _view(client, campaign_id)
    assert [threat.name for threat in view.threats] == ["the tide"]
    # The token sits at The Mill's marker, six kilometers east of the player.
    mill = next(marker for marker in view.markers if marker.name == "The Mill")
    assert (view.threats[0].left, view.threats[0].top) == (mill.left, mill.top)
    assert view.threats[0].left > 50 and view.threats[0].top == 50
    assert view.threats[0].icon == "horde"
    assert view.threats[0].magnitude == 2


def test_a_horde_at_the_players_place_stands_at_the_centre(client_factory):
    client, campaign_id, tools = _campaign(client_factory)
    _two_places(tools)
    tools.create_threat("horde", name="the tide", magnitude=3, location="North Gate")

    view = _view(client, campaign_id)
    assert [(threat.name, threat.left, threat.top) for threat in view.threats] == [
        ("the tide", 50.0, 50.0)
    ]
    assert "here" in view.threats[0].tip
    assert 'data-statblock-name="the tide"' not in client.get(f"/campaigns/{campaign_id}").text


def test_the_nearest_horde_draws_last_so_it_is_on_top(client_factory):
    client, campaign_id, tools = _campaign(client_factory)
    _two_places(tools)
    tools.create_location("Far Ridge", "A ridge above the valley.", discovered=True, x=-9, y=0)
    tools.create_threat("horde", name="the far tide", magnitude=1, location="Far Ridge")
    tools.create_threat("horde", name="the near tide", magnitude=2, location="The Mill")

    view = _view(client, campaign_id)
    assert [threat.name for threat in view.threats] == ["the far tide", "the near tide"]
    assert view.threats[0].left < 50 < view.threats[1].left


def test_a_horde_inside_a_room_stands_on_the_building(client_factory):
    client, campaign_id, tools = _campaign(client_factory)
    _two_places(tools)
    tools.create_location("St. Jude's Church", "A stone church.", discovered=True, x=0, y=4)
    tools.create_location("The Nave", "Rows of pews.", discovered=True, parent="St. Jude's Church")
    tools.create_threat("horde", name="the tide", magnitude=1, location="The Nave")

    view = _view(client, campaign_id)
    church = next(marker for marker in view.markers if marker.name == "St. Jude's Church")
    assert [(threat.left, threat.top) for threat in view.threats] == [(church.left, church.top)]


def test_a_horde_stretches_the_map_to_hold_it(client_factory):
    client, campaign_id, tools = _campaign(client_factory)
    _two_places(tools)
    before = _view(client, campaign_id).radius_km

    # A horde at an undiscovered place still stands on the map, and sets its reach, so the
    # player can see it coming from beyond the places they know.
    tools.create_region("Far Reach", "farmland", [[20.0, -4.0], [30.0, -4.0], [30.0, 4.0], [20.0, 4.0]])
    tools.create_location("The Camp", "Tents beyond the fence.", discovered=False, x=25, y=0)
    tools.create_threat("horde", name="the long tide", magnitude=1, location="The Camp")
    grown = _view(client, campaign_id)
    assert grown.radius_km > before
    assert [threat.name for threat in grown.threats] == ["the long tide"]
    assert all(0 <= threat.left <= 100 and 0 <= threat.top <= 100 for threat in grown.threats)
    assert "The Camp" not in [marker.name for marker in grown.markers]


def test_a_horde_with_nowhere_to_stand_is_left_out(client_factory):
    client, campaign_id, tools = _campaign(client_factory)
    _two_places(tools)
    tools.create_location("The Camp", "Tents beyond the fence.", discovered=True)
    tools.create_threat("horde", name="the tide", magnitude=1, location="The Camp")
    tools.create_threat("horde", name="the lost tide", magnitude=1, location="Nowhere")

    assert _view(client, campaign_id).threats == []
    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'class="map-marker threat"' not in page


def test_a_hidden_threat_stays_out_of_the_player_view(client_factory):
    client, campaign_id, tools = _campaign(client_factory)
    _two_places(tools)
    rules = rules_for(RULE_SET)
    with db(client.app) as session:
        threats_service.create_threat(
            session,
            campaign_id,
            "walker",
            rules.threats,
            name="the lurker",
            location="North Gate",
            visible=False,
        )

    page = client.get(f"/campaigns/{campaign_id}").text
    # The hidden log line may still be in the markup for the GM, but nothing player-facing
    # offers the threat: no roster entry and no token on the map.
    assert 'data-statblock-name="the lurker"' not in page
    assert 'class="map-marker threat"' not in page
    block = client.get(f"/campaigns/{campaign_id}/statblock", params={"name": "the lurker"})
    assert block.status_code == 404
