from __future__ import annotations

from dataclasses import replace

import pytest
from sqlmodel import Session

from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import load_state
from cygnusforge.services.market import listed_item
from tests.test_attack_modifiers import _run, _struck
from tests.test_campaigns import _create_character
from tests.test_character_creation import (
    SCRIVENER_WORK,
    WILD_TONGUE,
    _cairn_campaign,
    _complete_form,
)
from tests.test_combat import CAIRN, PC, Rolls, _character, _fight, _foe


def _held(app, campaign_id: int, name: str):
    return [item for item in _character(app, campaign_id, PC).items if item.name == name]


def test_a_carried_custom_weapon_rolls_its_own_dice(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.give_item(PC, "Cutlass", damage="1d8")
    _foe(tools, "Bo", hp=20)
    replace(tools, rng=Rolls(8)).player_attack("Bo", "Cutlass")
    assert _struck(app, campaign_id)[-1].raw == 8


def test_a_modifier_with_its_own_dice_overrides_the_weapons(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.give_item(PC, "Cutlass", damage="1d8")
    _foe(tools, "Bo")
    replace(tools, rng=Rolls(4)).player_attack("Bo", "Cutlass", modifier="impaired")
    assert _struck(app, campaign_id)[-1].raw == 4


def test_two_weapons_keep_the_higher_of_their_dice(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.give_item(PC, "Twin Daggers", damage="1d6+1d6")
    _foe(tools, "Bo", hp=20)
    replace(tools, rng=Rolls(2, 5)).player_attack("Bo", "Twin Daggers")
    assert _struck(app, campaign_id)[-1].raw == 5


def test_unarmed_fists_roll_1d4(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Bo")
    replace(tools, rng=Rolls(4)).player_attack("Bo", "fists")
    assert _struck(app, campaign_id)[-1].raw == 4
    assert CAIRN.combat.unarmed_damage == "1d4"


def test_an_uncarried_weapon_is_refused(client_factory):
    _, _, tools = _fight(client_factory)
    _foe(tools, "Bo")
    with pytest.raises(InvalidInput, match="carries no weapon named 'Cutlass'"):
        tools.player_attack("Bo", "Cutlass")


def test_a_carried_item_with_no_damage_is_refused(client_factory):
    _, _, tools = _fight(client_factory)
    tools.give_item(PC, "Rope")
    _foe(tools, "Bo")
    with pytest.raises(InvalidInput, match="Rope is not a weapon: it has no damage dice"):
        tools.player_attack("Bo", "Rope")


@pytest.mark.parametrize("damage", ["banana", "1d"])
def test_a_custom_item_with_a_bad_damage_formula_is_refused(client_factory, damage):
    _, _, tools = _fight(client_factory)
    with pytest.raises(InvalidInput, match="is not a dice formula such as 'd6'"):
        tools.give_item(PC, "Cutlass", damage=damage)


def test_damage_given_for_a_listed_item_is_refused(client_factory):
    _, _, tools = _fight(client_factory)
    with pytest.raises(InvalidInput, match="is in the catalogue, so its damage comes from there"):
        tools.give_item(PC, "Dagger", damage="1d4")


def test_a_catalogue_weapon_takes_its_damage_from_the_catalogue(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    listed = listed_item(CAIRN.marketplace, "Spear")
    assert listed is not None and listed.damage == "1d8"
    tools.give_item(PC, "Spear")
    assert _held(app, campaign_id, "Spear")[-1].damage == "1d8"


def test_a_starting_feature_weapon_carries_its_damage(client_factory):
    client, campaign_id = _cairn_campaign(client_factory)
    fields = _complete_form(client, campaign_id)
    fields["template-background"] = "scrivener"
    fields[f"field-{SCRIVENER_WORK}"] = WILD_TONGUE
    assert _create_character(client, campaign_id, name="Mara", fields=fields).status_code == 303
    with Session(client.app.state.engine) as db:
        pc = load_state(db, campaign_id).pc()
    assert pc is not None
    awl = [item for item in pc.items if item.name == "Awl"]
    assert awl and awl[0].damage == "1d6"


def test_the_tool_path_resolves_a_carried_weapons_dice(client_factory):
    _, _, tools = _fight(client_factory)
    tools.give_item(PC, "Cutlass", damage="1d8")
    ask = _run(tools, "player_attack", {"target": "Bo", "weapon": "Cutlass", "prompt": "Roll."})
    assert (ask.text, ask.dice) == ("Roll.", "1d8")
