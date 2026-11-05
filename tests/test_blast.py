from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import PARTY_SIDE
from tests.test_combat import (
    PC,
    YARD,
    Rolls,
    _fight,
    _foe,
    _set_stat,
)
from tests.test_detachment import _ally, _group
from tests.turns import take_turn


def _blaster(tools, name: str, side: str = "foes", flags: list[str] | None = None) -> None:
    """A non-group NPC whose Claw is a 1d6 attack with the given flags."""
    attack: dict[str, Any] = {"name": "Claw", "dice": "1d6"}
    if flags is not None:
        attack["flags"] = flags
    tools.create_monster(
        name,
        YARD,
        description="A blaster.",
        stats={"hp": 6, "str": 10, "dex": 10, "wil": 10},
        armor=0,
        attacks=[attack],
    )
    tools.add_participant(name, side=side)


def test_a_blast_attack_rolls_each_target_separately(client_factory):
    _, _, tools = _fight(client_factory)
    _blaster(tools, "Gorm", flags=["blast"])
    _ally(tools, "Tam")
    _ally(tools, "Wren")
    take_turn(tools, "Gorm")
    result = replace(tools, rng=Rolls(1, 1)).attack("Gorm", "Claw", "Tam", targets=["Tam", "Wren"])
    assert result["blast"] is True
    assert [entry["target"] for entry in result["targets"]] == ["Tam", "Wren"]


def test_a_blast_target_must_be_in_its_targets(client_factory):
    _, _, tools = _fight(client_factory)
    _blaster(tools, "Gorm", flags=["blast"])
    _ally(tools, "Tam")
    take_turn(tools, "Gorm")
    with pytest.raises(InvalidInput, match="must be one of the targets"):
        replace(tools, rng=Rolls(1)).attack("Gorm", "Claw", "Tam", targets=[PC])


def test_a_blast_target_list_names_each_target_once(client_factory):
    _, _, tools = _fight(client_factory)
    _blaster(tools, "Gorm", flags=["blast"])
    take_turn(tools, "Gorm")
    with pytest.raises(InvalidInput, match="lists a target twice"):
        replace(tools, rng=Rolls(1)).attack("Gorm", "Claw", PC, targets=[PC, PC])


def test_a_blast_with_no_targets_hits_the_named_target_once(client_factory):
    _, _, tools = _fight(client_factory)
    _blaster(tools, "Gorm", flags=["blast"])
    _ally(tools, "Tam")
    take_turn(tools, "Gorm")
    result = replace(tools, rng=Rolls(1)).attack("Gorm", "Claw", "Tam")
    assert result["blast"] is True
    assert [entry["target"] for entry in result["targets"]] == ["Tam"]


def test_a_blast_refuses_a_target_on_the_attackers_side(client_factory):
    _, _, tools = _fight(client_factory)
    _blaster(tools, "Gorm", flags=["blast"])
    _foe(tools, "Bo")
    take_turn(tools, "Gorm")
    with pytest.raises(InvalidInput, match="cannot attack them"):
        replace(tools, rng=Rolls(1)).attack("Gorm", "Claw", "Bo", targets=["Bo"])


def test_a_blast_refuses_a_target_owing_a_save(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _blaster(tools, "Gorm", flags=["blast"])
    _set_stat(app, campaign_id, PC, "hp", 0)
    take_turn(tools, "Gorm")
    result = replace(tools, rng=Rolls(6)).attack("Gorm", "Claw", PC)
    assert result["targets"][0]["save_due"] == "str"
    _ally(tools, "Tam")
    with pytest.raises(InvalidInput, match="must make their save first"):
        replace(tools, rng=Rolls(1, 1)).attack("Gorm", "Claw", "Tam", targets=["Tam", PC])


def test_targets_are_refused_on_an_attack_without_blast(client_factory):
    _, _, tools = _fight(client_factory)
    _foe(tools, "Gorm")
    _ally(tools, "Tam")
    take_turn(tools, "Gorm")
    with pytest.raises(InvalidInput, match="has no Blast"):
        replace(tools, rng=Rolls(1)).attack("Gorm", "Claw", "Tam", targets=["Tam"])


def test_targets_are_refused_for_a_group_attack(client_factory):
    _, _, tools = _fight(client_factory)
    _group(tools, "Swarm", side="foes")
    _ally(tools, "Tam")
    take_turn(tools, "Swarm")
    with pytest.raises(InvalidInput, match="leave targets out"):
        replace(tools, rng=Rolls(1)).attack("Swarm", "Sting", PC, targets=[PC])


def test_a_blast_by_an_individual_on_a_group_is_not_impaired(client_factory):
    _, _, tools = _fight(client_factory)
    _blaster(tools, "Gorm", side="foes", flags=["blast"])
    _group(tools, "Swarm", side=PARTY_SIDE)
    take_turn(tools, "Gorm")
    result = replace(tools, rng=Rolls(1)).attack("Gorm", "Claw", "Swarm", targets=["Swarm"])
    assert [entry["target"] for entry in result["targets"]] == ["Swarm"]


def test_a_blast_on_a_group_still_hits_every_living_individual(client_factory):
    _, _, tools = _fight(client_factory)
    _group(tools, "Swarm", side="foes")
    _ally(tools, "Tam")
    take_turn(tools, "Swarm")
    result = replace(tools, rng=Rolls(1, 1, 1)).attack("Swarm", "Sting", "Tam", modifier="enhanced")
    assert sorted(entry["target"] for entry in result["targets"]) == sorted([PC, "Tam"])


def test_resolve_attacks_refuses_a_blast_attack(client_factory):
    _, _, tools = _fight(client_factory)
    _blaster(tools, "Gorm", flags=["blast"])
    take_turn(tools, "Gorm")
    with pytest.raises(InvalidInput, match="use attack with targets"):
        replace(tools, rng=Rolls(1)).resolve_attacks(PC, [{"attacker": "Gorm", "attack": "Claw"}])


def test_an_ignores_armor_attack_skips_the_targets_armor(client_factory):
    _, _, tools = _fight(client_factory)
    _foe(tools, "Gorm")
    _ally(tools, "Tam")
    tools.set_armor("Tam", 1)
    tools.create_monster(
        "Hollow Thing",
        YARD,
        description="A hollow thing.",
        stats={"hp": 6, "str": 10, "dex": 10, "wil": 10},
        armor=0,
        attacks=[{"name": "Claw", "dice": "1d6", "flags": ["ignores_armor"]}],
    )
    tools.add_participant("Hollow Thing")
    take_turn(tools, "Gorm")
    armored = replace(tools, rng=Rolls(2)).attack("Gorm", "Claw", "Tam")
    take_turn(tools, "Hollow Thing")
    unarmored = replace(tools, rng=Rolls(2)).attack("Hollow Thing", "Claw", "Tam")
    assert armored["armor"] == 1
    assert unarmored["armor"] == 0


def test_an_unknown_attack_flag_is_refused(client_factory):
    _, _, tools = _fight(client_factory)
    with pytest.raises(InvalidInput, match="flags"):
        tools.create_monster(
            "Imp",
            YARD,
            description="An imp.",
            stats={"hp": 6, "str": 10, "dex": 10, "wil": 10},
            armor=0,
            attacks=[{"name": "Claw", "dice": "1d6", "flags": ["fly"]}],
        )


def test_a_repeated_attack_flag_is_refused(client_factory):
    _, _, tools = _fight(client_factory)
    with pytest.raises(InvalidInput, match="flags"):
        tools.create_monster(
            "Imp",
            YARD,
            description="An imp.",
            stats={"hp": 6, "str": 10, "dex": 10, "wil": 10},
            armor=0,
            attacks=[{"name": "Claw", "dice": "1d6", "flags": ["blast", "blast"]}],
        )
