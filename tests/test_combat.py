from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Callable
from dataclasses import replace
from types import ModuleType
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusforge.content.definitions import ContentError, Definition
from cygnusforge.content.library import MonsterTemplate, _load_combat, _load_monster
from cygnusforge.llm.base import ToolRequest
from cygnusforge.rules import cairn2e
from cygnusforge.rules.cairn2e import save_succeeds
from cygnusforge.rules.context import ToolContext
from cygnusforge.services import encounters
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import NPC_KIND
from cygnusforge.services.tools import Ask, RollAnswer, ToolRun, run_tool
from cygnusforge.services.world import WorldTools
from tests.conftest import content_library
from tests.test_campaigns import _create_character
from tests.test_character_creation import _cairn_campaign, _complete_form
from tests.test_world import _db, _state

LIBRARY = content_library()
CAIRN = LIBRARY.rule_sets["cairn2e"]
PC = "Mara"
YARD = "Stonegate Yard"
MAP = "Yard Green"
FULL_STATS = {"hp": 6, "str": 10, "dex": 10, "wil": 10}


class Rolls(random.Random):
    """Dice that show the given values, in order. A value outside the die's range fails the test."""

    def __init__(self, *values: int):
        super().__init__(0)
        self.values = list(values)

    def randint(self, low: int, high: int) -> int:
        value = self.values.pop(0)
        assert low <= value <= high, f"a die of {low} to {high} cannot show {value}"
        return value


def _tools(app: FastAPI, campaign_id: int, rng: random.Random | None = None) -> WorldTools:
    return WorldTools(
        engine=app.state.engine,
        campaign_id=campaign_id,
        tables=LIBRARY.core.tables,
        rng=rng or random.Random(0),
        character=CAIRN.character,
        grid=CAIRN.grid,
        combat=CAIRN.combat,
        monsters=CAIRN.monsters,
    )


def _fight(
    client_factory: Callable[[], TestClient], rolled: bool = True
) -> tuple[FastAPI, int, WorldTools]:
    """A Cairn campaign with Mara in the yard, the yard mapped, and an encounter under way. Unless
    `rolled` is False, Mara has rolled for initiative, and the order is set."""
    client, campaign_id = _cairn_campaign(client_factory)
    created = _create_character(
        client, campaign_id, name=PC, fields=_complete_form(client, campaign_id)
    )
    assert created.status_code == 303
    tools = _tools(client.app, campaign_id)
    tools.create_location(YARD, "Cobbles around a dry well.")
    tools.move_actor(PC, YARD)
    tools.create_encounter_map(MAP, YARD, 10, 10, "Open cobbles.")
    tools.start_encounter(MAP)
    tools.add_participant(PC)
    if rolled:
        tools.player_initiative(1)
    return client.app, campaign_id, tools


def _foe(tools: WorldTools, name: str, hp: int = 6, armor: int = 0, claw: str = "1d6") -> None:
    tools.create_monster(
        name,
        YARD,
        description="A foe.",
        stats={"hp": hp, "str": 10, "dex": 10, "wil": 10},
        armor=armor,
        attacks=[{"name": "Claw", "dice": claw}],
    )
    tools.add_participant(name)


def _hit(tools: WorldTools, attacker: str, target: str, *dice: int) -> dict[str, Any]:
    """`attacker` swings its Claw at `target`, with the given dice."""
    return replace(tools, rng=Rolls(*dice)).attack(attacker, "Claw", target)


def _character(app: FastAPI, campaign_id: int, name: str):
    return _state(app, campaign_id).characters[name.casefold()]


@pytest.mark.parametrize(
    ("roll", "score", "passed"),
    [(1, 0, True), (1, 3, True), (10, 10, True), (11, 10, False), (20, 20, False), (19, 18, False)],
)
def test_a_save_passes_at_or_under_the_score_and_a_one_always_passes(roll, score, passed):
    assert save_succeeds(roll, score) is passed


def _monster(**changes: Any) -> Definition:
    data: dict[str, Any] = {
        "type": "monster",
        "id": "ghoul",
        "name": "Ghoul",
        "description": "A foe.",
        "stats": dict(FULL_STATS),
        "armor": 0,
        "attacks": {"claw": "1d6"},
    }
    data.update(changes)
    return Definition(type="monster", id="ghoul", data=data)


def test_a_monster_definition_is_read_into_a_statblock():
    template = _load_monster(_monster(armor=2), CAIRN.character, "cairn2e")
    assert template == MonsterTemplate(
        id="ghoul",
        name="Ghoul",
        description="A foe.",
        stats=FULL_STATS,
        armor=2,
        attacks={"claw": "1d6"},
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"stats": {"hp": 6, "str": 10, "dex": 10}}, "'stats' must give a whole number"),
        ({"stats": {**FULL_STATS, "hp": "6"}}, "stat 'hp' must be a whole number"),
        ({"armor": -1}, "'armor' must be a whole number"),
        ({"attacks": ["claw"]}, "'attacks' must be an object"),
        ({"attacks": {"claw": "d6x"}}, "attack 'claw' must be a dice formula"),
        ({"attacks": {"": "1d6"}}, "attack names must be non-empty text"),
    ],
)
def test_a_bad_monster_definition_is_rejected(changes, message):
    with pytest.raises(ContentError, match=message):
        _load_monster(_monster(**changes), CAIRN.character, "cairn2e")


def _combat(**changes: Any) -> Definition:
    data: dict[str, Any] = {
        "type": "combat",
        "id": "cairn",
        "python_module": "cairn2e",
        "hit_points": "hp",
        "overflow": "str",
        "initiative_stat": "dex",
    }
    data.update(changes)
    return Definition(type="combat", id="cairn", data=data)


def test_a_combat_definition_is_read_with_its_module_and_stats():
    combat = _load_combat(_combat(), {"cairn2e": cairn2e}, CAIRN.character, "cairn2e")
    assert (combat.hit_points, combat.overflow, combat.initiative_stat) == ("hp", "str", "dex")
    assert combat.module is cairn2e


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"python_module": "nope"}, "unknown python_module 'nope'"),
        ({"overflow": "luck"}, "'overflow' names no stat"),
        ({"hit_points": "str"}, "must name different stats"),
    ],
)
def test_a_bad_combat_definition_is_rejected(changes, message):
    with pytest.raises(ContentError, match=message):
        _load_combat(_combat(**changes), {"cairn2e": cairn2e}, CAIRN.character, "cairn2e")


def test_a_combat_module_needs_the_save_and_initiative_functions():
    with pytest.raises(ContentError, match="'save_succeeds' is not a function"):
        _load_combat(
            _combat(python_module="bare"),
            {"bare": ModuleType("bare")},
            CAIRN.character,
            "cairn2e",
        )


def test_a_statblock_template_gives_the_starting_values(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    result = tools.create_monster("Rover", YARD, template="blink-dog")
    assert result["template"] == "blink-dog"
    assert result["stats"] == {"hp": 5, "str": 11, "dex": 14, "wil": 5}
    assert result["armor"] == 0
    assert result["attacks"] == {"bite": "1d6"}
    rover = _character(app, campaign_id, "Rover")
    assert rover.kind == NPC_KIND
    assert rover.attacks == {"bite": "1d6"}


def test_given_values_replace_the_template_and_the_rest_are_kept(client_factory):
    _, _, tools = _fight(client_factory)
    result = tools.create_monster(
        "Rover",
        YARD,
        template="blink-dog",
        stats={"hp": 2, "str": None},
        armor=1,
        attacks=[{"name": "Snap", "dice": "1d4"}],
    )
    assert result["stats"] == {"hp": 2, "str": 11, "dex": 14, "wil": 5}
    assert result["armor"] == 1
    assert result["attacks"] == {"Snap": "1d4"}


BASE_MONSTER: dict[str, Any] = {
    "name": "Thing",
    "location": YARD,
    "description": "A thing.",
    "stats": FULL_STATS,
}


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"stats": {"hp": 3}}, "missing stats: Strength"),
        ({"template": "no-such-beast"}, "there is no monster template"),
        ({"description": None}, "a description is needed"),
        ({"stats": {**FULL_STATS, "luck": 2}}, "unknown stats: luck"),
        ({"stats": {**FULL_STATS, "hp": 1000}}, "Hit Protection must be"),
        ({"armor": -1}, "armor"),
        ({"attacks": [{"name": "Bite", "dice": "d6x"}]}, "needs a dice formula"),
        ({"attacks": [{"name": "Bite", "dice": "1d6"}, {"name": "bite", "dice": "1d4"}]}, "twice"),
    ],
)
def test_a_monster_is_refused_when_it_is_incomplete_or_wrong(client_factory, changes, message):
    _, _, tools = _fight(client_factory)
    with pytest.raises(InvalidInput, match=message):
        tools.create_monster(**{**BASE_MONSTER, **changes})


def test_search_finds_statblocks_by_the_words_in_their_text(client_factory):
    _, _, tools = _fight(client_factory)
    found = [monster["id"] for monster in tools.search_monster("dog")["monsters"]]
    assert "blink-dog" in found
    assert tools.search_monster("no-such-creature")["monsters"] == []


def test_attacks_wait_until_player_characters_roll_for_initiative(client_factory):
    app, campaign_id, tools = _fight(client_factory, rolled=False)
    _foe(tools, "Ghoul")
    with pytest.raises(InvalidInput, match="initiative is not set yet; Mara must roll"):
        _hit(tools, "Ghoul", PC, 2)
    with pytest.raises(InvalidInput, match="initiative is not set yet"):
        tools.player_attack("Ghoul", "Sword", 2)
    with _db(app) as db, pytest.raises(InvalidInput, match="initiative is not set yet"):
        encounters.begin_turn(db, campaign_id, "Ghoul")

    result = tools.player_initiative(1)
    assert result["passed"] is True
    assert result["still_waiting"] == []
    assert _hit(tools, "Ghoul", PC, 1)["harm"] == {"hp": 1}


@pytest.mark.parametrize(("roll", "first"), [(1, PC), (20, "Ghoul")])
def test_passing_player_characters_act_first_in_round_one(client_factory, roll, first):
    app, campaign_id, tools = _fight(client_factory, rolled=False)
    _foe(tools, "Ghoul")
    tools.player_initiative(roll)
    assert _state(app, campaign_id).encounter.order[0] == first


def test_a_roll_for_initiative_is_refused_when_none_is_owed(client_factory):
    _, _, tools = _fight(client_factory)
    with pytest.raises(InvalidInput, match="no one is rolling for initiative"):
        tools.player_initiative(5)


def test_hit_points_take_harm_before_strength(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Ghoul")
    _foe(tools, "Bo")
    result = _hit(tools, "Ghoul", "Bo", 4)
    assert result["armor"] == 0
    assert result["harm"] == {"hp": 4}
    assert result["save_due"] is None
    assert result["defeated"] is False
    assert _character(app, campaign_id, "Bo").harm == {"hp": 4}


def test_armor_absorbs_before_hit_points(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Ghoul")
    _foe(tools, "Bo")
    tools.set_armor("Bo", 3)
    result = _hit(tools, "Ghoul", "Bo", 5)
    assert result["armor"] == 3
    assert result["harm"] == {"hp": 2}
    assert _character(app, campaign_id, "Bo").armor == 3


def test_armor_that_absorbs_the_whole_hit_causes_no_harm(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Ghoul")
    _foe(tools, "Bo")
    tools.set_armor("Bo", 6)
    result = _hit(tools, "Ghoul", "Bo", 4)
    assert result["harm"] == {}
    assert _character(app, campaign_id, "Bo").harm == {}


def test_overflow_comes_off_strength_and_a_passed_save_keeps_the_monster_standing(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Ghoul", claw="1d6+1d6")
    _foe(tools, "Bo")
    result = _hit(tools, "Ghoul", "Bo", 6, 5, 1)
    assert result["harm"] == {"hp": 6, "str": 5}
    assert result["save"] == {"stat": "str", "roll": 1, "score": 5, "passed": True}
    assert result["defeated"] is False
    bo = _character(app, campaign_id, "Bo")
    assert bo.harm == {"hp": 6, "str": 5}
    assert bo.save_due is None


def test_a_failed_save_defeats_a_monster(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Ghoul", claw="1d6+1d6")
    _foe(tools, "Bo")
    result = _hit(tools, "Ghoul", "Bo", 6, 5, 20)
    assert result["save"]["passed"] is False
    assert result["defeated"] is True
    assert _character(app, campaign_id, "Bo").defeated is True


def test_a_defeated_monster_cannot_be_hit_or_attack(client_factory):
    _, _, tools = _fight(client_factory)
    _foe(tools, "Ghoul", claw="1d6+1d6")
    _foe(tools, "Bo")
    _hit(tools, "Ghoul", "Bo", 6, 5, 20)
    with pytest.raises(InvalidInput, match="Bo is defeated"):
        _hit(tools, "Ghoul", "Bo", 1)
    with pytest.raises(InvalidInput, match="Bo is defeated"):
        _hit(tools, "Bo", "Ghoul", 1)


def test_an_unknown_attack_is_refused_and_the_options_are_listed(client_factory):
    _, _, tools = _fight(client_factory)
    _foe(tools, "Ghoul")
    _foe(tools, "Bo")
    with pytest.raises(InvalidInput, match="has no attack named 'Sting'; attacks: Claw"):
        tools.attack("Ghoul", "Sting", "Bo")


def test_a_player_character_cannot_use_the_monster_attack(client_factory):
    _, _, tools = _fight(client_factory)
    _foe(tools, "Bo")
    with pytest.raises(InvalidInput, match="use player_attack instead"):
        tools.attack(PC, "Claw", "Bo")


def test_a_player_hit_is_the_damage_the_player_rolled(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Bo")
    result = tools.player_attack("Bo", "Sword", 4)
    assert result == {
        "target": "Bo",
        "raw": 4,
        "armor": 0,
        "harm": {"hp": 4},
        "save_due": None,
        "defeated": False,
    }
    assert _character(app, campaign_id, "Bo").harm == {"hp": 4}


@pytest.mark.parametrize(
    ("damage", "weapon", "message"),
    [(-1, "Sword", "damage"), (1001, "Sword", "damage"), (2, "  ", "weapon must name the attack")],
)
def test_a_player_hit_is_refused_for_a_bad_damage_or_weapon(
    client_factory, damage, weapon, message
):
    _, _, tools = _fight(client_factory)
    _foe(tools, "Bo")
    with pytest.raises(InvalidInput, match=message):
        tools.player_attack("Bo", weapon, damage)


def test_overflow_on_a_player_character_waits_for_the_player_to_save(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Ghoul", claw="1d6+1d6+1d6")
    result = _hit(tools, "Ghoul", PC, 6, 6, 6)
    assert result["save_due"] == "str"
    assert "save" not in result
    assert result["defeated"] is False
    assert _character(app, campaign_id, PC).save_due == "str"

    with pytest.raises(InvalidInput, match="must make their str save first"):
        _hit(tools, "Ghoul", PC, 1, 1, 1)
    with pytest.raises(InvalidInput, match="must make their str save first"):
        tools.player_attack(PC, "Sword", 1)


def test_a_player_character_who_fails_the_save_is_defeated(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Ghoul", claw="1d6+1d6+1d6")
    _hit(tools, "Ghoul", PC, 6, 6, 6)
    result = tools.player_save(20)
    assert result["passed"] is False
    assert result["stat"] == "str"
    assert _character(app, campaign_id, PC).defeated is True


def test_a_player_character_who_passes_the_save_stands(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Ghoul", claw="1d6+1d6+1d6")
    _hit(tools, "Ghoul", PC, 6, 6, 6)
    result = tools.player_save(1)
    assert result["passed"] is True
    pc = _character(app, campaign_id, PC)
    assert pc.save_due is None
    assert pc.defeated is False


def test_a_player_save_is_refused_when_none_is_owed(client_factory):
    _, _, tools = _fight(client_factory)
    with pytest.raises(InvalidInput, match="no player character owes a save"):
        tools.player_save(5)


def test_set_armor_is_refused_for_an_unknown_name(client_factory):
    _, _, tools = _fight(client_factory)
    with pytest.raises(InvalidInput, match="there is no character named 'Nobody'"):
        tools.set_armor("Nobody", 1)


def test_combat_is_refused_when_the_rule_set_has_none(client_factory):
    app, campaign_id, _ = _fight(client_factory)
    plain = replace(_tools(app, campaign_id), combat=None)
    with pytest.raises(InvalidInput, match="this rule set has no combat"):
        plain.player_attack("Ghoul", "Sword", 1)


async def _no_speak(*_: Any) -> None:
    return None


def test_player_attack_asks_for_damage_then_resolves_through_the_tool(client_factory):
    _, _, tools = _fight(client_factory)
    _foe(tools, "Ghoul", hp=6)
    ctx = ToolContext(
        world=tools, speak=_no_speak, ooc=lambda narrate: None, rules={}, lookup_tools={}
    )
    request = ToolRequest(
        id="call_1",
        name="player_attack",
        arguments=json.dumps(
            {
                "target": "Ghoul",
                "weapon": "Sword",
                "prompt": "Roll your sword damage.",
                "damage_dice": "1d8",
            }
        ),
    )
    ask = asyncio.run(run_tool(LIBRARY.rule_sets["cairn2e"].tool_calls, request, ctx=ctx))
    assert isinstance(ask, Ask)
    assert (ask.text, ask.dice) == ("Roll your sword damage.", "1d8")

    run = asyncio.run(
        run_tool(LIBRARY.rule_sets["cairn2e"].tool_calls, request, RollAnswer(3), ctx=ctx)
    )
    assert isinstance(run, ToolRun)
    result = json.loads(run.output)
    assert "error" not in result
    assert result["harm"] == {"hp": 3}
