from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Callable
from dataclasses import replace
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusforge.content.definitions import ContentError, Definition
from cygnusforge.content.library import (
    DamageStep,
    MonsterTemplate,
    _hit_pool,
    _load_combat,
    _load_monster,
)
from cygnusforge.llm.base import ToolRequest
from cygnusforge.rules import cairn2e
from cygnusforge.rules.context import ToolContext
from cygnusforge.services import encounters
from cygnusforge.services.combat import (
    Damage,
    apply_damage,
    player_save,
    set_debug_stat,
    set_invincibility,
)
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import (
    NPC_KIND,
    ChatGM,
    EncounterEnded,
    MaxChanged,
    Struck,
    current,
    decode,
    events_since,
    record,
)
from cygnusforge.services.tools import Ask, RollAnswer, ToolRun, run_tool
from cygnusforge.services.world import WorldTools
from tests.conftest import content_library
from tests.test_campaigns import _create_character
from tests.test_character_creation import _cairn_campaign, _complete_form
from tests.test_world import _db, _state

LIBRARY = content_library()
CAIRN = LIBRARY.rule_sets["cairn2e"]


def save_succeeds(roll: int, score: int) -> bool:
    """Cairn's d20 save as its combat JSON declares it: a 1 passes, a 20 fails, and otherwise the
    roll must be at or under the score."""
    return roll == 1 or (roll != 20 and roll <= score)


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
        plus=CAIRN.plus,
        monsters=CAIRN.monsters,
        conditions=CAIRN.conditions,
        clock_rules=CAIRN.clock,
        rests=CAIRN.rests,
        interrupted_rest=CAIRN.interrupted_rest,
        inventory=CAIRN.inventory,
        item_sizes=CAIRN.item_sizes,
        magic=CAIRN.magic,
        hooks=CAIRN.hooks,
        marketplace=CAIRN.marketplace,
    )


def _set_condition(tools: WorldTools, name: str, deprived: bool, cause: str = "") -> dict:
    """Calls the Cairn set_condition tool against the tools' world."""
    return cairn2e.set_condition(
        name, deprived, cause, cast(ToolContext, SimpleNamespace(world=tools))
    )


def _fight(
    client_factory: Callable[[], TestClient], rolled: bool = True
) -> tuple[FastAPI, int, WorldTools]:
    """A Cairn campaign with Mara in the yard, the yard mapped, and an encounter under way. Unless
    `rolled` is False, Mara has rolled for initiative, and the order is set."""
    client, campaign_id, tools = _fight_client(client_factory, rolled)
    return client.app, campaign_id, tools


def _fight_client(
    client_factory: Callable[[], TestClient], rolled: bool = True
) -> tuple[TestClient, int, WorldTools]:
    client, campaign_id = _cairn_campaign(client_factory)
    created = _create_character(
        client, campaign_id, name=PC, fields=_complete_form(client, campaign_id)
    )
    assert created.status_code == 303
    tools = _tools(client.app, campaign_id)
    tools.give_item(PC, "Sword", damage="1d8")
    tools.create_location(YARD, "Cobbles around a dry well.")
    tools.move_actor(PC, YARD)
    tools.create_encounter_map(MAP, YARD, 10, 10, "Open cobbles.")
    tools.start_encounter(MAP)
    tools.add_participant(PC)
    if rolled:
        tools.player_initiative(1)
    return client, campaign_id, tools


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


def _finish_gm_reply(app: FastAPI, campaign_id: int) -> None:
    """The GM's final reply, which closes the turn it was answering."""
    with _db(app) as db:
        record(db, campaign_id, ChatGM(content="The turn plays out.", model="test"))
        db.commit()


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
        "id": "grave-thing",
        "name": "Grave Thing",
        "description": "A foe.",
        "stats": dict(FULL_STATS),
        "armor": 0,
        "attacks": {"claw": "1d6"},
    }
    data.update(changes)
    return Definition(type="monster", id="grave-thing", data=data)


def test_a_monster_definition_is_read_into_a_statblock():
    template = _load_monster(
        _monster(armor=2), CAIRN.character, "cairn2e", pool=_hit_pool(CAIRN.combat)
    )
    assert template == MonsterTemplate(
        id="grave-thing",
        name="Grave Thing",
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
        _load_monster(_monster(**changes), CAIRN.character, "cairn2e", pool=_hit_pool(CAIRN.combat))


CAIRN_DAMAGE = [
    {"kind": "absorb"},
    {"kind": "harm", "stat": "hp"},
    {"kind": "overflow", "stat": "str"},
]


def _combat(**changes: Any) -> Definition:
    data: dict[str, Any] = {
        "type": "combat",
        "id": "cairn",
        "python_module": "cairn2e",
        "damage": CAIRN_DAMAGE,
        "check_die": "1d20",
        "initiative_stat": "dex",
        "armor_max": 3,
        "defeat": {"player_character": "in critical damage", "npc": "dead"},
    }
    data.update(changes)
    return Definition(type="combat", id="cairn", data=data)


def test_a_combat_definition_is_read_with_its_module_and_stats():
    combat = _load_combat(_combat(), {"cairn2e": cairn2e}, CAIRN.character, {}, {}, "cairn2e")
    assert combat.damage == (
        DamageStep("absorb", None),
        DamageStep("harm", "hp"),
        DamageStep("overflow", "str"),
    )
    assert (combat.check_die, combat.check_sides, combat.initiative_stat) == ("1d20", 20, "dex")
    assert combat.defeat == {"player_character": "in critical damage", "npc": "dead"}
    assert combat.module is cairn2e


def test_a_harm_only_combat_definition_is_accepted():
    combat = _load_combat(
        _combat(damage=[{"kind": "harm", "stat": "hp"}], check_die="1d12"),
        {"cairn2e": cairn2e},
        CAIRN.character,
        {},
        {},
        "cairn2e",
    )
    assert combat.damage == (DamageStep("harm", "hp"),)
    assert combat.check_sides == 12


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"python_module": "nope"}, "unknown python_module 'nope'"),
        ({"damage": []}, "'damage' must be a non-empty list"),
        ({"damage": ["harm"]}, "each damage step must be an object"),
        ({"damage": [{"kind": "burn", "stat": "hp"}]}, "kind 'burn' is not one of"),
        ({"damage": [{"kind": "harm", "stat": "luck"}]}, "damage step names no stat"),
        ({"damage": [{"kind": "absorb", "stat": "hp"}]}, "an 'absorb' damage step takes no stat"),
        ({"damage": [{"kind": "absorb"}]}, "needs a 'harm' or 'overflow' step"),
        (
            {"damage": [{"kind": "overflow", "stat": "str"}, {"kind": "harm", "stat": "hp"}]},
            "'overflow' must be the last damage step",
        ),
        (
            {"damage": [{"kind": "harm", "stat": "hp"}, {"kind": "absorb"}]},
            "'absorb' must be the first damage step",
        ),
        (
            {"damage": [{"kind": "absorb"}, {"kind": "absorb"}, {"kind": "harm", "stat": "hp"}]},
            "may each appear at most once",
        ),
        (
            {"damage": [{"kind": "harm", "stat": "hp"}, {"kind": "harm", "stat": "hp"}]},
            "each stat may be used by one damage step only",
        ),
        ({"check_die": "d20"}, "'check_die' must be a single die"),
        ({"check_die": "1d1"}, "'check_die' must be a single die"),
        ({"initiative_stat": "luck"}, "'initiative_stat' names no stat"),
        ({"defeat": "dead"}, "'defeat' must be an object"),
        ({"defeat": {"player_character": "down"}}, "'npc' must be a non-empty string"),
    ],
)
def test_a_bad_combat_definition_is_rejected(changes, message):
    with pytest.raises(ContentError, match=message):
        _load_combat(_combat(**changes), {"cairn2e": cairn2e}, CAIRN.character, {}, {}, "cairn2e")


def test_apply_damage_takes_armor_then_harm_then_overflow():
    steps = _load_combat(_combat(), {"cairn2e": cairn2e}, CAIRN.character, {}, {}, "cairn2e").damage
    stats = {"hp": 4, "str": 10}
    result = apply_damage(steps, armor=2, current_of=stats.__getitem__, raw=9)
    assert result == Damage(absorbed=2, harm={"hp": 4, "str": 3}, save="str")


def test_apply_damage_without_overflow_leaves_no_save():
    steps = _load_combat(_combat(), {"cairn2e": cairn2e}, CAIRN.character, {}, {}, "cairn2e").damage
    stats = {"hp": 4, "str": 10}
    result = apply_damage(steps, armor=0, current_of=stats.__getitem__, raw=3)
    assert result == Damage(absorbed=0, harm={"hp": 3}, save=None)


def test_apply_damage_for_a_harm_only_rule_set_ignores_armor_and_saves():
    steps = (DamageStep("harm", "hp"),)
    stats = {"hp": 5}
    result = apply_damage(steps, armor=3, current_of=stats.__getitem__, raw=9)
    assert result == Damage(absorbed=0, harm={"hp": 5}, save=None)


def test_a_combat_module_may_leave_the_save_and_initiative_to_its_json():
    combat = _load_combat(
        _combat(python_module="bare"),
        {"bare": ModuleType("bare")},
        CAIRN.character,
        {},
        {},
        "cairn2e",
    )
    assert combat.module is not None
    assert combat.save_succeeds(1, 0) is True
    assert combat.save_succeeds(20, 20) is False
    assert combat.save_succeeds(11, 10) is False


def _declarative_combat(**changes: Any) -> Definition:
    """A combat definition with no python_module, so its save and initiative are declarative."""
    data = {key: value for key, value in _combat().data.items() if key != "python_module"}
    data.update(changes)
    return Definition(type="combat", id="cairn", data=data)


def _declarative(**changes: Any):
    return _load_combat(_declarative_combat(**changes), {}, CAIRN.character, {}, {}, "cairn2e")


@pytest.mark.parametrize(
    ("save", "roll", "score", "passed"),
    [
        ({"mode": "roll_under"}, 12, 12, True),
        ({"mode": "roll_under"}, 13, 12, False),
        ({"mode": "roll_over"}, 12, 12, True),
        ({"mode": "roll_over"}, 11, 12, False),
        ({"mode": "roll_over", "auto_success": 20, "auto_fail": 2}, 20, 1, True),
        ({"mode": "roll_over", "auto_success": 20, "auto_fail": 2}, 2, 19, False),
        ({"auto_success": None, "auto_fail": None}, 20, 19, False),
        ({"auto_success": None, "auto_fail": None}, 20, 20, True),
    ],
)
def test_a_declarative_save_decides_without_a_python_module(save, roll, score, passed):
    combat = _declarative(save=save)
    assert combat.module is None
    assert combat.save_succeeds(roll, score) is passed


def test_without_a_save_block_a_one_always_passes_and_a_twenty_always_fails():
    combat = _declarative()
    assert combat.save_succeeds(1, 0) is True
    assert combat.save_succeeds(20, 20) is False


@pytest.mark.parametrize(
    ("save", "message"),
    [
        ({"mode": "roll_sideways"}, "'mode' must be one of"),
        ({"auto_success": 21}, "save 'auto_success' must be a roll on the check die"),
        ({"auto_fail": 0}, "save 'auto_fail' must be a roll on the check die"),
    ],
)
def test_a_bad_declarative_save_is_rejected(save, message):
    with pytest.raises(ContentError, match=message):
        _declarative(save=save)


ENTRIES = [("Ann", False), ("Ogre", None), ("Bo", True), ("Cy", None), ("Dee", True)]


def test_a_declarative_initiative_puts_passers_then_npcs_then_failers():
    combat = _declarative()
    assert combat.initiative_mode == "pass_npcs_fail"
    assert combat.initiative_order(ENTRIES) == ["Bo", "Dee", "Ogre", "Cy", "Ann"]


def test_a_roll_order_initiative_keeps_the_entries_as_given():
    combat = _declarative(initiative_mode="roll_order")
    assert combat.initiative_order(ENTRIES) == ["Ann", "Ogre", "Bo", "Cy", "Dee"]


def test_the_encounter_uses_a_declarative_initiative_order_without_a_module(client_factory):
    _, _, tools = _fight(client_factory)
    declarative = replace(tools, combat=_declarative(initiative_mode="roll_order"))
    assert declarative._initiative_order()([("Ann", False), ("Bo", True)]) == ["Ann", "Bo"]


def _struck_events(app: FastAPI, campaign_id: int) -> list[Struck]:
    with _db(app) as db:
        return [decode(row) for row in events_since(db, campaign_id) if row.kind == Struck.kind]


def _sturdy(app: FastAPI, campaign_id: int) -> None:
    """Mara at 10 hit points. Her creation rolls its stats, so a test's hits must not drop her to
    0 at random, which would gain an injury and need another die."""
    before = _character(app, campaign_id, PC).stats["hp"]
    with _db(app) as db:
        record(
            db,
            campaign_id,
            MaxChanged(name=PC, stat="hp", before=before, after=10, cause="a test"),
        )
        db.commit()


def test_multi_attack_each_applies_every_attack_in_turn(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _sturdy(app, campaign_id)
    _foe(tools, "Grave Thing", claw="1d6")
    _foe(tools, "Bo", claw="1d6")
    each = replace(tools, combat=replace(CAIRN.combat, multi_attack="each"), rng=Rolls(1, 2))
    result = each.resolve_attacks(
        PC, [{"attacker": "Grave Thing", "attack": "Claw"}, {"attacker": "Bo", "attack": "Claw"}]
    )
    assert [attack["raw"] for attack in result["attacks"]] == [1, 2]
    assert result["raw"] == 2
    assert _character(app, campaign_id, PC).harm == {"hp": 3}
    assert [strike.attacker for strike in _struck_events(app, campaign_id)] == [
        "Grave Thing",
        "Bo",
    ]


def test_multi_attack_each_stops_once_the_target_owes_a_save(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _sturdy(app, campaign_id)
    _foe(tools, "Grave Thing", claw="1d6")
    _foe(tools, "Bo", claw="1d6")
    _set_stat(app, campaign_id, PC, "hp", 0)
    each = replace(tools, combat=replace(CAIRN.combat, multi_attack="each"), rng=Rolls(2, 3))
    result = each.resolve_attacks(
        PC, [{"attacker": "Grave Thing", "attack": "Claw"}, {"attacker": "Bo", "attack": "Claw"}]
    )
    assert len(result["attacks"]) == 1
    assert result["save_due"] == "str"
    assert len(_struck_events(app, campaign_id)) == 1


def test_roll_vs_defense_misses_a_blow_that_does_not_reach_the_armor(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _sturdy(app, campaign_id)
    tools.set_armor(PC, 3)
    _foe(tools, "Bo", claw="1d6")
    result = replace(
        tools, combat=replace(CAIRN.combat, hit_mode="roll_vs_defense"), rng=Rolls(5, 2)
    ).attack("Bo", "Claw", PC)
    assert result["hit"] is False
    assert (result["raw"], result["to_hit"], result["harm"]) == (5, 2, {})
    assert not any(_character(app, campaign_id, PC).harm.values())
    strike = _struck_events(app, campaign_id)[-1]
    assert strike.hit is False and strike.to_hit == 2
    assert strike.render().endswith("swings the Claw at Mara and misses (rolled 2).")


def test_roll_vs_defense_lands_a_blow_that_reaches_the_armor(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _sturdy(app, campaign_id)
    tools.set_armor(PC, 3)
    _foe(tools, "Bo", claw="1d6")
    result = replace(
        tools, combat=replace(CAIRN.combat, hit_mode="roll_vs_defense"), rng=Rolls(5, 3)
    ).attack("Bo", "Claw", PC)
    assert "hit" not in result
    assert (result["raw"], result["armor"], result["harm"]) == (5, 3, {"hp": 2})
    assert _character(app, campaign_id, PC).harm == {"hp": 2}
    strike = _struck_events(app, campaign_id)[-1]
    assert strike.hit is True and strike.to_hit == 3


def test_the_auto_hit_mode_never_rolls_to_hit(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _sturdy(app, campaign_id)
    tools.set_armor(PC, 3)
    _foe(tools, "Bo", claw="1d6")
    result = replace(tools, rng=Rolls(5)).attack("Bo", "Claw", PC)
    assert result["raw"] == 5
    assert _struck_events(app, campaign_id)[-1].to_hit is None


def test_a_large_group_line_names_the_incoming_modifier_its_rule_set_gives(client_factory):
    _, _, tools = _fight(client_factory)
    tools.create_monster(
        "Swarm", YARD, description="A group.", template="killer-bees", stats={"hp": 3, "str": 5}
    )
    tools.add_participant("Swarm")

    def large_group_line(combat) -> str:
        lines = replace(tools, combat=combat).get_statblock("Swarm")["statblock"]
        return next(line for line in lines if line.startswith("Large group:"))

    groups = CAIRN.combat.groups
    assert large_group_line(CAIRN.combat) == (
        "Large group: a group that attacks with Blast and is hit only by impaired attacks"
    )
    enhanced = replace(CAIRN.combat, groups=replace(groups, incoming_modifier="enhanced"))
    assert large_group_line(enhanced) == (
        "Large group: a group that attacks with Blast and is hit only by enhanced attacks"
    )
    assert large_group_line(replace(CAIRN.combat, groups=None)) == (
        "Large group: a group that attacks with Blast"
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
    assert rover.creature == "Blink Dog"


def test_the_roster_shows_the_statblock_beside_a_monster(client_factory):
    client, campaign_id, tools = _fight_client(client_factory)
    tools.create_monster("Rover", YARD, template="blink-dog")
    tools.add_participant("Rover")
    page = client.get(f"/campaigns/{campaign_id}").text
    roster = page[page.index('class="encounter-roster"') : page.index('aria-label="Panels"')]
    assert "<strong>Rover</strong>" in roster
    assert '<span class="muted">Blink Dog</span>' in roster


def test_the_encounter_view_names_the_statblock_a_monster_was_built_from(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.create_monster("Rover", YARD, template="blink-dog")
    tools.add_participant("Rover")
    tools.create_monster("Brute", YARD, description="A brute.", stats=FULL_STATS)
    tools.add_participant("Brute")
    view = encounters.encounter_view(_state(app, campaign_id), None, CAIRN.combat)
    creatures = {p["name"]: p["creature"] for p in view["participants"]}
    assert creatures == {PC: "", "Rover": "Blink Dog", "Brute": ""}


def test_a_monster_named_after_its_statblock_does_not_repeat_it(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.create_monster("blink dog", YARD, template="blink-dog")
    tools.add_participant("blink dog")
    view = encounters.encounter_view(_state(app, campaign_id), None, CAIRN.combat)
    creatures = {p["name"]: p["creature"] for p in view["participants"]}
    assert creatures["blink dog"] == ""


def test_a_name_that_matches_a_statblock_uses_it_without_a_template(client_factory):
    _app, _campaign_id, tools = _fight(client_factory)
    result = tools.create_monster("Blink Dog", YARD)
    assert result["template"] == "blink-dog"
    assert result["stats"] == {"hp": 5, "str": 11, "dex": 14, "wil": 5}
    assert result["attacks"] == {"bite": "1d6"}


def test_a_statblock_name_with_stats_is_refused_unless_forced(client_factory):
    _app, _campaign_id, tools = _fight(client_factory)
    with pytest.raises(InvalidInput, match="published statblock"):
        tools.create_monster("Blink Dog", YARD, stats=FULL_STATS)
    result = tools.create_monster(
        "Blink Dog", YARD, description="A custom beast.", stats=FULL_STATS, force=True
    )
    assert result["stats"] == FULL_STATS
    assert result["template"] is None


def test_an_npc_named_after_a_statblock_takes_its_stats(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    result = tools.create_npc("Blink Dog", YARD, "A beast.")
    assert result["statblock"] == "blink-dog"
    npc = _state(app, campaign_id).characters["blink dog"]
    assert npc.stats == {"hp": 5, "str": 11, "dex": 14, "wil": 5}


def test_a_world_npc_linked_to_a_statblock_is_seeded_with_its_stats(client_factory):
    app, campaign_id, _ = _fight(client_factory)
    fasha = _character(app, campaign_id, "Fasha")
    assert fasha.stats == {"hp": 6, "str": 14, "dex": 13, "wil": 5}
    assert fasha.creature == "Fasha"
    assert fasha.attacks == {"fists": "1d8+1d8", "hook knife": "1d6"}


def test_an_npc_statblock_name_with_stats_is_refused_unless_forced(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    with pytest.raises(InvalidInput, match="published statblock"):
        tools.create_npc("Blink Dog", YARD, "A beast.", character={"stats": FULL_STATS})
    tools.create_npc(
        "Blink Dog", YARD, "A custom beast.", character={"stats": FULL_STATS}, force=True
    )
    npc = _state(app, campaign_id).characters["blink dog"]
    assert npc.stats == FULL_STATS


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
    _foe(tools, "Grave Thing")
    # Stats are random per run; a 1-damage hit on a PC at 1 HP would injury and need another die.
    with _db(app) as db:
        before = _character(app, campaign_id, PC).stats["hp"]
        record(
            db, campaign_id, MaxChanged(name=PC, stat="hp", before=before, after=10, cause="a test")
        )
        db.commit()
    with pytest.raises(InvalidInput, match="initiative is not set yet; Mara must roll"):
        _hit(tools, "Grave Thing", PC, 2)
    with pytest.raises(InvalidInput, match="initiative is not set yet"):
        tools.player_attack("Grave Thing", "Sword")
    with _db(app) as db, pytest.raises(InvalidInput, match="initiative is not set yet"):
        encounters.begin_turn(db, campaign_id, "Grave Thing")

    result = tools.player_initiative(1)
    assert result["passed"] is True
    assert result["still_waiting"] == []
    assert _hit(tools, "Grave Thing", PC, 1)["harm"] == {"hp": 1}


@pytest.mark.parametrize(("roll", "first"), [(1, PC), (20, "Grave Thing")])
def test_passing_player_characters_act_first_in_round_one(client_factory, roll, first):
    app, campaign_id, tools = _fight(client_factory, rolled=False)
    _foe(tools, "Grave Thing")
    tools.player_initiative(roll)
    assert _state(app, campaign_id).encounter.order[0] == first


def test_a_roll_for_initiative_is_refused_when_none_is_owed(client_factory):
    _, _, tools = _fight(client_factory)
    with pytest.raises(InvalidInput, match="no one is rolling for initiative"):
        tools.player_initiative(5)


def test_hit_points_take_harm_before_strength(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing")
    _foe(tools, "Bo")
    result = _hit(tools, "Grave Thing", "Bo", 4)
    assert result["armor"] == 0
    assert result["harm"] == {"hp": 4}
    assert result["save_due"] is None
    assert result["defeated"] is False
    assert _character(app, campaign_id, "Bo").harm == {"hp": 4}


def test_armor_absorbs_before_hit_points(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing")
    _foe(tools, "Bo")
    tools.set_armor("Bo", 3)
    result = _hit(tools, "Grave Thing", "Bo", 5)
    assert result["armor"] == 3
    assert result["harm"] == {"hp": 2}
    assert _character(app, campaign_id, "Bo").armor == 3


def test_armor_that_absorbs_the_whole_hit_causes_no_harm(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing")
    _foe(tools, "Bo")
    tools.set_armor("Bo", 3)
    result = _hit(tools, "Grave Thing", "Bo", 3)
    assert result["harm"] == {}
    assert _character(app, campaign_id, "Bo").harm == {}


def _set_invincible(app: FastAPI, campaign_id: int, name: str, active: bool) -> dict[str, Any]:
    with _db(app) as db:
        return set_invincibility(db, campaign_id, name, active)


def test_an_invincible_character_takes_no_harm_from_a_strike(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing", claw="4d6")
    _set_invincible(app, campaign_id, PC, True)
    result = _hit(tools, "Grave Thing", PC, 6, 6, 6, 6)
    assert result["armor"] == 24
    assert result["harm"] == {}
    assert result["save_due"] is None
    assert result["defeated"] is False
    pc = _character(app, campaign_id, PC)
    assert pc.harm == {}
    assert pc.save_due is None
    assert pc.defeated is False


def test_an_invincible_character_takes_no_outside_harm(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _set_invincible(app, campaign_id, PC, True)
    result = tools.harm_character(PC, "str", 5, "a fall")
    assert result["taken"] == 0
    assert _character(app, campaign_id, PC).harm == {}


def test_turning_invincibility_off_restores_harm(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing")
    _set_invincible(app, campaign_id, PC, True)
    _hit(tools, "Grave Thing", PC, 6)
    assert _character(app, campaign_id, PC).harm == {}
    _set_invincible(app, campaign_id, PC, False)
    _set_stat(app, campaign_id, PC, "hp", 0)
    before = dict(_character(app, campaign_id, PC).harm)
    result = _hit(tools, "Grave Thing", PC, 6)
    assert result["harm"]
    after = _character(app, campaign_id, PC).harm
    for stat, lost in result["harm"].items():
        assert after[stat] == before.get(stat, 0) + lost


def _set_stat(
    app: FastAPI, campaign_id: int, name: str, stat: str, remaining: int
) -> dict[str, Any]:
    with _db(app) as db:
        return set_debug_stat(db, campaign_id, name, stat, remaining)


def test_a_debug_score_sets_how_much_of_a_stat_is_left(client_factory):
    app, campaign_id, _tools = _fight(client_factory)
    maximum = _character(app, campaign_id, PC).stats["hp"]
    _set_stat(app, campaign_id, PC, "hp", 0)
    pc = _character(app, campaign_id, PC)
    assert pc.harm == {"hp": maximum}
    assert current(pc, "hp") == 0
    _set_stat(app, campaign_id, PC, "hp", maximum)
    assert current(_character(app, campaign_id, PC), "hp") == maximum


def test_a_debug_score_is_refused_for_an_npc_or_out_of_range(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing")
    with pytest.raises(InvalidInput):
        _set_stat(app, campaign_id, "Grave Thing", "hp", 2)
    with pytest.raises(InvalidInput):
        _set_stat(app, campaign_id, PC, "hp", 999)
    with pytest.raises(InvalidInput):
        _set_stat(app, campaign_id, PC, "luck", 1)


def test_turning_invincibility_on_clears_a_pending_save(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing", claw="4d6")
    _set_stat(app, campaign_id, PC, "hp", 0)
    _hit(tools, "Grave Thing", PC, 6, 6, 6, 6)
    assert _character(app, campaign_id, PC).save_due is not None
    _set_invincible(app, campaign_id, PC, True)
    assert _character(app, campaign_id, PC).save_due is None
    with _db(app) as db, pytest.raises(InvalidInput, match="no player character owes a save"):
        player_save(db, campaign_id, 1, CAIRN.combat)


def test_invincibility_is_only_for_known_player_characters(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing")
    with pytest.raises(InvalidInput, match="not a player character"):
        _set_invincible(app, campaign_id, "Grave Thing", True)
    with pytest.raises(InvalidInput, match="no character named"):
        _set_invincible(app, campaign_id, "Nobody", True)


def test_overflow_comes_off_strength_and_a_passed_save_keeps_the_monster_standing(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing", claw="2d6")
    _foe(tools, "Bo")
    result = _hit(tools, "Grave Thing", "Bo", 6, 5, 1)
    assert result["harm"] == {"hp": 6, "str": 5}
    assert result["save"] == {"stat": "str", "roll": 1, "score": 5, "passed": True}
    assert result["defeated"] is False
    bo = _character(app, campaign_id, "Bo")
    assert bo.harm == {"hp": 6, "str": 5}
    assert bo.save_due is None


def test_a_failed_save_defeats_a_monster(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing", claw="2d6")
    _foe(tools, "Bo")
    result = _hit(tools, "Grave Thing", "Bo", 6, 5, 20, 1, 1)
    assert result["save"]["passed"] is False
    assert result["defeated"] is True
    assert _character(app, campaign_id, "Bo").defeated is True


def test_a_defeated_monster_cannot_be_hit_or_attack(client_factory):
    _, _, tools = _fight(client_factory)
    _foe(tools, "Grave Thing", claw="2d6")
    _foe(tools, "Bo")
    _hit(tools, "Grave Thing", "Bo", 6, 5, 20, 1, 1)
    with pytest.raises(InvalidInput, match="Bo is dead"):
        _hit(tools, "Grave Thing", "Bo", 1)
    with pytest.raises(InvalidInput, match="Bo is dead"):
        _hit(tools, "Bo", "Grave Thing", 1)


def test_an_unknown_attack_is_refused_and_the_options_are_listed(client_factory):
    _, _, tools = _fight(client_factory)
    _foe(tools, "Grave Thing")
    _foe(tools, "Bo")
    with pytest.raises(InvalidInput, match="has no attack named 'Sting'; attacks: Claw"):
        tools.attack("Grave Thing", "Sting", "Bo")


def test_a_player_character_cannot_use_the_monster_attack(client_factory):
    _, _, tools = _fight(client_factory)
    _foe(tools, "Bo")
    with pytest.raises(InvalidInput, match="use player_attack instead"):
        tools.attack(PC, "Claw", "Bo")


def test_a_player_hit_is_the_damage_the_player_rolled(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Bo")
    result = replace(tools, rng=Rolls(4)).player_attack("Bo", "Sword")
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
    ("weapon", "message"),
    [
        ("  ", "weapon must name the attack"),
        ("Axe", "carries no weapon named 'Axe'"),
        ("Rope", "Rope is not a weapon"),
    ],
)
def test_a_player_hit_is_refused_for_a_bad_weapon(client_factory, weapon, message):
    _, _, tools = _fight(client_factory)
    _foe(tools, "Bo")
    tools.give_item(PC, "Rope")
    with pytest.raises(InvalidInput, match=message):
        tools.player_attack("Bo", weapon)


def test_overflow_on_a_player_character_waits_for_the_player_to_save(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing", claw="3d6")
    _set_stat(app, campaign_id, PC, "hp", 0)
    result = _hit(tools, "Grave Thing", PC, 6, 6, 6)
    assert result["save_due"] == "str"
    assert "save" not in result
    assert result["defeated"] is False
    assert _character(app, campaign_id, PC).save_due == "str"

    with pytest.raises(InvalidInput, match="must make their str save first"):
        _hit(tools, "Grave Thing", PC, 1, 1, 1)
    with pytest.raises(InvalidInput, match="must make their str save first"):
        tools.player_attack(PC, "Sword")


def test_a_player_character_who_fails_the_save_is_defeated(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing", claw="3d6")
    _set_stat(app, campaign_id, PC, "hp", 0)
    _hit(tools, "Grave Thing", PC, 6, 6, 6)
    result = tools.player_save(20)
    assert result["passed"] is False
    assert result["stat"] == "str"
    assert _character(app, campaign_id, PC).defeated is True


def test_a_defeated_foe_is_skipped_when_its_turn_comes_round(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing", claw="2d6")
    _foe(tools, "Bo")
    _foe(tools, "Cy")
    _hit(tools, "Grave Thing", "Bo", 6, 5, 20, 1)
    assert _character(app, campaign_id, "Bo").defeated is True
    _finish_gm_reply(app, campaign_id)

    with _db(app) as db:
        encounters.end_turn(db, campaign_id, PC, by_player=True)
        encounters.begin_turn(db, campaign_id, "Grave Thing")
        encounters.end_turn(db, campaign_id, "Grave Thing", by_player=False)
    state = _state(app, campaign_id).encounter
    assert state.order[state.up] == "Cy"
    assert state.acting is None


def test_a_player_character_who_passes_the_save_stands(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing", claw="3d6")
    _set_stat(app, campaign_id, PC, "hp", 0)
    _hit(tools, "Grave Thing", PC, 6, 6, 6)
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
        plain.player_attack("Grave Thing", "Sword")


async def _no_speak(*_: Any) -> None:
    return None


def test_player_attack_asks_for_damage_then_resolves_through_the_tool(client_factory):
    _, _, tools = _fight(client_factory)
    _foe(tools, "Grave Thing", hp=6)
    ctx = ToolContext(
        world=replace(tools, rng=Rolls(1)),
        speak=_no_speak,
        ooc=lambda narrate: None,
        rules={},
        lookup_tools={},
    )
    request = ToolRequest(
        id="call_1",
        name="player_attack",
        arguments=json.dumps(
            {
                "target": "Grave Thing",
                "weapon": "Sword",
                "prompt": "Roll your sword damage.",
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
    assert result["raw"] == 3
    assert result["harm"] == {"hp": 3}


def test_a_defeated_foe_does_not_end_the_encounter_while_another_foe_stands(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing", claw="2d6")
    _foe(tools, "Bo")
    _foe(tools, "Cy")
    _hit(tools, "Grave Thing", "Bo", 6, 5, 20, 1)
    assert _character(app, campaign_id, "Bo").defeated is True
    assert _state(app, campaign_id).encounter is not None


def test_the_encounter_ends_when_the_last_foe_falls(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Bo")
    replace(tools, rng=Rolls(8, 20)).player_attack("Bo", "Sword")
    assert _character(app, campaign_id, "Bo").defeated is True
    assert _state(app, campaign_id).encounter is None


def test_the_encounter_ends_when_the_player_character_is_knocked_out(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Bo", claw="4d6")
    _set_stat(app, campaign_id, PC, "hp", 0)
    _hit(tools, "Bo", PC, 6, 6, 6, 6)
    assert _character(app, campaign_id, PC).save_due is not None
    tools.player_save(20)
    assert _character(app, campaign_id, PC).defeated is True
    assert _state(app, campaign_id).encounter is None


def test_the_encounter_end_says_which_side_can_still_fight():
    assert EncounterEnded(reason="Only the foes side can still fight.").render() == (
        "Only the foes side can still fight. The encounter is over."
    )
    assert EncounterEnded().render() == "The encounter is over."


def _knock_out_pc(app: FastAPI, campaign_id: int, tools: WorldTools) -> None:
    _foe(tools, "Bo", claw="4d6")
    _set_stat(app, campaign_id, PC, "hp", 0)
    _hit(tools, "Bo", PC, 6, 6, 6, 6)
    tools.player_save(20)


def test_the_gm_brings_a_knocked_out_character_back_with_an_explanation(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _knock_out_pc(app, campaign_id, tools)
    assert _character(app, campaign_id, PC).defeated is True
    tools.stabilize(PC, "A healer's bandages stop the bleeding.")
    result = tools.recover_character(
        PC, "A passing cart carried her to the chapel.", "Her left hand is lamed."
    )
    assert result == {"character": PC, "recovered": True}
    assert _character(app, campaign_id, PC).defeated is False


def test_the_statblock_shows_a_knocked_out_character_in_critical_damage(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    assert "Status: in critical damage" not in "\n".join(tools.get_statblock(PC)["statblock"])
    _knock_out_pc(app, campaign_id, tools)
    assert "Status: in critical damage" in "\n".join(tools.get_statblock(PC)["statblock"])
    tools.stabilize(PC, "A healer's bandages stop the bleeding.")
    tools.recover_character(PC, "A passing cart carried her to the chapel.", "Her hand is lamed.")
    assert "Status: in critical damage" not in "\n".join(tools.get_statblock(PC)["statblock"])


def test_recovery_says_how_the_character_survived_and_what_it_cost():
    from cygnusforge.services.events import CharacterRecovered

    text = CharacterRecovered(
        name=PC, explanation="A passing cart carried her out.", consequence="Her hand is lamed."
    ).render()
    assert text == "Mara comes to. A passing cart carried her out. Her hand is lamed."


def test_recovery_refuses_an_npc_and_a_character_who_is_not_down(client_factory):
    _app, _campaign_id, tools = _fight(client_factory)
    _foe(tools, "Bo")
    with pytest.raises(InvalidInput, match="not a player character"):
        tools.recover_character("Bo", "Lucky.", "Nothing.")
    with pytest.raises(InvalidInput, match="not in critical damage"):
        tools.recover_character(PC, "Lucky.", "Nothing.")
    with pytest.raises(InvalidInput, match="no character named"):
        tools.recover_character("Nobody", "Lucky.", "Nothing.")


def test_a_defeated_foe_stays_on_the_map_as_a_body(client_factory):
    client, campaign_id, tools = _fight_client(client_factory)
    _foe(tools, "Grave Thing", claw="2d6")
    _foe(tools, "Bo")
    _foe(tools, "Cy")
    tools.place_participant("Bo", 2, 2)
    _hit(tools, "Grave Thing", "Bo", 6, 5, 20, 1)
    page = client.get(f"/campaigns/{campaign_id}").text
    roster = page[page.index('class="encounter-roster"') : page.index('aria-label="Panels"')]
    assert '<span class="roster-status">dead</span>' in roster
    assert "encounter-cell occupied defeated" in page


def test_a_strike_names_the_attacker_and_weapon_in_one_sentence():
    strike = Struck(
        name="Ysolde Vane",
        attacker="Cavity Grave Thing",
        weapon="claws",
        raw=6,
        armor=0,
        harm={"hp": 6},
    )
    assert strike.render() == (
        "Cavity Grave Thing strikes Ysolde Vane with the claws for 6; Ysolde Vane takes 6 harm."
    )
