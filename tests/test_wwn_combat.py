"""Tests for the Worlds Without Number combat runtime: defense, Shock, hit and damage bonuses,
and the defeat rules. The campaign and character come from the real creation form, as a player
would make them."""

from __future__ import annotations

import asyncio
import json
import random
import re
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.content.character import Feature, Reaction
from cygnusloom.content.library import CombatRules, ContentLibrary, InjuryEntry, InjuryTable
from cygnusloom.rules import wwn
from cygnusloom.services import death, encounters
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import (
    CharacterDying,
    CharacterSaved,
    ChatGM,
    EncounterTurnBegun,
    EncounterTurnEnded,
    FeatureLearned,
    Harmed,
    Struck,
    current,
    decode,
    events_since,
    load_state,
    name_key,
    record,
)
from cygnusloom.services.tools import Ask, RollAnswer, ToolRequest, run_tool
from cygnusloom.services.views import scene_view
from cygnusloom.services.world import WorldTools
from tests.conftest import content_library, csrf_token
from tests.factories import WWN_STATS, pinned_stats
from tests.test_campaigns import _create_character, _new_campaign, _signed_in
from tests.test_character_creation import _choices, _named_picks, _picks, _rolled_stats
from tests.test_combat import Rolls, _character
from tests.test_world import _db
from tests.turns import take_turn

LIBRARY: ContentLibrary = content_library()
WWN = LIBRARY.rule_sets["worldswithoutnumber"]

PC = "Wren"
FOE = "Hound"
OTHER_FOE = "Second Hound"
YARD = "Old Yard"
MAP = "Yard Grid"

LONG_SWORD = "Sword, Long"
WAR_AXE = "Axe, War"
BOW = "Bow, Large"
UNARMED = "unarmed"

# The stats every WWN statblock must give, since the rule set's stat list includes its derived
# saves, strain, and effort. The values a test cares about are the ones it overrides.
BASE_STATS: dict[str, int] = {
    "str": 10,
    "dex": 10,
    "con": 10,
    "int": 10,
    "wis": 10,
    "cha": 10,
    "hp": 6,
    "attack_bonus": 0,
    "castings": 0,
    "morale": 7,
    "strain": 10,
    "effort_high": 1,
    "effort_necromancer": 1,
    "effort_elemental": 1,
    "effort_healer": 1,
    "effort_vowed": 1,
    "save_physical": 15,
    "save_evasion": 15,
    "save_mental": 15,
    "save_luck": 15,
}


def _wwn_tools(
    app: FastAPI,
    campaign_id: int,
    rng: random.Random | None = None,
    combat: CombatRules | None = None,
) -> WorldTools:
    return WorldTools(
        engine=app.state.engine,
        campaign_id=campaign_id,
        tables=LIBRARY.core.tables,
        rng=rng or random.Random(0),
        character=WWN.character,
        rule_data=WWN.rule_data,
        grid=WWN.grid,
        combat=combat or WWN.combat,
        monsters=WWN.monsters,
        checks=WWN.checks,
        conditions=WWN.conditions,
        counters=WWN.counters,
        moves=WWN.moves,
        clock_rules=WWN.clock,
        rests=WWN.rests,
        interrupted_rest=WWN.interrupted_rest,
        inventory=WWN.inventory,
        item_sizes=WWN.item_sizes,
        plus=WWN.plus,
        npc_hp=WWN.npc_hp,
        npc_hp_stat=WWN.npc_hp_stat,
    )


def _wwn_campaign(
    client_factory: Callable[[], TestClient], choices: dict[str, str] | None = None
) -> tuple[TestClient, int, WorldTools]:
    """An unwritten-world campaign with a newly created player character and the WWN world tools."""
    client = _signed_in(client_factory)
    response = _new_campaign(
        client, world_template="unwritten-world", rule_set="worldswithoutnumber"
    )
    assert response.status_code == 303
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    with pinned_stats(WWN_STATS):
        page = client.get(f"/campaigns/{campaign_id}").text
    fields = {f"stat-{stat}": str(value) for stat, value in _rolled_stats(page).items()}
    fields.update(_choices(page))
    fields.update(_picks(page))
    fields.update(
        {
            "pick-feature-class_focus_any-0": "focus_unique_gift",
            "pick-feature-class_focus_warrior-0": "focus_valiant_defender",
        }
    )
    fields.update(choices or {})
    fields = _named_picks(fields)
    created = _create_character(client, campaign_id, name=PC, fields=fields)
    error = re.search(r'<p class="form-error"[^>]*>(.*?)</p>', created.text, re.DOTALL)
    assert created.status_code == 303, error.group(1) if error else "no error"
    # Close the opening reply so an encounter turn can start from a clean state.
    with Session(client.app.state.engine) as db:
        record(db, campaign_id, ChatGM(content="The opening plays out.", model="test"))
        db.commit()
    return client, campaign_id, _wwn_tools(client.app, campaign_id)


def _fight(
    client_factory: Callable[[], TestClient], choices: dict[str, str] | None = None
) -> tuple[FastAPI, int, WorldTools]:
    """A WWN campaign with a mapped yard, an encounter, and the player character joined. WWN
    rolls initiative for each side as soon as both sides have a member, so no player rolls."""
    client, campaign_id, tools = _wwn_campaign(client_factory, choices)
    tools.create_location(YARD, "Cobbles around a dry well.")
    tools.move_actor(PC, YARD)
    tools.create_encounter_map(MAP, YARD, 10, 10, "Open cobbles.")
    tools.start_encounter(MAP)
    tools.add_participant(PC)
    return client.app, campaign_id, tools


def _stats(**overrides: int) -> dict[str, int]:
    return {**BASE_STATS, **overrides}


def _foe(
    tools: WorldTools,
    name: str = FOE,
    *,
    hp: int = 6,
    armor: int = 0,
    attack_bonus: int = 0,
    shock: int = 0,
    shock_ac: int = 0,
) -> None:
    attack: dict[str, Any] = {"name": "Claw", "dice": "1d6"}
    if shock:
        attack.update(shock=shock, shock_ac=shock_ac)
    tools.create_monster(
        name,
        YARD,
        description="A test foe.",
        stats=_stats(hp=hp, attack_bonus=attack_bonus),
        armor=armor,
        attacks=[attack],
        force=True,
    )
    tools.add_participant(name)


def _make_room(tools: WorldTools) -> None:
    """Drop the starting kit's gear so the Readied pool has room for whatever a test grants.
    Weapons are carried first so one is always ready, and armor and shield take the slots that
    are left, so dropping all four always frees the pool a test needs."""
    for item in ("Short Sword", "Dagger", "Pieced Armor", "Large Shield"):
        tools.drop_item(PC, item)


def _ready_shield(tools: WorldTools) -> None:
    """Drop the kit's armor so its Large Shield can be readied even when the Readied pool was
    full of armor."""
    tools.drop_item(PC, "Pieced Armor")
    tools.ready_item(PC, "Large Shield", ready=True)


def _feature(name: str) -> Feature:
    return next(feature for feature in WWN.character.features.values() if feature.name == name)


def _pc(app: FastAPI, campaign_id: int):
    return _character(app, campaign_id, PC)


def _events(app: FastAPI, campaign_id: int, kind: str) -> list[Any]:
    with _db(app) as db:
        return [decode(row) for row in events_since(db, campaign_id) if row.kind == kind]


def _struck(app: FastAPI, campaign_id: int) -> list[Struck]:
    return _events(app, campaign_id, Struck.kind)


def _modifier(character, stat: str) -> int:
    return WWN.character.modifier(character.stats, stat)


def _best_attribute(character, feature: Feature) -> int:
    scores = [_modifier(character, stat) for stat in feature.attributes if stat in character.stats]
    return max(scores) if scores else 0


def _attack_skill(weapon: str, feature: Feature | None) -> str:
    if feature is not None and feature.skill:
        return feature.skill
    if name_key(weapon) in WWN.combat.unarmed_names:
        return WWN.combat.unarmed_skill
    return ""


def _to_hit_bonus(character, weapon: str, feature: Feature) -> int:
    """The bonus a WWN player attack adds to its d20, computed from the character's actual sheet."""
    bonus = current(character, "attack_bonus") + _best_attribute(character, feature)
    skill = _attack_skill(weapon, feature)
    if skill:
        level = WWN.character.skill_level(character.skills, skill)
        bonus += level if level >= 0 else -WWN.combat.unskilled_attack_penalty
    return bonus


def _hit_bonus(character, attack: str) -> int:
    """The bonus an NPC's attack adds to its d20: its statblock's attack_bonus."""
    return current(character, attack)


def _roll_for_total(total: int, bonus: int) -> int:
    roll = total - bonus
    assert 1 <= roll <= 20, f"cannot roll {roll} on a d20 for a total of {total} with {bonus:+d}"
    return roll


def _raise(tools: WorldTools, skill: str, level: int = 1) -> int:
    """Raise a skill to at least `level` and return its level afterwards."""
    with Session(tools.engine, expire_on_commit=False) as db:
        level_before = WWN.character.skill_level(
            load_state(db, tools.campaign_id).characters[name_key(PC)].skills, skill
        )
    if level_before < level:
        tools.raise_skill(PC, skill, level, "training with the GM")
    with Session(tools.engine, expire_on_commit=False) as db:
        return WWN.character.skill_level(
            load_state(db, tools.campaign_id).characters[name_key(PC)].skills, skill
        )


def test_an_attack_below_ac_misses_and_one_at_ac_hits(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=20, armor=13)
    _make_room(tools)
    tools.give_item(PC, BOW, damage="1d8")
    take_turn(tools, PC)
    pc = _pc(app, campaign_id)
    feature = _feature(BOW)
    bonus = _to_hit_bonus(pc, BOW, feature)

    missed = replace(tools, rng=Rolls(4, _roll_for_total(12, bonus))).player_attack(FOE, BOW)
    assert missed["hit"] is False
    assert missed["harm"] == {}
    assert missed["armor"] == 0
    assert _character(app, campaign_id, FOE).harm == {}

    hit = replace(tools, rng=Rolls(4, _roll_for_total(13, bonus))).player_attack(FOE, BOW)
    assert hit["armor"] == 0
    assert hit["harm"] == {"hp": 4 + _modifier(pc, "dex") + 1}
    assert _character(app, campaign_id, FOE).harm == hit["harm"]

    miss_event, hit_event = _struck(app, campaign_id)
    assert (miss_event.hit, miss_event.to_hit) == (False, 12)
    assert (hit_event.hit, hit_event.to_hit) == (True, 13)


def test_a_miss_still_does_shock_to_a_low_ac_target(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=20, armor=13)
    _make_room(tools)
    tools.give_item(PC, WAR_AXE, damage="1d10")
    take_turn(tools, PC)
    pc = _pc(app, campaign_id)
    feature = _feature(WAR_AXE)
    shock = feature.shock + _modifier(pc, "str") + 1
    assert shock >= 1
    bonus = _to_hit_bonus(pc, WAR_AXE, feature)

    result = replace(tools, rng=Rolls(5, _roll_for_total(12, bonus))).player_attack(FOE, WAR_AXE)
    assert result["hit"] is False
    assert result["harm"] == {"hp": shock}
    assert _character(app, campaign_id, FOE).harm == {"hp": shock}

    [event] = _struck(app, campaign_id)
    assert (event.hit, event.to_hit, event.shock) == (False, 12, shock)
    assert event.render().endswith(f"The shock still does {shock} harm to HP.")


def test_shock_does_not_reach_a_shield_grade_ac(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=20, armor=16)
    _make_room(tools)
    tools.give_item(PC, WAR_AXE, damage="1d10")
    take_turn(tools, PC)
    pc = _pc(app, campaign_id)
    feature = _feature(WAR_AXE)
    bonus = _to_hit_bonus(pc, WAR_AXE, feature)

    # 14 is a miss against AC 16 but would have hit AC 13, where the Shock reaches.
    result = replace(tools, rng=Rolls(5, _roll_for_total(14, bonus))).player_attack(FOE, WAR_AXE)
    assert result["hit"] is False
    assert result["harm"] == {}
    assert _character(app, campaign_id, FOE).harm == {}

    [event] = _struck(app, campaign_id)
    assert (event.hit, event.to_hit, event.shock) == (False, 14, 0)


def test_a_hit_does_at_least_its_shock(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=20, armor=13)
    _make_room(tools)
    tools.give_item(PC, WAR_AXE, damage="1d10")
    take_turn(tools, PC)
    pc = _pc(app, campaign_id)
    feature = _feature(WAR_AXE)
    shock = feature.shock + _modifier(pc, "str") + 1
    bonus = _to_hit_bonus(pc, WAR_AXE, feature)

    # A damage roll of 1 plus the Strength modifier is below the Shock, so Shock is the damage.
    result = replace(tools, rng=Rolls(1, _roll_for_total(13, bonus))).player_attack(FOE, WAR_AXE)
    assert result["raw"] == shock
    assert result["harm"] == {"hp": shock}


def test_a_ranged_weapon_does_no_shock_on_a_miss(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=20, armor=13)
    _make_room(tools)
    tools.give_item(PC, BOW, damage="1d8")
    take_turn(tools, PC)
    pc = _pc(app, campaign_id)
    feature = _feature(BOW)
    assert feature.shock == 0
    bonus = _to_hit_bonus(pc, BOW, feature)

    result = replace(tools, rng=Rolls(5, _roll_for_total(12, bonus))).player_attack(FOE, BOW)
    assert result["hit"] is False
    assert result["harm"] == {}

    [event] = _struck(app, campaign_id)
    assert (event.hit, event.to_hit, event.shock) == (False, 12, 0)


def test_hit_roll_adds_attack_bonus_skill_and_best_attribute(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=20, armor=10)
    _make_room(tools)
    tools.give_item(PC, LONG_SWORD, damage="1d8")
    level = _raise(tools, "stab")
    take_turn(tools, PC)
    pc = _pc(app, campaign_id)
    _feature(LONG_SWORD)
    attribute = max(WWN.character.modifier(pc.stats, stat) for stat in ("str", "dex"))
    expected = current(pc, "attack_bonus") + level + attribute

    replace(tools, rng=Rolls(3, 10)).player_attack(FOE, LONG_SWORD)
    [event] = _struck(app, campaign_id)
    assert event.to_hit == 10 + expected


@pytest.mark.parametrize(
    ("weapon", "skill", "attribute"),
    [(LONG_SWORD, "stab", 2), (BOW, "shoot", 1), (UNARMED, "punch", 2)],
)
def test_an_unskilled_attack_takes_the_penalty(client_factory, weapon, skill, attribute):
    app, campaign_id, tools = _fight(
        client_factory,
        choices={
            "template-background": "artisan",
            "template-class": "warrior",
            "template-tradition": "no_tradition",
            "pick-skill-background_learning-0": "connect",
            "pick-skill-background_learning-1": "craft",
            "pick-skill-class_free_skill-0": "notice",
            "pick-feature-class_focus_any-0": "focus_die_hard",
            "pick-feature-class_focus_warrior-0": "focus_alert",
        },
    )
    _foe(tools, hp=20, armor=10)
    tools.set_stat_max(PC, "str", 18, "test the attack attribute")
    tools.set_stat_max(PC, "dex", 14, "test the attack attribute")
    pc = _pc(app, campaign_id)
    assert skill not in pc.skills
    if weapon != UNARMED:
        feature = _feature(weapon)
        _make_room(tools)
        tools.give_item(PC, weapon, damage=feature.damage)
    expected = current(pc, "attack_bonus") + attribute - 2
    take_turn(tools, PC)

    replace(tools, rng=Rolls(1, 10)).player_attack(FOE, weapon)
    [event] = _struck(app, campaign_id)
    assert event.to_hit == 10 + expected


def test_damage_adds_the_weapon_attribute_and_damage_bonus_stat(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools = replace(
        tools,
        combat=replace(
            WWN.combat, attack=replace(WWN.combat.attack, damage_bonus_stat="attack_bonus")
        ),
    )
    _foe(tools, hp=20, armor=10)
    _make_room(tools)
    tools.give_item(PC, LONG_SWORD, damage="1d8")
    take_turn(tools, PC)
    pc = _pc(app, campaign_id)
    feature = _feature(LONG_SWORD)
    expected = 5 + _best_attribute(pc, feature) + current(pc, "attack_bonus") + 1

    result = replace(tools, rng=Rolls(5, 20)).player_attack(FOE, LONG_SWORD)
    assert result["raw"] == expected
    assert result["harm"] == {"hp": expected}


def test_damage_bonus_stat_adds_to_shock(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools = replace(
        tools,
        combat=replace(
            WWN.combat, attack=replace(WWN.combat.attack, damage_bonus_stat="attack_bonus")
        ),
    )
    _foe(tools, hp=20, armor=13)
    _make_room(tools)
    tools.give_item(PC, WAR_AXE, damage="1d10")
    take_turn(tools, PC)
    pc = _pc(app, campaign_id)
    feature = _feature(WAR_AXE)
    shock = feature.shock + _modifier(pc, "str") + current(pc, "attack_bonus") + 1
    bonus = _to_hit_bonus(pc, WAR_AXE, feature)

    result = replace(tools, rng=Rolls(5, _roll_for_total(12, bonus))).player_attack(FOE, WAR_AXE)
    assert result["hit"] is False
    assert result["harm"] == {"hp": shock}


def test_an_unarmed_strike_adds_punch_skill_and_attribute_to_damage(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=20, armor=10)
    level = _raise(tools, "punch")
    take_turn(tools, PC)
    pc = _pc(app, campaign_id)
    attribute = max(WWN.character.modifier(pc.stats, stat) for stat in ("str", "dex"))
    expected = 2 + attribute + level + 1

    result = replace(tools, rng=Rolls(2, 20)).player_attack(FOE, UNARMED)
    assert result["raw"] == expected
    assert result["harm"] == {"hp": expected}


def test_an_unarmed_strike_uses_the_best_attribute_on_the_hit_roll(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=20, armor=10)
    level = _raise(tools, "punch")
    take_turn(tools, PC)
    pc = _pc(app, campaign_id)
    attribute = max(WWN.character.modifier(pc.stats, stat) for stat in ("str", "dex"))
    expected = current(pc, "attack_bonus") + level + attribute

    replace(tools, rng=Rolls(2, 10)).player_attack(FOE, UNARMED)
    [event] = _struck(app, campaign_id)
    assert event.to_hit == 10 + expected


def test_a_hit_that_takes_an_npc_to_zero_defeats_it_as_dead(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=3, armor=10)
    _make_room(tools)
    tools.give_item(PC, LONG_SWORD, damage="1d8")
    take_turn(tools, PC)

    result = replace(tools, rng=Rolls(5, 20)).player_attack(FOE, LONG_SWORD)
    assert result["defeated"] is True

    [event] = _struck(app, campaign_id)
    assert (event.defeats, event.status) == (True, "dead")
    npc = _character(app, campaign_id, FOE)
    assert npc.defeated is True
    assert npc.dead is False
    assert _events(app, campaign_id, CharacterDying.kind) == []


def test_a_hit_that_takes_a_pc_to_zero_leaves_them_mortally_wounded(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=10)
    tools.set_debug_stat(PC, "hp", 1)
    take_turn(tools, FOE)

    result = replace(tools, rng=Rolls(1, 20)).attack(FOE, "Claw", PC)
    assert result["defeated"] is True

    pc = _pc(app, campaign_id)
    assert pc.defeated is True
    assert pc.dead is False
    assert pc.dying_hours == 1

    [event] = _struck(app, campaign_id)
    assert (event.defeats, event.status) == (True, "mortally wounded")
    [dying] = _events(app, campaign_id, CharacterDying.kind)
    assert dying.hours == 1


def test_harm_outside_combat_leaves_a_pc_mortally_wounded(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.set_debug_stat(PC, "hp", 2)

    result = tools.harm_character(PC, "hp", 2, "a fall from the wall")
    assert result["defeated"] is True

    pc = _pc(app, campaign_id)
    assert pc.defeated is True
    assert pc.dead is False
    assert pc.dying_hours == 1

    [harmed] = _events(app, campaign_id, Harmed.kind)
    assert (harmed.defeats, harmed.status) == (True, "mortally wounded")
    [dying] = _events(app, campaign_id, CharacterDying.kind)
    assert dying.hours == 1


def test_harm_outside_combat_kills_an_npc(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=2)

    result = tools.harm_character(FOE, "hp", 2, "a fall from the wall")
    assert result["defeated"] is True

    npc = _character(app, campaign_id, FOE)
    assert npc.defeated is True
    [harmed] = _events(app, campaign_id, Harmed.kind)
    assert (harmed.defeats, harmed.status) == (True, "dead")


def test_npc_attack_uses_its_attack_bonus_and_shock_on_a_miss(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=20, attack_bonus=3, shock=2, shock_ac=13)
    tools.equip_armor(PC, "Large Shield", equipped=False)
    tools.set_armor(PC, 13)
    foe = _character(app, campaign_id, FOE)
    d20 = _roll_for_total(12, _hit_bonus(foe, "attack_bonus"))
    take_turn(tools, FOE)

    result = replace(tools, rng=Rolls(2, d20)).attack(FOE, "Claw", PC)
    assert result["hit"] is False
    assert result["harm"] == {"hp": 2}

    [event] = _struck(app, campaign_id)
    assert (event.hit, event.to_hit, event.shock) == (False, 12, 2)


def test_npc_attack_damage_uses_its_statblock_dice(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=20, attack_bonus=3)
    tools.set_armor(PC, 13)
    pc = _pc(app, campaign_id)
    foe = _character(app, campaign_id, FOE)
    d20 = _roll_for_total(20, _hit_bonus(foe, "attack_bonus"))
    take_turn(tools, FOE)

    result = replace(tools, rng=Rolls(4, d20)).attack(FOE, "Claw", PC)
    assert result["raw"] == 4
    assert result["harm"] == {"hp": min(4, current(pc, "hp"))}
    assert result["armor"] == 0

    [event] = _struck(app, campaign_id)
    assert (event.hit, event.to_hit) == (True, 20)


def test_a_natural_one_always_misses_and_a_natural_twenty_always_hits(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=50, armor=30)
    tools.set_armor(FOE, 0)
    take_turn(tools, PC)

    missed = replace(tools, rng=Rolls(1, 1)).player_attack(FOE, UNARMED)
    assert missed["hit"] is False
    assert missed["harm"] == {}

    tools.set_armor(FOE, 30)
    hit = replace(tools, rng=Rolls(1, 20)).player_attack(FOE, UNARMED)
    pc = _pc(app, campaign_id)
    punch = max(0, WWN.character.skill_level(pc.skills, "punch"))
    attribute = max(WWN.character.modifier(pc.stats, stat) for stat in ("str", "dex"))
    assert hit["harm"] == {"hp": 1 + attribute + punch + 1}


def test_side_initiative_is_rolled_once_both_sides_have_joined(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=20)

    with Session(app.state.engine) as db:
        encounter = load_state(db, campaign_id).encounter
    assert encounter is not None
    assert encounter.settled
    assert set(encounter.sides.values()) == {"party", "foes"}
    assert {name_key(name) for name in encounter.order} == {name_key(PC), name_key(FOE)}


def _learn(app: FastAPI, campaign_id: int, feature: str, name: str) -> None:
    with Session(app.state.engine) as db:
        record(db, campaign_id, FeatureLearned(name=PC, feature=feature, feature_name=name))
        db.commit()


def _react(tools: WorldTools, rng: random.Random) -> dict[str, Any]:
    request = ToolRequest(
        id="call_1",
        name="roll_reaction",
        arguments=json.dumps({"character": PC, "cause": "meeting a stranger"}),
    )
    run = asyncio.run(
        run_tool(WWN.tool_calls, request, ctx=SimpleNamespace(world=replace(tools, rng=rng)))
    )
    assert not isinstance(run, Ask), run.output
    return json.loads(run.output)


def test_well_met_improves_a_reaction_and_its_second_rank_is_weekly(client_factory):
    client, campaign_id, tools = _wwn_campaign(client_factory)
    _learn(client.app, campaign_id, "focus_well_met", "Well Met")

    improved = _react(tools, Rolls(3, 3))
    assert improved["total"] == improved["roll"] + improved["bonus"] + 1

    _learn(client.app, campaign_id, "focus_well_met_2", "Well Met")
    friendly = _react(tools, Rolls(3, 3))
    assert friendly["reaction"] == (
        "as friendly and helpful as their nature and the situation permit"
    )
    with Session(client.app.state.engine) as db:
        character = load_state(db, campaign_id).characters[name_key(PC)]
    assert character.reaction_uses == {"week:focus_well_met_2:well-met-2": 1}

    ordinary = _react(tools, Rolls(3, 3))
    assert ordinary["reaction"] != friendly["reaction"]

    tools.advance_time(days=7)
    friendly_again = _react(tools, Rolls(3, 3))
    assert friendly_again["reaction"] == friendly["reaction"]


def test_a_check_reaction_can_reroll_a_failed_social_check(client_factory):
    client, campaign_id, tools = _wwn_campaign(client_factory)
    base = WWN.character.features["focus_cultured_2"]
    reroll = Reaction(
        id="cultured-reroll",
        trigger="check",
        when={"skills": ("convince",), "success": False},
        effect={"op": "reroll", "keep": "better"},
        uses=1,
        reset="week",
        text="Cultured rerolls the failed check.",
    )
    spec = replace(
        WWN.character,
        features={**WWN.character.features, base.id: replace(base, reactions=(reroll,))},
    )
    tools = replace(tools, character=spec, rng=Rolls(6, 6))
    _learn(client.app, campaign_id, base.id, base.name)

    result = tools.roll_check("skill", PC, skill="convince", stat="cha", roll=2, difficulty=12)

    assert result["success"] is True
    assert result["roll"] == 12
    assert result["reaction"] == "Cultured rerolls the failed check."
    with Session(client.app.state.engine) as db:
        character = load_state(db, campaign_id).characters[name_key(PC)]
    assert character.reaction_uses == {"week:focus_cultured_2:cultured-reroll": 1}

    # The weekly use is spent, so a second failure is not rerolled.
    failed = tools.roll_check("skill", PC, skill="convince", stat="cha", roll=2, difficulty=12)
    assert failed["success"] is False
    assert "reaction" not in failed


def test_wwn_skill_check_prompts_for_the_players_own_dice(client_factory):
    """A skill check is the player's roll, not the engine's."""
    _client, _campaign_id, tools = _wwn_campaign(client_factory)
    ctx = SimpleNamespace(world=replace(tools, rng=Rolls(1)))
    request = ToolRequest(
        id="call_1",
        name="roll_check",
        arguments=json.dumps(
            {
                "prompt": "Roll to sneak past the sentry.",
                "character": PC,
                "skill": "sneak",
                "stat": "dex",
            }
        ),
    )

    ask = asyncio.run(run_tool(WWN.tool_calls, request, None, ctx=ctx))
    assert isinstance(ask, Ask)
    assert ask.dice == "2d6"
    assert "skill" in (ask.details or "")

    run = asyncio.run(run_tool(WWN.tool_calls, request, RollAnswer(12), ctx=ctx))
    assert not isinstance(run, Ask)
    result = json.loads(run.output)
    assert result["roll"] == 12
    assert result["total"] == 12 + result["bonus"]


def test_wwn_player_attack_and_save_prompt_for_the_players_own_dice(client_factory):
    """The player presses the roll button for their d20; the engine still rolls damage."""
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=20, armor=10)
    _make_room(tools)
    tools.give_item(PC, LONG_SWORD, damage="1d8")
    take_turn(tools, PC)
    tools = replace(tools, rng=Rolls(4))
    ctx = SimpleNamespace(world=tools)
    attack = ToolRequest(
        id="call_1",
        name="player_attack",
        arguments=json.dumps({"target": FOE, "weapon": LONG_SWORD, "prompt": "Roll your attack."}),
    )
    ask = asyncio.run(run_tool(WWN.tool_calls, attack, None, ctx=ctx))
    assert isinstance(ask, Ask)
    assert ask.dice == "1d20"
    assert LONG_SWORD in (ask.details or "") and FOE in (ask.details or "")

    run = asyncio.run(run_tool(WWN.tool_calls, attack, RollAnswer(20), ctx=ctx))
    assert not isinstance(run, Ask)
    result = json.loads(run.output)
    pc = _pc(app, campaign_id)
    assert result["harm"] == {"hp": result["raw"]}
    assert result["raw"] >= 4
    [struck] = _struck(app, campaign_id)
    assert (struck.hit, struck.to_hit) == (
        True,
        20 + _to_hit_bonus(pc, LONG_SWORD, _feature(LONG_SWORD)),
    )

    save = ToolRequest(
        id="call_2",
        name="player_roll_save",
        arguments=json.dumps(
            {"character": PC, "save": "physical", "cause": "poison", "prompt": "Roll a save."}
        ),
    )
    ask = asyncio.run(run_tool(WWN.tool_calls, save, None, ctx=ctx))
    assert isinstance(ask, Ask)
    assert ask.dice == "1d20"
    run = asyncio.run(run_tool(WWN.tool_calls, save, RollAnswer(1), ctx=ctx))
    assert not isinstance(run, Ask)
    saved = json.loads(run.output)
    assert (saved["roll"], saved["passed"]) == (1, False)
    [event] = _events(app, campaign_id, CharacterSaved.kind)
    assert (event.roll, event.passed, event.cause) == (1, False, "poison")


def test_wwn_npc_turn_ends_when_the_gm_reply_finishes(client_factory):
    """Starting a foe's turn runs the GM's reply, and the engine ends the turn when that reply
    finishes. The player only presses Start for the next actor. WWN rolls side initiative as soon
    as both sides have joined."""
    client, campaign_id, tools = _wwn_campaign(client_factory)
    with Session(tools.engine) as db:
        record(db, campaign_id, ChatGM(content="The opening plays out.", model="test"))
        db.commit()
    tools.create_location(YARD, "Cobbles around a dry well.")
    tools.move_actor(PC, YARD)
    tools.create_encounter_map(MAP, YARD, 10, 10, "Open cobbles.")
    tools.start_encounter(MAP)
    tools.add_participant(PC)
    _foe(tools, hp=20)
    with Session(tools.engine) as db:
        encounter = load_state(db, tools.campaign_id).encounter
        assert encounter is not None and encounter.settled
        assert FOE in encounter.order
        assert encounter.acting is None
        if encounter.order[encounter.up] == PC:
            encounters.begin_turn(db, campaign_id, PC)
            encounters.end_turn(db, campaign_id, PC, by_player=True)
    with Session(tools.engine) as db:
        encounter = load_state(db, campaign_id).encounter
        assert encounter.order[encounter.up] == FOE

    token = csrf_token(client, "/campaigns")
    response = client.post(
        f"/campaigns/{campaign_id}/encounter/turn/stream",
        data={"csrf_token": token, "name": FOE},
    )
    assert response.status_code == 200
    assert response.text.strip()

    with Session(tools.engine) as db:
        state = load_state(db, tools.campaign_id)
        assert state.turn is None
        assert state.encounter.acting is None
        assert state.encounter.order[state.encounter.up] == PC
    page = client.get(f"/campaigns/{campaign_id}").text
    assert f"End {FOE}'s turn" not in page
    assert f"Start {PC}'s turn" in page


def test_wwn_player_save_prompt_works_outside_an_encounter(client_factory):
    client, campaign_id, tools = _wwn_campaign(client_factory)
    ctx = SimpleNamespace(world=tools)
    request = ToolRequest(
        id="call_1",
        name="player_roll_save",
        arguments=json.dumps(
            {"character": PC, "save": "luck", "cause": "a falling stone", "prompt": "Roll."}
        ),
    )
    ask = asyncio.run(run_tool(WWN.tool_calls, request, None, ctx=ctx))
    assert isinstance(ask, Ask)
    run = asyncio.run(run_tool(WWN.tool_calls, request, RollAnswer(20), ctx=ctx))
    assert not isinstance(run, Ask)
    result = json.loads(run.output)
    assert (result["roll"], result["passed"]) == (20, True)
    [event] = _events(client.app, campaign_id, CharacterSaved.kind)
    assert (event.roll, event.passed, event.cause) == (20, True, "a falling stone")


def test_wwn_mortal_wounds_count_six_rounds(client_factory):
    client, campaign_id, tools = _wwn_campaign(client_factory)
    with Session(client.app.state.engine) as db:
        pc = load_state(db, campaign_id).pc()
        assert pc is not None
    tools.harm_character(PC, "hp", 99, "a test wound")
    with Session(client.app.state.engine) as db:
        pc = load_state(db, campaign_id).characters[name_key(PC)]
        assert (pc.dying_rounds, pc.dying_hours, pc.dead) == (6, 1, False)

    with Session(client.app.state.engine) as db:
        for _ in range(5):
            death.tick_dying_rounds(db, campaign_id)
        pc = load_state(db, campaign_id).characters[name_key(PC)]
        assert (pc.dying_rounds, pc.dead) == (1, False)
        death.tick_dying_rounds(db, campaign_id)
        pc = load_state(db, campaign_id).characters[name_key(PC)]
        assert (pc.dying_rounds, pc.dead) == (None, True)


def _player_turn(app: FastAPI, campaign_id: int) -> None:
    """Make the player character the acting one, playing through any NPC turns first."""
    with Session(app.state.engine) as db:
        state = load_state(db, campaign_id)
        encounter = state.encounter
        assert encounter is not None
        for _ in range(len(encounter.order)):
            state = load_state(db, campaign_id)
            encounter = state.encounter
            assert encounter is not None
            if encounter.acting is not None:
                if encounter.acting == PC:
                    return
                encounters.end_turn(db, campaign_id, encounter.acting, by_player=False)
                continue
            next_name = encounter.order[encounter.up]
            encounters.begin_turn(db, campaign_id, next_name)
            if next_name == PC:
                return
            record(db, campaign_id, ChatGM(content="The NPC turn plays out.", model="test"))
            db.commit()
            encounters.end_turn(db, campaign_id, next_name, by_player=False)
    raise AssertionError(f"{PC} could not be made the acting character")


def test_a_stowed_weapon_must_be_readied_before_it_can_strike(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=20)
    _make_room(tools)
    tools.give_item(PC, WAR_AXE, damage="1d10")
    tools.ready_item(PC, WAR_AXE, ready=False)
    assert next(i for i in _pc(app, campaign_id).items if i.name == WAR_AXE).ready is False
    _player_turn(app, campaign_id)

    with pytest.raises(InvalidInput, match="stowed; ready it with ready_item"):
        tools.player_attack(FOE, WAR_AXE, attack_roll=20)

    tools.ready_item(PC, WAR_AXE, ready=True)
    assert next(i for i in _pc(app, campaign_id).items if i.name == WAR_AXE).ready is True
    result = tools.player_attack(FOE, WAR_AXE, attack_roll=20, damage_roll=4)
    assert result["harm"]["hp"] > 0


def test_a_statblocks_attack_count_is_resolved_in_one_action(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=30)
    tools.create_monster("Ember Hound", YARD, template="ember_hound")
    tools.add_participant("Ember Hound")
    tools.set_stat_max(PC, "hp", 30, "for a long test fight")

    before = len(_struck(app, campaign_id))
    take_turn(tools, "Ember Hound")
    tools.attack("Ember Hound", "bite", PC)
    assert len(_struck(app, campaign_id)) - before == 2


def test_a_single_attack_statblock_resolves_one_attack(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=30)
    tools.create_monster("Rot Shambler", YARD, template="rot_shambler")
    tools.add_participant("Rot Shambler")
    tools.set_stat_max(PC, "hp", 30, "for a long test fight")

    before = len(_struck(app, campaign_id))
    take_turn(tools, "Rot Shambler")
    tools.attack("Rot Shambler", "claw", PC)
    assert len(_struck(app, campaign_id)) - before == 1


def test_a_four_attack_statblock_resolves_all_four_attacks(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=30)
    tools.create_monster("Tide Grasper", YARD, template="tide_grasper")
    tools.add_participant("Tide Grasper")
    tools.set_stat_max(PC, "hp", 200, "for a long test fight")

    before = len(_struck(app, campaign_id))
    take_turn(tools, "Tide Grasper")
    result = tools.attack("Tide Grasper", "tentacle", PC)
    assert len(_struck(app, campaign_id)) - before == 4
    assert len(result["attacks"]) == 4


def test_an_important_npc_is_mortally_wounded_rather_than_killed(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.create_npc(
        "Elder Marrow",
        YARD,
        "A named elder of the village.",
        important=True,
    )

    tools.harm_character("Elder Marrow", "hp", 99, "a test wound")

    with Session(app.state.engine) as db:
        npc = load_state(db, campaign_id).characters[name_key("Elder Marrow")]
        assert npc.important is True
        assert (npc.dying_rounds, npc.dead) == (6, False)
    [harmed] = _events(app, campaign_id, Harmed.kind)
    assert harmed.status == "mortally wounded"


def test_a_week_of_bed_rest_ends_frail(client_factory):
    from cygnusloom.services.events import ConditionAdded, record

    client, campaign_id, tools = _wwn_campaign(client_factory)
    app = client.app
    with Session(app.state.engine) as db:
        record(db, campaign_id, ConditionAdded(name=PC, condition="frail", cause="a mortal wound"))
        db.commit()

    result = tools.rest("extended", reason="recover from a mortal wound")

    assert PC not in result["held_back"]
    with Session(app.state.engine) as db:
        assert load_state(db, campaign_id).characters[name_key(PC)].conditions == []


def test_a_shield_turns_aside_only_the_first_shock_in_a_round(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools = replace(tools, marketplace=WWN.marketplace)
    _foe(tools, hp=20, shock=2, shock_ac=20, attack_bonus=0)
    pc = _pc(app, campaign_id)
    assert any("shield" in item.flags for item in pc.items)
    _ready_shield(tools)
    tools.equip_armor(PC, "Large Shield")
    take_turn(tools, FOE)

    replace(tools, rng=Rolls(1, 1, 1, 1, 1, 1)).attack(FOE, "Claw", PC)
    first = _struck(app, campaign_id)[-1]
    assert (first.hit, first.shock, first.shield_round) == (False, 0, 1)

    replace(tools, rng=Rolls(1, 1, 1, 1, 1, 1)).attack(FOE, "Claw", PC)
    second = _struck(app, campaign_id)[-1]
    assert (second.hit, second.shock, second.shield_round) == (False, 2, 0)


def test_a_carried_shield_only_blocks_shock_when_wielded(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools = replace(tools, marketplace=WWN.marketplace)
    _foe(tools, hp=20, shock=2, shock_ac=20, attack_bonus=0)
    tools.set_stat_max(PC, "hp", 20, cause="a test")
    _ready_shield(tools)
    tools.equip_armor(PC, "Large Shield", equipped=False)
    tools.set_armor(PC, 13)
    take_turn(tools, FOE)

    replace(tools, rng=Rolls(1, 1, 1, 1, 1, 1)).attack(FOE, "Claw", PC)
    carried = _struck(app, campaign_id)[-1]
    assert (carried.hit, carried.shock, carried.shield_round) == (False, 2, 0)

    tools.equip_armor(PC, "Large Shield")
    replace(tools, rng=Rolls(1, 1, 1, 1, 1, 1)).attack(FOE, "Claw", PC)
    wielded = _struck(app, campaign_id)[-1]
    assert (wielded.hit, wielded.shock, wielded.shield_round) == (False, 0, 1)


def test_creating_a_magic_item_charges_its_cost_and_records_its_powers(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools = replace(tools, projects=WWN.projects, marketplace=WWN.marketplace)
    tools.change_currency(PC, 100, "a test purse")
    _make_room(tools)

    result = tools.create_magic_item(
        PC,
        "Staff of Ash",
        "A staff that sheds ember light and adds 1 to fire damage.",
        damage="1d6",
        cost=100,
    )
    assert (result["magic_item"], result["cost"]) == ("Staff of Ash", 100)
    with Session(app.state.engine) as db:
        pc = load_state(db, campaign_id).characters[name_key(PC)]
    staff = next(item for item in pc.items if item.name == "Staff of Ash")
    assert staff.damage == "1d6"
    assert "ember light" in staff.text
    assert pc.features.get("silver", 0) == 0


def test_a_source_project_charges_its_cost_and_requires_its_skill(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools = replace(tools, projects=WWN.projects, marketplace=WWN.marketplace)
    tools.change_currency(PC, 1000, "a test purse")
    tools.raise_skill(PC, "magic", 1, "a test teacher")

    project = tools.start_project("Staff of Ash", "magic_item", "A fire staff", owner=PC)
    assert (project["cost"], project["currency"], project["owner"]) == (1000, "silver", PC)
    with Session(app.state.engine) as db:
        pc = load_state(db, campaign_id).characters[name_key(PC)]
    assert pc.features.get("silver", 0) == 0
    with pytest.raises(InvalidInput, match="costs 1000 silver"):
        tools.start_project("Second Staff", "magic_item", "Another staff", owner=PC)


def test_a_developed_attribute_focus_stores_a_validated_choice(client_factory):
    client, campaign_id, tools = _wwn_campaign(
        client_factory,
        choices={"pick-feature-class_focus_any-0": "focus_developed_attribute"},
    )
    tools.choose_feature_choice(PC, "Developed Attribute", "attribute", "str")
    with Session(client.app.state.engine) as db:
        pc = load_state(db, campaign_id).characters[name_key(PC)]
    assert pc.feature_choices["focus_developed_attribute:attribute"] == ["str"]
    with pytest.raises(InvalidInput, match="not an attribute"):
        tools.choose_feature_choice(PC, "Developed Attribute", "attribute", "magic")


def test_an_execution_against_an_unaware_target_does_maximum_damage(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=30)
    _make_room(tools)
    tools.give_item(PC, LONG_SWORD, damage="1d8")
    tools.add_condition(FOE, "unaware", "watching the wrong doorway")

    tools.prepare_execution(PC, LONG_SWORD)
    take_turn(tools, PC)
    result = tools.execute(FOE, LONG_SWORD)

    assert result["execution"] is True
    assert result["damage"] == 8
    assert _character(app, campaign_id, PC).execution_prepared is False


def test_grappling_restricts_both_sides_to_unarmed_attacks(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=20)
    _make_room(tools)
    tools.give_item(PC, LONG_SWORD, damage="1d8")

    tools.grapple(FOE, PC)
    holder = _character(app, campaign_id, FOE)
    target = _character(app, campaign_id, PC)
    assert (holder.grappling, holder.grappled_by) == (PC, "")
    assert (target.grappling, target.grappled_by) == ("", FOE)
    assert "grappled" in holder.conditions
    assert "grappled" in target.conditions
    take_turn(tools, PC)
    with pytest.raises(InvalidInput, match="grappled"):
        tools.player_attack(FOE, LONG_SWORD, attack_roll=10, damage_roll=2)

    tools.break_grapple(PC)
    freed = _character(app, campaign_id, PC)
    assert (freed.grappling, freed.grappled_by) == ("", "")
    assert "grappled" not in freed.conditions


def test_a_wwn_attack_needs_the_attackers_turn(client_factory):
    """A foe may not swing before the player presses Start for its turn, and the player may not
    swing out of turn either. The GM can force an action the story needs."""
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=20)

    with pytest.raises(InvalidInput, match="no one is taking a turn"):
        tools.attack(FOE, "Claw", PC)
    assert _struck(app, campaign_id) == []

    forced = tools.attack(FOE, "Claw", PC, force=True)
    assert forced["target"] == PC

    take_turn(tools, PC)
    with pytest.raises(InvalidInput, match=f"it is {PC}'s turn"):
        tools.attack(FOE, "Claw", PC)
    hit = tools.player_attack(FOE, "Short Sword", attack_roll=20, damage_roll=5)
    assert hit["target"] == FOE


def test_a_surprised_character_loses_a_turn_before_acting(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=20)

    result = tools.surprise(FOE)
    assert result["turns"] == 1
    assert _character(app, campaign_id, FOE).surprised_turns == 1
    take_turn(tools, FOE)
    with pytest.raises(InvalidInput, match="surprised"):
        tools.attack(FOE, "Claw", PC)

    with Session(app.state.engine) as db:
        record(db, campaign_id, EncounterTurnEnded(name=FOE))
        db.commit()
    assert _character(app, campaign_id, FOE).surprised_turns == 0


def test_long_range_takes_the_configured_penalty(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=20, armor=20)
    _make_room(tools)
    tools.give_item(PC, BOW, damage="1d8")
    take_turn(tools, PC)

    short = tools.player_attack(FOE, BOW, range_band="short", attack_roll=10, damage_roll=2)
    short_hit = _struck(app, campaign_id)[-1]
    long = tools.player_attack(FOE, BOW, range_band="long", attack_roll=10, damage_roll=2)
    long_hit = _struck(app, campaign_id)[-1]

    assert short["hit"] is False
    assert long["hit"] is False
    assert long_hit.to_hit == short_hit.to_hit - 2


def test_dual_wielding_applies_its_hit_penalty_and_damage_bonus(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=20, armor=20)
    _make_room(tools)
    tools.give_item(PC, LONG_SWORD, damage="1d8")
    take_turn(tools, PC)

    tools.player_attack(FOE, LONG_SWORD, attack_roll=10, damage_roll=2)
    single = _struck(app, campaign_id)[-1]
    tools.player_attack(FOE, LONG_SWORD, attack_roll=10, damage_roll=2, dual_wield=True)
    paired = _struck(app, campaign_id)[-1]

    assert paired.to_hit == single.to_hit - 1
    assert paired.raw == single.raw + 2


def test_total_defense_adds_its_bonus_until_the_next_turn(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=20)
    before = _pc(app, campaign_id)

    result = tools.total_defense(PC)
    assert result["bonus"] == 2
    guarding = _pc(app, campaign_id)
    assert guarding.total_defense is True
    assert guarding.armor == before.armor + 2

    with Session(app.state.engine) as db:
        record(db, campaign_id, EncounterTurnBegun(name=PC))
        db.commit()

    restored = _pc(app, campaign_id)
    assert restored.total_defense is False
    assert restored.armor == before.armor


def test_a_shield_bash_suspends_the_shields_ac_until_the_next_turn(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools = replace(tools, marketplace=WWN.marketplace)
    _foe(tools, hp=20)
    tools.set_stat_max(PC, "str", 18, "room for the shield and its bash")
    _ready_shield(tools)
    tools.equip_armor(PC, "Large Shield")
    tools.give_item(PC, "Shield Bash, Large", damage="1d6")
    take_turn(tools, PC)

    before = _pc(app, campaign_id)
    assert before.shield_bonus > 0
    tools.player_attack(FOE, "Shield Bash, Large", attack_roll=20, damage_roll=2)

    suspended = _pc(app, campaign_id)
    assert suspended.shield_suspended is True
    assert suspended.armor == before.armor - before.shield_bonus

    with Session(app.state.engine) as db:
        record(db, campaign_id, EncounterTurnBegun(name=PC))
        db.commit()

    restored = _pc(app, campaign_id)
    assert restored.shield_suspended is False
    assert restored.armor == before.armor


def test_equipping_a_shield_on_a_statblock_uses_its_printed_ac_as_the_baseline(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools = replace(tools, marketplace=WWN.marketplace)
    _foe(tools, armor=18)
    assert _character(app, campaign_id, FOE).defense_tracked is False
    tools.give_item(FOE, "Large Shield")

    tools.equip_armor(FOE, "Large Shield")
    foe = _character(app, campaign_id, FOE)
    assert (foe.armor, foe.innate_defense, foe.defense_adjustment) == (19, 18, 0)
    tools.ready_item(FOE, "Large Shield", False)
    assert _character(app, campaign_id, FOE).armor == 18


@pytest.mark.parametrize("attacks", [1, 2])
def test_injury_effects_refresh_defense_before_the_next_hit(client_factory, attacks: int):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=20)
    tools.set_stat_max(PC, "dex", 14, cause="a test")
    tools.set_stat_max(PC, "hp", 20, cause="a test")
    tools.adjust_counter(PC, "focus_picks", 1)
    wwn.learn_focus(PC, "focus_impervious_defense", SimpleNamespace(world=tools))
    assert _pc(app, campaign_id).armor == 18
    table = InjuryTable(
        id="test",
        stat="hp",
        entries=(
            InjuryEntry(
                roll=1,
                name="Inspiration",
                text="A sudden insight.",
                effects=({"op": "raise_stat", "stat": "dex", "delta": 4, "cap": 18},),
            ),
        ),
        trigger="any",
        target_kinds=("pc",),
    )
    combat = replace(WWN.combat, injury_table=table)
    rolls = (1, 20) if attacks == 1 else (1, 1, 20, 17)
    take_turn(tools, FOE)
    result = replace(tools, combat=combat, rng=Rolls(*rolls)).attack(FOE, "Claw", PC, count=attacks)

    assert _pc(app, campaign_id).armor == 19
    if attacks == 2:
        assert result["attacks"][1]["hit"] is False


def test_inventory_displays_worn_and_wielded_equipment(client_factory):
    client, campaign_id, tools = _wwn_campaign(client_factory)
    tools = replace(tools, marketplace=WWN.marketplace)
    _ready_shield(tools)
    tools.give_item(PC, "Buff Coat")
    tools.equip_armor(PC, "Buff Coat")
    tools.equip_armor(PC, "Large Shield")

    page = client.get(f"/campaigns/{campaign_id}").text
    start = page.index('id="panel-inventory"')
    panel = page[start : page.index("</ul>", start)]
    assert "Worn: Buff Coat" in panel
    assert "Wielded shield: Large Shield" in panel
    assert "· Readied" in panel
    assert "· Worn" in panel
    assert "· Wielded" in panel

    tools.ready_item(PC, "Large Shield", False)
    page = client.get(f"/campaigns/{campaign_id}").text
    start = page.index('id="panel-inventory"')
    panel = page[start : page.index("</ul>", start)]
    assert "Wielded shield: none" in panel
    assert "· Stowed" in panel


def test_lookup_condition_returns_its_effects_and_triggers(client_factory):
    _, _, tools = _wwn_campaign(client_factory)

    found = tools.lookup_condition("frail")
    assert found["label"] == "Frail"
    assert found["ends"] == ["extended_rest", "magic"]

    with pytest.raises(InvalidInput, match="there is no condition named 'nope'"):
        tools.lookup_condition("nope")


def test_healing_restores_harm_and_never_goes_below_zero(client_factory):
    client, campaign_id, tools = _wwn_campaign(client_factory)
    app = client.app
    from cygnusloom.services.events import Recovered

    tools.harm_character(PC, "hp", 2, "a test wound")
    healed = tools.heal_character(PC, 5, cause="a healing art")

    assert healed["restored"] == 2
    assert healed["harm"] == 0
    assert _pc(app, campaign_id).harm.get("hp", 0) == 0
    [event] = _events(app, campaign_id, Recovered.kind)
    assert (event.stat, event.amount, event.cause) == ("hp", 2, "a healing art")


def test_healing_with_no_harm_records_zero_and_changes_nothing(client_factory):
    client, campaign_id, tools = _wwn_campaign(client_factory)
    app = client.app
    from cygnusloom.services.events import Recovered

    healed = tools.heal_character(PC, 5, cause="a healing art")

    assert healed["restored"] == 0
    assert healed["harm"] == 0
    assert _pc(app, campaign_id).harm == {}
    [event] = _events(app, campaign_id, Recovered.kind)
    assert event.amount == 0


def test_a_wounded_character_sheet_shows_the_harm_left(client_factory):
    """A player reads their own sheet to know how hurt they are, so a harmed stat shows its
    remaining value beside its score."""
    client, campaign_id, tools = _wwn_campaign(client_factory)
    app = client.app
    tools.set_stat_max(PC, "hp", 10, "for a test wound")

    tools.harm_character(PC, "hp", 3, "a test wound")

    hurt = _pc(app, campaign_id)
    assert current(hurt, "hp") == 7
    with Session(app.state.engine) as db:
        sheet = scene_view(load_state(db, campaign_id), WWN.character).sheet
    assert "Current: Hit Points 7 of 10" in sheet


def test_an_unwounded_character_sheet_has_no_current_line(client_factory):
    client, campaign_id, _tools = _wwn_campaign(client_factory)

    with Session(client.app.state.engine) as db:
        sheet = scene_view(load_state(db, campaign_id), WWN.character).sheet

    assert not any(line.startswith("Current:") for line in sheet)


def test_default_healing_stat_is_ruleset_configurable(client_factory):
    client, campaign_id, tools = _wwn_campaign(client_factory)
    assert tools.combat is not None
    rules = replace(
        tools.combat,
        healing_stat="str",
        dying=replace(tools.combat.dying, outside_harm=("hp", "str")),
    )
    tools = replace(tools, combat=rules)
    tools.harm_character(PC, "str", 2, "a test wound")

    healed = tools.heal_character(PC, 1)

    assert healed["stat"] == "str"
    assert healed["restored"] == 1
    assert _pc(client.app, campaign_id).harm["str"] == 1


def test_healing_without_a_default_requires_an_explicit_stat(client_factory):
    _, _, tools = _wwn_campaign(client_factory)
    assert tools.combat is not None
    tools = replace(tools, combat=replace(tools.combat, healing_stat=None))
    with pytest.raises(InvalidInput, match="no default healing stat"):
        tools.heal_character(PC, 1)
    assert tools.heal_character(PC, 1, stat="hp")["stat"] == "hp"


def test_a_project_tracks_progress_to_completion(client_factory):
    client, campaign_id, tools = _wwn_campaign(client_factory)

    started = tools.start_project("Map the Vault", "research", "Find the sealed gallery.", 10)
    assert (started["progress"], started["completed"]) == (0, False)
    with pytest.raises(InvalidInput, match="already a project"):
        tools.start_project("Map the Vault", "research", "Again.", 5)

    tools.advance_project("Map the Vault", 4, note="A week in the archive.")
    half = tools.advance_project("Map the Vault", 6, note="The last rubbing.")
    assert (half["progress"], half["completed"]) == (10, True)
    with pytest.raises(InvalidInput, match="already complete"):
        tools.advance_project("Map the Vault", 1)

    with Session(client.app.state.engine) as db:
        state = load_state(db, campaign_id)
        assert state.projects[name_key("Map the Vault")].notes == [
            "A week in the archive.",
            "The last rubbing.",
        ]
    assert [p["name"] for p in tools.list_projects()] == ["Map the Vault"]


def _roomy_pack(tools: WorldTools) -> WorldTools:
    """A single-pool inventory large enough for the armor tests."""
    return replace(
        tools,
        marketplace=WWN.marketplace,
        inventory=replace(
            WWN.inventory,
            pools=(),
            size_pools={},
            default_pool="",
            capacity=100,
            capacity_stat="",
            capacity_multiplier=1,
        ),
    )


def test_medium_armor_penalizes_only_sneak_by_its_encumbrance(client_factory):
    _, _, tools = _wwn_campaign(client_factory)
    tools = _roomy_pack(tools)
    tools.give_item(PC, "Mail Shirt")
    tools.equip_armor(PC, "Mail Shirt")

    sneak = tools.roll_check("skill", PC, skill="sneak", stat="dex", difficulty=8)
    exert = tools.roll_check("skill", PC, skill="exert", stat="str", difficulty=8)

    assert sneak["armor_penalty"] == -1
    assert exert["armor_penalty"] == 0


def test_heavy_armor_penalizes_sneak_and_exert_by_its_encumbrance(client_factory):
    _, _, tools = _wwn_campaign(client_factory)
    tools = _roomy_pack(tools)
    tools.give_item(PC, "Mail Hauberk")
    tools.equip_armor(PC, "Mail Hauberk")

    sneak = tools.roll_check("skill", PC, skill="sneak", stat="dex", difficulty=8)
    exert = tools.roll_check("skill", PC, skill="exert", stat="str", difficulty=8)

    assert sneak["armor_penalty"] == -2
    assert exert["armor_penalty"] == -2


def test_light_armor_has_no_check_penalty(client_factory):
    client, campaign_id, tools = _wwn_campaign(client_factory)
    tools = _roomy_pack(tools)
    tools.give_item(PC, "Buff Coat")
    tools.equip_armor(PC, "Buff Coat")

    result = tools.roll_check("skill", PC, skill="sneak", stat="dex", difficulty=8)

    assert result["armor_penalty"] == 0
    assert _pc(client.app, campaign_id).equipped_armor == "Buff Coat"
