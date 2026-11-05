from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import pytest

from cygnusloom.content.definitions import ContentError
from cygnusloom.content.library import _load_combat
from cygnusloom.llm.base import ToolRequest
from cygnusloom.rules import cairn2e
from cygnusloom.rules.context import ToolContext
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import Struck, decode, events_since
from cygnusloom.services.tools import ToolRun, run_tool
from tests.test_combat import (
    CAIRN,
    LIBRARY,
    PC,
    Rolls,
    _character,
    _db,
    _fight,
    _foe,
    _hit,
    _no_speak,
    _set_stat,
)
from tests.test_combat import _combat as _combat_definition
from tests.turns import take_turn

IMPAIRED = "1d4"
ENHANCED = "1d12"


def _struck(app, campaign_id: int) -> list[Struck]:
    with _db(app) as db:
        return [decode(row) for row in events_since(db, campaign_id) if row.kind == Struck.kind]


def _ctx(tools) -> ToolContext:
    return ToolContext(
        world=tools, speak=_no_speak, ooc=lambda narrate: None, rules={}, lookup_tools={}
    )


def _run(tools, name: str, arguments: dict[str, Any], answer=None):
    request = ToolRequest(id="call_1", name=name, arguments=json.dumps(arguments))
    return asyncio.run(
        run_tool(LIBRARY.rule_sets["cairn2e"].tool_calls, request, answer, ctx=_ctx(tools))
    )


def test_the_rule_set_names_the_impaired_and_enhanced_dice():
    assert CAIRN.combat.modifiers == {"impaired": IMPAIRED, "enhanced": ENHANCED}


def test_an_impaired_npc_attack_rolls_the_impaired_dice(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing", claw="1d6")
    _foe(tools, "Bo")
    take_turn(tools, "Grave Thing")
    result = replace(tools, rng=Rolls(4)).attack("Grave Thing", "Claw", "Bo", modifier="impaired")
    assert result["raw"] == 4
    assert _struck(app, campaign_id)[-1].modifier == "impaired"
    with pytest.raises(AssertionError, match="cannot show 5"):
        replace(tools, rng=Rolls(5)).attack("Grave Thing", "Claw", "Bo", modifier="impaired")


def test_an_enhanced_npc_attack_rolls_the_enhanced_dice(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing", claw="1d6")
    _foe(tools, "Bo", hp=20)
    take_turn(tools, "Grave Thing")
    result = replace(tools, rng=Rolls(12)).attack("Grave Thing", "Claw", "Bo", modifier="Enhanced")
    assert result["raw"] == 12
    assert _struck(app, campaign_id)[-1].modifier == "enhanced"


def test_normal_is_the_attacks_own_dice_and_no_modifier(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing", claw="1d6")
    _foe(tools, "Bo", hp=20)
    take_turn(tools, "Grave Thing")
    assert (
        replace(tools, rng=Rolls(6)).attack("Grave Thing", "Claw", "Bo", modifier="normal")["raw"]
        == 6
    )
    assert _struck(app, campaign_id)[-1].modifier == ""


def test_an_unknown_modifier_is_an_error(client_factory):
    _, _, tools = _fight(client_factory)
    _foe(tools, "Grave Thing")
    _foe(tools, "Bo")
    take_turn(tools, "Grave Thing")
    with pytest.raises(InvalidInput, match="unknown attack modifier 'stunned'"):
        tools.attack("Grave Thing", "Claw", "Bo", modifier="stunned")


def test_an_unknown_modifier_on_a_player_attack_is_an_error(client_factory):
    _, _, tools = _fight(client_factory)
    _foe(tools, "Bo")
    take_turn(tools, PC)
    with pytest.raises(InvalidInput, match="unknown attack modifier"):
        tools.player_attack("Bo", "Sword", modifier="stunned")


def test_a_player_hit_records_its_modifier(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Bo", hp=20)
    take_turn(tools, PC)
    tools.player_attack("Bo", "Sword", modifier="impaired")
    assert _struck(app, campaign_id)[-1].modifier == "impaired"


def test_player_attack_asks_for_the_modifiers_dice(client_factory):
    _, _, tools = _fight(client_factory)
    _foe(tools, "Bo")
    arguments = {"target": "Bo", "weapon": "Sword", "prompt": "Roll."}

    for modifier, dice in (("enhanced", ENHANCED), ("impaired", IMPAIRED), ("normal", "1d8")):
        ask = _run(tools, "player_attack", {**arguments, "modifier": modifier})
        assert (ask.text, ask.dice) == ("Roll.", dice), modifier


def test_the_gm_cannot_change_the_dice_of_a_modified_player_attack(client_factory):
    _, _, tools = _fight(client_factory)
    _foe(tools, "Bo")
    ask = _run(
        tools,
        "player_attack",
        {"target": "Bo", "weapon": "Sword", "prompt": "Roll.", "modifier": "impaired"},
    )
    assert ask.dice == IMPAIRED


def test_a_player_attack_with_an_unknown_modifier_returns_an_error(client_factory):
    _, _, tools = _fight(client_factory)
    _foe(tools, "Bo")
    run = _run(
        tools,
        "player_attack",
        {"target": "Bo", "weapon": "Sword", "prompt": "Roll.", "modifier": "stunned"},
        answer=None,
    )
    assert not hasattr(run, "dice")
    if isinstance(run, ToolRun):
        assert "'modifier' must be one of" in json.loads(run.output)["error"]


def test_resolve_attacks_keeps_the_highest_roll_and_applies_one_hit(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing", claw="1d6")
    _foe(tools, "Bo", claw="1d6")
    _set_stat(app, campaign_id, PC, "hp", 0)
    result = replace(tools, rng=Rolls(2, 5)).resolve_attacks(
        PC,
        [{"attacker": "Grave Thing", "attack": "Claw"}, {"attacker": "Bo", "attack": "Claw"}],
        force=True,
    )
    assert result["raw"] == 5
    assert result["harm"] == {"str": 5}
    assert result["save_due"] == "str"
    assert _character(app, campaign_id, PC).harm["str"] == 5
    strikes = _struck(app, campaign_id)
    assert len(strikes) == 1
    assert strikes[0].attacker == "Bo"
    assert strikes[0].rolled == (
        "Rolls: Grave Thing's Claw rolled 2; Bo's Claw rolled 5; the highest hits."
    )


def test_resolve_attacks_ties_go_to_the_earlier_attack(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing", claw="1d6")
    _foe(tools, "Bo", claw="1d6")
    _set_stat(app, campaign_id, PC, "hp", 0)
    replace(tools, rng=Rolls(4, 4)).resolve_attacks(
        PC,
        [{"attacker": "Grave Thing", "attack": "Claw"}, {"attacker": "Bo", "attack": "Claw"}],
        force=True,
    )
    assert _struck(app, campaign_id)[-1].attacker == "Grave Thing"


def test_resolve_attacks_uses_each_attacks_modifier_and_records_it(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing", claw="1d6")
    _foe(tools, "Bo", claw="1d6")
    _set_stat(app, campaign_id, PC, "hp", 0)
    replace(tools, rng=Rolls(3, 12)).resolve_attacks(
        PC,
        [
            {"attacker": "Grave Thing", "attack": "Claw", "modifier": "impaired"},
            {"attacker": "Bo", "attack": "Claw", "modifier": "enhanced"},
        ],
        force=True,
    )
    strike = _struck(app, campaign_id)[-1]
    assert (strike.attacker, strike.raw) == ("Bo", 12)
    assert "Grave Thing's Claw (impaired) rolled 3" in strike.rolled
    assert "Bo's Claw (enhanced) rolled 12" in strike.rolled


def test_resolve_attacks_with_one_attack_has_no_rolled_record(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing", claw="1d6")
    _set_stat(app, campaign_id, PC, "hp", 0)
    take_turn(tools, "Grave Thing")
    replace(tools, rng=Rolls(3)).resolve_attacks(
        PC, [{"attacker": "Grave Thing", "attack": "Claw"}]
    )
    assert _struck(app, campaign_id)[-1].rolled == ""


def test_resolve_attacks_needs_the_attackers_turn_unless_forced(client_factory):
    """Several attackers at once steps outside the order, so it is refused until the GM forces it;
    one attacker's several attacks happen on that attacker's turn."""
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing", claw="1d6")
    _foe(tools, "Bo", claw="1d6")
    both = [
        {"attacker": "Grave Thing", "attack": "Claw"},
        {"attacker": "Bo", "attack": "Claw"},
    ]

    with pytest.raises(InvalidInput, match="no one is taking a turn"):
        replace(tools, rng=Rolls(1, 1)).resolve_attacks(PC, both)
    assert _struck(app, campaign_id) == []

    take_turn(tools, "Grave Thing")
    with pytest.raises(InvalidInput, match="it is Grave Thing's turn"):
        replace(tools, rng=Rolls(1, 1)).resolve_attacks(PC, both)
    result = replace(tools, rng=Rolls(2, 5)).resolve_attacks(
        PC, [{"attacker": "Grave Thing", "attack": "Claw"}]
    )
    assert result["target"] == PC


@pytest.mark.parametrize(
    ("attacks", "message"),
    [
        ([], "list at least one attack"),
        ([{"attacker": "Grave Thing"}], "each attack needs an attacker and an attack name"),
        ([{"attacker": "Grave Thing", "attack": "Sting"}], "has no attack named 'Sting'"),
        (
            [{"attacker": "Grave Thing", "attack": "Claw", "modifier": "stunned"}],
            "unknown attack modifier",
        ),
        ([{"attacker": "Nobody", "attack": "Claw"}], "Nobody"),
        ([{"attacker": PC, "attack": "Claw"}], "player character"),
        (
            [{"attacker": "Bo", "attack": "Claw"}, {"attacker": "Grave Thing", "attack": "Sting"}],
            "Sting",
        ),
    ],
)
def test_resolve_attacks_checks_every_attack_before_rolling(client_factory, attacks, message):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing", claw="1d6")
    _foe(tools, "Bo", claw="1d6")
    # With no dice at all, any roll would fail with an IndexError rather than InvalidInput.
    with pytest.raises(InvalidInput, match=message):
        replace(tools, rng=Rolls()).resolve_attacks(PC, attacks, force=True)
    assert _struck(app, campaign_id) == []


def test_resolve_attacks_rejects_an_attacker_on_the_targets_side(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing", claw="1d6")
    _foe(tools, "Bo", claw="1d6")
    with pytest.raises(InvalidInput, match="is on Grave Thing's side and cannot attack them"):
        replace(tools, rng=Rolls()).resolve_attacks(
            "Grave Thing", [{"attacker": "Bo", "attack": "Claw"}], force=True
        )
    assert _struck(app, campaign_id) == []


def test_resolve_attacks_rejects_a_defeated_attacker(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing", claw="2d6")
    _foe(tools, "Bo")
    # Two of the dice are the claw, then the save for the overflow, then a morale roll for the
    # first casualty and one for half the side.
    take_turn(tools, "Grave Thing")
    _hit(tools, "Grave Thing", "Bo", 6, 5, 20, 1, 1)
    assert _character(app, campaign_id, "Bo").defeated is True
    with pytest.raises(InvalidInput):
        replace(tools, rng=Rolls()).resolve_attacks(
            PC, [{"attacker": "Bo", "attack": "Claw"}], force=True
        )


def test_resolve_attacks_through_the_tool_takes_modifiers(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing", claw="1d6")
    _foe(tools, "Bo", claw="1d6")
    _set_stat(app, campaign_id, PC, "hp", 0)
    run = _run(
        replace(tools, rng=Rolls(2, 3)),
        "resolve_attacks",
        {
            "target": PC,
            "force": True,
            "attacks": [
                {"attacker": "Grave Thing", "attack": "Claw", "modifier": "impaired"},
                {"attacker": "Bo", "attack": "Claw"},
            ],
        },
    )
    assert isinstance(run, ToolRun)
    assert "error" not in json.loads(run.output)
    assert _struck(app, campaign_id)[-1].attacker == "Bo"


def test_set_armor_accepts_three_and_rejects_four(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Bo")
    assert tools.set_armor("Bo", 3)["armor"] == 3
    assert _character(app, campaign_id, "Bo").armor == 3
    with pytest.raises(InvalidInput, match="armor must be a whole number from 0 to 3"):
        tools.set_armor("Bo", 4)
    assert _character(app, campaign_id, "Bo").armor == 3


def test_every_monster_armor_is_within_the_book_maximum():
    assert all(monster.armor <= 3 for monster in CAIRN.monsters.values()), (
        "a monster's armor is above 3"
    )


def test_combat_modifiers_are_read_optionally_and_lowercased():
    combat = _load_combat(
        _combat_definition(modifiers={"Impaired": "1d4"}),
        {"cairn2e": cairn2e},
        CAIRN.character,
        {},
        {},
        "cairn2e",
    )
    assert combat.modifiers == {"impaired": "1d4"}
    plain = _load_combat(
        _combat_definition(), {"cairn2e": cairn2e}, CAIRN.character, {}, {}, "cairn2e"
    )
    assert plain.modifiers == {}


@pytest.mark.parametrize(
    ("modifiers", "message"),
    [
        ("impaired", "'modifiers' must be an object"),
        ({"normal": "1d4"}, "'normal' is reserved"),
        ({"Impaired": "1d4", "impaired": "1d6"}, "listed twice"),
        ({"impaired": "four"}, "must be a dice formula"),
        ({"impaired": 4}, "must be a dice formula"),
        ({"  ": "1d4"}, "needs a name"),
    ],
)
def test_a_bad_modifier_is_rejected_when_the_rule_set_loads(modifiers, message):
    with pytest.raises(ContentError, match=message):
        _load_combat(
            _combat_definition(modifiers=modifiers),
            {"cairn2e": cairn2e},
            CAIRN.character,
            {},
            {},
            "cairn2e",
        )


def test_a_struck_event_names_its_modifier_and_rolls():
    plain = Struck(
        name="Mara", attacker="Grave Thing", weapon="Claw", raw=2, armor=0, harm={"hp": 2}
    )
    assert plain.render() == "Grave Thing strikes Mara with the Claw for 2; Mara takes 2 harm."
    modified = Struck(
        name="Mara",
        attacker="Bo",
        weapon="Claw",
        raw=5,
        armor=0,
        harm={"hp": 5},
        modifier="enhanced",
        rolled="Rolls: Grave Thing's Claw rolled 2; Bo's Claw (enhanced) rolled 5; the highest hits.",
    )
    assert modified.render() == (
        "Bo strikes Mara with the Claw (enhanced) for 5; Mara takes 5 harm. "
        "Rolls: Grave Thing's Claw rolled 2; Bo's Claw (enhanced) rolled 5; the highest hits."
    )
