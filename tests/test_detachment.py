from __future__ import annotations

from dataclasses import replace

import pytest

from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import PARTY_SIDE
from tests.test_combat import (
    PC,
    YARD,
    Rolls,
    _character,
    _fight,
    _foe,
    _hit,
    _set_stat,
)
from tests.test_morale import _morale


def _group(tools, name: str, side: str | None = None) -> None:
    """A group NPC built from the traitor-rats bestiary entry, which marks it as a group."""
    tools.create_monster(name, YARD, description="A group.", template="traitor-rats")
    tools.add_participant(name, side=side)


def _ally(tools, name: str) -> None:
    """A non-group NPC on the party's side, so a blast has two living targets."""
    tools.create_monster(
        name,
        YARD,
        description="An ally.",
        stats={"hp": 6, "str": 10, "dex": 10, "wil": 10},
        armor=0,
        attacks=[{"name": "Claw", "dice": "1d6"}],
    )
    tools.add_participant(name, side=PARTY_SIDE)


def _break_group(tools, name: str) -> None:
    """Overflow a group's 3 hp onto its Strength (5), so it owes a Strength save; the d20 roll
    of 20 fails it."""
    replace(tools, rng=Rolls(20)).player_attack(name, "Sword", 6, modifier="impaired")


def test_an_impaired_attack_may_hit_a_group(client_factory):
    _, _, tools = _fight(client_factory)
    _foe(tools, "Gorm")
    _group(tools, "Rats")
    result = replace(tools, rng=Rolls(1)).attack("Gorm", "Claw", "Rats", modifier="impaired")
    assert result["target"] == "Rats"


@pytest.mark.parametrize("modifier", ["", "enhanced"])
def test_a_normal_or_enhanced_attack_is_refused_on_a_group(client_factory, modifier):
    _, _, tools = _fight(client_factory)
    _foe(tools, "Gorm")
    _group(tools, "Rats")
    with pytest.raises(InvalidInput, match="must be impaired"):
        replace(tools, rng=Rolls(1, 1)).attack("Gorm", "Claw", "Rats", modifier=modifier)


def test_a_normal_player_attack_is_refused_on_a_group(client_factory):
    _, _, tools = _fight(client_factory)
    _group(tools, "Rats")
    with pytest.raises(InvalidInput, match="must be impaired"):
        tools.player_attack("Rats", "Sword", 1)


def test_an_impaired_player_attack_is_allowed_on_a_group(client_factory):
    _, _, tools = _fight(client_factory)
    _group(tools, "Rats")
    result = tools.player_attack("Rats", "Sword", 1, modifier="impaired")
    assert result["target"] == "Rats"


def test_a_group_blast_rolls_one_enhanced_die_per_living_individual(client_factory):
    _, _, tools = _fight(client_factory)
    _group(tools, "Rats", side="foes")
    _ally(tools, "Tam")
    # Mara's HP is rolled at random, so a 1-point hit can take her to 0 and roll an injury die first.
    result = replace(tools, rng=Rolls(1, 1, 1)).attack("Rats", "Bite", "Mara", modifier="enhanced")
    assert result["blast"] is True
    assert sorted(entry["target"] for entry in result["targets"]) == [PC, "Tam"]


def test_a_group_blast_refuses_a_target_owing_a_save(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _group(tools, "Rats", side="foes")
    _foe(tools, "Gorm")
    _set_stat(app, campaign_id, PC, "hp", 0)
    _hit(tools, "Gorm", PC, 1)
    with pytest.raises(InvalidInput, match="must make their save first"):
        replace(tools, rng=Rolls(1)).attack("Rats", "Bite", PC)


def test_a_group_blast_refuses_a_same_side_target(client_factory):
    _, _, tools = _fight(client_factory)
    _group(tools, "Rats", side="foes")
    _foe(tools, "Gorm")
    with pytest.raises(InvalidInput, match="cannot attack them"):
        replace(tools, rng=Rolls(1)).attack("Rats", "Bite", "Gorm")


def test_a_group_blast_refuses_another_group(client_factory):
    _, _, tools = _fight(client_factory)
    _group(tools, "Rats", side="foes")
    _group(tools, "Bees", side=PARTY_SIDE)
    with pytest.raises(InvalidInput, match="another group"):
        replace(tools, rng=Rolls(1)).attack("Rats", "Bite", "Bees")


def test_resolve_attacks_refuses_a_group_attacker(client_factory):
    _, _, tools = _fight(client_factory)
    _group(tools, "Rats")
    with pytest.raises(InvalidInput, match="attack"):
        tools.resolve_attacks(PC, [{"attacker": "Rats", "attack": "Bite", "modifier": "enhanced"}])


def test_a_failed_critical_save_leaves_a_group_standing_awaiting_the_warden(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _group(tools, "Rats")
    _break_group(tools, "Rats")
    rats = _character(app, campaign_id, "Rats")
    assert rats.critical_due is True
    assert rats.defeated is False


def test_a_group_awaiting_the_warden_cannot_attack(client_factory):
    _, _, tools = _fight(client_factory)
    _foe(tools, "Gorm")
    _group(tools, "Rats", side="foes")
    _break_group(tools, "Rats")
    with pytest.raises(InvalidInput, match="resolve_group_break"):
        replace(tools, rng=Rolls(1)).attack("Rats", "Bite", PC)


def test_a_group_awaiting_the_warden_cannot_be_attacked(client_factory):
    _, _, tools = _fight(client_factory)
    _foe(tools, "Gorm")
    _group(tools, "Rats")
    _break_group(tools, "Rats")
    with pytest.raises(InvalidInput, match="resolve_group_break"):
        _hit(tools, "Gorm", "Rats", 1)


def test_resolve_group_break_defeat_defeats_the_group(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Gorm")
    _group(tools, "Rats")
    _break_group(tools, "Rats")
    result = tools.resolve_group_break("Rats", "defeat")
    assert result == {"character": "Rats", "outcome": "defeat"}
    rats = _character(app, campaign_id, "Rats")
    assert rats.defeated is True
    assert rats.critical_due is False


def test_resolve_group_break_flee_makes_the_group_flee(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Gorm")
    _group(tools, "Rats")
    _break_group(tools, "Rats")
    tools.resolve_group_break("Rats", "flee")
    rats = _character(app, campaign_id, "Rats")
    assert rats.fled is True
    assert rats.critical_due is False


def test_resolve_group_break_refuses_an_unknown_outcome(client_factory):
    _, _, tools = _fight(client_factory)
    _group(tools, "Rats")
    _break_group(tools, "Rats")
    with pytest.raises(InvalidInput, match="defeat' or 'flee"):
        tools.resolve_group_break("Rats", "surrender")


def test_resolve_group_break_refuses_a_group_that_is_not_critical(client_factory):
    _, _, tools = _fight(client_factory)
    _group(tools, "Rats")
    with pytest.raises(InvalidInput, match="no failed critical"):
        tools.resolve_group_break("Rats", "defeat")


def test_resolve_group_break_refuses_a_character_that_is_not_a_group(client_factory):
    _, _, tools = _fight(client_factory)
    _foe(tools, "Gorm")
    with pytest.raises(InvalidInput, match="not a group"):
        tools.resolve_group_break("Gorm", "defeat")


def test_resolve_group_break_refuses_an_unknown_name(client_factory):
    _, _, tools = _fight(client_factory)
    with pytest.raises(InvalidInput, match="no character named"):
        tools.resolve_group_break("Nobody", "defeat")


def test_a_group_hit_triggers_no_morale_check(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _group(tools, "Rats")
    result = replace(tools, rng=Rolls()).player_attack("Rats", "Sword", 3, modifier="impaired")
    assert "morale" not in result
    assert _morale(app, campaign_id) == {}
