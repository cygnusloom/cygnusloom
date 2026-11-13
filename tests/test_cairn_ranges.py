"""Cairn 2e's attack limits on the encounter grid.

Cairn's book gives no distances, so its rule set keeps melee to the cell next to the attacker
and lets a bow, crossbow, or sling reach anywhere on the map; only a blocking object on the line
stops the shot.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusloom.content.definitions import ContentError, range_bands
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.world import WorldTools
from tests.conftest import content_library
from tests.test_combat import MAP, PC, _character, _fight, _foe
from tests.turns import take_turn

FOE = "Alley Hound"


def _placed(
    client_factory: Callable[[], TestClient], column: int, row: int = 0
) -> tuple[FastAPI, int, WorldTools]:
    """A Cairn fight in the mapped yard with the player character and a foe on the grid."""
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, FOE)
    tools.place_participant(PC, 0, 0)
    tools.place_participant(FOE, column, row)
    return app, campaign_id, tools


def test_melee_needs_the_next_cell(client_factory):
    _app, _campaign_id, tools = _placed(client_factory, 2)
    take_turn(tools, PC)

    with pytest.raises(InvalidInput, match=f"10 feet away, beyond {PC}'s melee reach of one cell"):
        tools.player_attack(FOE, "Sword", attack_roll=20, damage_roll=2)

    tools.place_participant(FOE, 0, 1)
    result = tools.player_attack(FOE, "Sword", attack_roll=20, damage_roll=2)
    assert result["range"] == {
        "melee": True,
        "band": "",
        "cells": 1,
        "feet": 5,
        "penalty": 0,
    }


def test_a_bow_reaches_the_whole_map(client_factory):
    _app, _campaign_id, tools = _placed(client_factory, 9, 9)
    tools.give_item(PC, "Bow")
    take_turn(tools, PC)

    result = tools.player_attack(FOE, "Bow", attack_roll=20, damage_roll=1)

    assert result["range"] == {
        "melee": False,
        "band": "ranged",
        "cells": 9,
        "feet": 45,
        "penalty": 0,
    }


def test_a_blocking_object_stops_a_shot_across_the_map(client_factory):
    _app, _campaign_id, tools = _placed(client_factory, 0, 6)
    tools.give_item(PC, "Bow")
    tools.set_map_cells(MAP, [{"column": 0, "row": 3, "object": "wall"}])
    take_turn(tools, PC)

    with pytest.raises(InvalidInput, match="wall at column 0, row 3 blocks the line of sight"):
        tools.player_attack(FOE, "Bow", attack_roll=20, damage_roll=1)

    tools.set_map_cells(MAP, [{"column": 0, "row": 3, "object": ""}])
    result = tools.player_attack(FOE, "Bow", attack_roll=20, damage_roll=1)
    assert result["range"]["feet"] == 30


def test_an_npc_armed_with_an_unlimited_band_reaches_anywhere(client_factory):
    app, campaign_id, tools = _placed(client_factory, 0, 8)
    tools.update_npc(FOE, attacks=[{"name": "Sling", "dice": "1d4", "ranges": {"ranged": 0}}])
    assert _character(app, campaign_id, FOE).attack_ranges == {"Sling": {"ranged": None}}
    take_turn(tools, FOE)

    result = tools.attack(FOE, "Sling", PC)

    assert result["range"] == {
        "melee": False,
        "band": "ranged",
        "cells": 8,
        "feet": 40,
        "penalty": 0,
    }
    statblock = tools.get_statblock(FOE)
    assert "Sling 1d4 (range unlimited)" in " ".join(statblock["statblock"])


def test_cairn_content_declares_adjacency_and_unlimited_missiles():
    cairn = content_library().rule_sets["cairn2e"]

    assert cairn.combat.ranges is not None
    # No melee distance in the ruleset means one cell, so an attack must be next to its target.
    assert cairn.combat.ranges.melee is None
    assert "wall" in cairn.combat.ranges.sight_blocked_by
    assert cairn.character.features["bow"].ranges == {"ranged": None}
    assert cairn.character.features["crossbow"].ranges == {"ranged": None}
    assert cairn.character.features["sling"].ranges == {"ranged": None}
    assert cairn.character.features["long-sword"].ranges == {}


def test_a_band_distance_of_zero_means_no_limit():
    assert range_bands({"ranged": 0}, "test") == {"ranged": None}
    assert range_bands({"Short": 30, "long": 0}, "test") == {"short": 30, "long": None}
    with pytest.raises(ContentError, match="0 for no limit"):
        range_bands({"short": -1}, "test")
