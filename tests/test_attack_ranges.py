"""Attack reach, range bands, and sight on the WWN encounter grid.

The engine measures from cell to cell: a melee attack must be within its reach, a ranged attack
must fit a band, and a blocking object on the line refuses the attack. The rule set supplies the
default reach, the sight blockers, and each weapon's or statblock attack's own reach or ranges.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusloom.services.common import InvalidInput
from cygnusloom.services.world import WorldTools
from tests.conftest import content_library
from tests.test_combat import Rolls, _character
from tests.test_world import _state
from tests.test_wwn_combat import (
    BOW,
    FOE,
    PC,
    YARD,
    _foe,
    _make_room,
    _stats,
    _struck,
    _wwn_campaign,
)
from tests.turns import next_turn, take_turn

RANGE_MAP = "Range Grid"


def _range_fight(
    client_factory: Callable[[], TestClient],
) -> tuple[FastAPI, int, WorldTools]:
    """A WWN campaign with a 24 by 24 mapped yard, an encounter, and the player character in it.
    The wide grid leaves room for long-range shots."""
    client, campaign_id, tools = _wwn_campaign(client_factory)
    tools.create_location(YARD, "Cobbles around a dry well.")
    tools.move_actor(PC, YARD)
    tools.create_encounter_map(RANGE_MAP, YARD, 24, 24, "Open cobbles.")
    tools.start_encounter(RANGE_MAP)
    tools.add_participant(PC)
    return client.app, campaign_id, tools


def _place(tools: WorldTools, name: str, column: int, row: int) -> None:
    tools.place_participant(name, column, row)


def test_a_melee_attack_beyond_reach_is_refused_until_the_attacker_moves(client_factory):
    _app, _campaign_id, tools = _range_fight(client_factory)
    _foe(tools, hp=20)
    _place(tools, PC, 0, 0)
    _place(tools, FOE, 0, 3)
    take_turn(tools, PC)

    with pytest.raises(InvalidInput, match="15 feet .* beyond .* melee reach of 5 feet"):
        tools.player_attack(FOE, "Short Sword", attack_roll=20, damage_roll=2)

    _place(tools, PC, 0, 2)
    result = tools.player_attack(FOE, "Short Sword", attack_roll=20, damage_roll=2)
    assert result["target"] == FOE
    assert result["range"] == {
        "melee": True,
        "band": "",
        "cells": 1,
        "feet": 5,
        "penalty": 0,
    }


def test_a_reach_weapon_strikes_ten_feet_but_not_fifteen(client_factory):
    _app, _campaign_id, tools = _range_fight(client_factory)
    _foe(tools, hp=20)
    _make_room(tools)
    tools.give_item(PC, "Pike", damage="1d8")
    _place(tools, PC, 0, 0)
    _place(tools, FOE, 0, 2)
    take_turn(tools, PC)

    result = tools.player_attack(FOE, "Pike", attack_roll=20, damage_roll=2)
    assert result["range"]["melee"] is True
    assert result["range"]["feet"] == 10

    _place(tools, FOE, 0, 3)
    next_turn(tools, PC)
    with pytest.raises(InvalidInput, match="15 feet .* beyond .* melee reach of 10 feet"):
        tools.player_attack(FOE, "Pike", attack_roll=20, damage_roll=2)


def test_a_thrown_axe_takes_the_long_band_penalty_and_a_shot_beyond_long_is_refused(
    client_factory,
):
    app, campaign_id, tools = _range_fight(client_factory)
    _foe(tools, hp=30, armor=20)
    _make_room(tools)
    tools.give_item(PC, "Hand Axe", damage="1d6")
    _place(tools, PC, 0, 0)
    _place(tools, FOE, 0, 2)
    take_turn(tools, PC)

    short = tools.player_attack(FOE, "Hand Axe", attack_roll=10, damage_roll=2)
    assert short["range"] == {
        "melee": False,
        "band": "short",
        "cells": 2,
        "feet": 10,
        "penalty": 0,
    }
    short_hit = _struck(app, campaign_id)[-1]

    _place(tools, FOE, 0, 3)
    next_turn(tools, PC)
    long = tools.player_attack(FOE, "Hand Axe", attack_roll=10, damage_roll=2)
    assert long["range"]["band"] == "long"
    assert long["range"]["penalty"] == -2
    long_hit = _struck(app, campaign_id)[-1]
    assert long_hit.to_hit == short_hit.to_hit - 2

    _place(tools, FOE, 0, 7)
    next_turn(tools, PC)
    with pytest.raises(InvalidInput, match="35 feet .* beyond .* long range of 30 feet"):
        tools.player_attack(FOE, "Hand Axe", attack_roll=10, damage_roll=2)

    _place(tools, FOE, 0, 3)
    next_turn(tools, PC)
    with pytest.raises(InvalidInput, match="beyond the short band of 10 feet; use the long band"):
        tools.player_attack(FOE, "Hand Axe", range_band="short", attack_roll=10, damage_roll=2)


def test_a_wall_blocks_the_line_of_sight_until_it_is_cleared(client_factory):
    _app, _campaign_id, tools = _range_fight(client_factory)
    _foe(tools, hp=20, armor=20)
    _make_room(tools)
    tools.give_item(PC, BOW, damage="1d8")
    _place(tools, PC, 0, 0)
    _place(tools, FOE, 0, 3)
    tools.set_map_cells(RANGE_MAP, [{"column": 0, "row": 1, "object": "wall"}])
    take_turn(tools, PC)

    with pytest.raises(InvalidInput, match="wall at column 0, row 1 blocks the line of sight"):
        tools.player_attack(FOE, BOW, attack_roll=20, damage_roll=2)

    tools.set_map_cells(RANGE_MAP, [{"column": 0, "row": 1, "object": ""}])
    result = tools.player_attack(FOE, BOW, attack_roll=20, damage_roll=2)
    assert result["range"]["feet"] == 15


def test_an_npc_reach_attack_is_measured_and_refused_out_of_reach(client_factory):
    app, campaign_id, tools = _range_fight(client_factory)
    tools.create_monster(
        "Pikeman",
        YARD,
        description="A guard with a pike.",
        stats=_stats(hp=20),
        attacks=[{"name": "Pike", "dice": "1d8", "reach": 10}],
        force=True,
    )
    assert _character(app, campaign_id, "Pikeman").attack_reach == {"Pike": 10}
    tools.add_participant("Pikeman")
    _place(tools, PC, 0, 0)
    _place(tools, "Pikeman", 0, 2)
    take_turn(tools, "Pikeman")

    result = tools.attack("Pikeman", "Pike", PC)
    assert result["range"]["melee"] is True
    assert result["range"]["feet"] == 10

    _place(tools, PC, 0, 5)
    state = _state(app, campaign_id)
    assert state.encounter is not None
    assert state.encounter.cells["wren"] == (0, 5)
    next_turn(tools, "Pikeman")
    with pytest.raises(InvalidInput, match="15 feet .* beyond .* melee reach of 10 feet"):
        tools.attack("Pikeman", "Pike", PC)


def test_an_npc_ranged_attack_uses_the_band_and_refuses_beyond_long(client_factory):
    app, campaign_id, tools = _range_fight(client_factory)
    tools.create_monster(
        "Archer",
        YARD,
        description="A hunter with a bow.",
        stats=_stats(hp=20, attack_bonus=4),
        attacks=[
            {"name": "Bow", "dice": "1d6", "ranges": {"short": 30, "long": 60}},
        ],
        force=True,
    )
    tools.add_participant("Archer")
    _place(tools, "Archer", 0, 0)
    _place(tools, PC, 0, 6)
    take_turn(tools, "Archer")

    short = replace(tools, rng=Rolls(3, 10)).attack("Archer", "Bow", PC)
    assert short["range"] == {
        "melee": False,
        "band": "short",
        "cells": 6,
        "feet": 30,
        "penalty": 0,
    }
    short_hit = _struck(app, campaign_id)[-1]

    _place(tools, PC, 0, 12)
    next_turn(tools, "Archer")
    long = replace(tools, rng=Rolls(3, 10)).attack("Archer", "Bow", PC)
    assert long["range"]["band"] == "long"
    assert long["range"]["penalty"] == -2
    long_hit = _struck(app, campaign_id)[-1]
    assert long_hit.to_hit == short_hit.to_hit - 2

    _place(tools, PC, 0, 13)
    next_turn(tools, "Archer")
    with pytest.raises(InvalidInput, match="65 feet .* beyond .* long range of 60 feet"):
        tools.attack("Archer", "Bow", PC)


def test_update_npc_can_give_an_existing_foe_a_reach_weapon(client_factory):
    app, _campaign_id, tools = _range_fight(client_factory)
    _foe(tools, hp=20)
    tools.update_npc(FOE, attacks=[{"name": "Longspear", "dice": "1d6", "reach": 10}])
    assert _character(app, _campaign_id, FOE).attack_reach == {"Longspear": 10}
    _place(tools, PC, 0, 0)
    _place(tools, FOE, 0, 2)
    take_turn(tools, FOE)

    result = tools.attack(FOE, "Longspear", PC)

    assert result["range"]["melee"] is True
    assert result["range"]["feet"] == 10


def test_resolve_attacks_checks_every_attacker_before_any_die_is_rolled(client_factory):
    app, campaign_id, tools = _range_fight(client_factory)
    for name in ("Near", "Far"):
        tools.create_monster(
            name,
            YARD,
            description="A hired hand.",
            stats=_stats(hp=20),
            attacks=[{"name": "Claw", "dice": "1d6"}],
            force=True,
        )
        tools.add_participant(name)
    _place(tools, PC, 5, 5)
    _place(tools, "Near", 5, 6)
    _place(tools, "Far", 5, 10)
    take_turn(tools, "Near")

    with pytest.raises(InvalidInput, match="25 feet away, beyond Far's melee reach of 5 feet"):
        tools.resolve_attacks(
            PC,
            [
                {"attacker": "Near", "attack": "Claw"},
                {"attacker": "Far", "attack": "Claw"},
            ],
            force=True,
        )

    assert _struck(app, campaign_id) == []


def test_wwn_content_declares_its_reach_range_and_sight_limits():
    wwn = content_library().rule_sets["worldswithoutnumber"]
    assert wwn.combat.ranges is not None
    assert wwn.combat.ranges.melee == 5
    assert "wall" in wwn.combat.ranges.sight_blocked_by
    assert wwn.character.features["pike"].reach == 10
    assert wwn.character.features["bow_large"].ranges == {"short": 100, "long": 600}
    assert wwn.monsters["bog_strider"].attack_reach == {"beak": 10}
    assert wwn.monsters["mound_breaker"].attack_ranges == {
        "hurled stone": {"short": 60, "long": 120}
    }


def test_unplaced_participants_keep_the_old_unmeasured_attack(client_factory):
    """A campaign that has not placed the combatants on the map leaves distance unchecked."""
    _app, _campaign_id, tools = _range_fight(client_factory)
    _foe(tools, hp=20)
    take_turn(tools, PC)

    result = tools.player_attack(FOE, "Short Sword", attack_roll=20, damage_roll=2)

    assert "range" not in result
