"""Tests for the Worlds Without Number combat runtime: defense, Shock, hit and damage bonuses,
and the defeat rules. The campaign and character come from the real creation form, as a player
would make them."""

from __future__ import annotations

import random
import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusforge.content.character import Feature
from cygnusforge.content.library import CombatRules, ContentLibrary
from cygnusforge.services.events import (
    CharacterDying,
    Harmed,
    Struck,
    current,
    decode,
    events_since,
    load_state,
    name_key,
)
from cygnusforge.services.world import WorldTools
from tests.conftest import content_library
from tests.test_campaigns import _create_character, _new_campaign, _signed_in
from tests.test_character_creation import _choices, _rolled_stats
from tests.test_combat import Rolls, _character
from tests.test_world import _db

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
        grid=WWN.grid,
        combat=combat or WWN.combat,
        monsters=WWN.monsters,
        checks=WWN.checks,
        conditions=WWN.conditions,
        counters=WWN.counters,
        moves=WWN.moves,
        inventory=WWN.inventory,
        item_sizes=WWN.item_sizes,
        plus=WWN.plus,
        npc_hp=WWN.npc_hp,
        npc_hp_stat=WWN.npc_hp_stat,
    )


def _wwn_campaign(client_factory: Callable[[], TestClient]) -> tuple[TestClient, int, WorldTools]:
    """A latter-earth campaign with a newly created player character and the WWN world tools."""
    client = _signed_in(client_factory)
    response = _new_campaign(client, world_template="latter-earth", rule_set="worldswithoutnumber")
    assert response.status_code == 303
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/campaigns/{campaign_id}").text
    fields = {f"stat-{stat}": str(value) for stat, value in _rolled_stats(page).items()}
    fields.update(_choices(page))
    created = _create_character(client, campaign_id, name=PC, fields=fields)
    error = re.search(r'<p class="form-error"[^>]*>(.*?)</p>', created.text, re.DOTALL)
    assert created.status_code == 303, error.group(1) if error else "no error"
    return client, campaign_id, _wwn_tools(client.app, campaign_id)


def _fight(client_factory: Callable[[], TestClient]) -> tuple[FastAPI, int, WorldTools]:
    """A WWN campaign with a mapped yard, an encounter, and the player character's initiative
    rolled. WWN still asks a player character to roll initiative, so the test rolls it; the rule
    set's `roll_order` keeps the order the characters joined in."""
    client, campaign_id, tools = _wwn_campaign(client_factory)
    tools.create_location(YARD, "Cobbles around a dry well.")
    tools.move_actor(PC, YARD)
    tools.create_encounter_map(MAP, YARD, 10, 10, "Open cobbles.")
    tools.start_encounter(MAP)
    tools.add_participant(PC)
    tools.player_initiative(1)
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


def _best_attribute(character, feature: Feature | None) -> int:
    if feature is None:
        return 0
    scores = [_modifier(character, stat) for stat in feature.attributes if stat in character.stats]
    return max(scores) if scores else 0


def _attack_skill(weapon: str, feature: Feature | None) -> str:
    if feature is not None and feature.skill:
        return feature.skill
    if name_key(weapon) in WWN.combat.unarmed_names:
        return "punch"
    return ""


def _to_hit_bonus(character, weapon: str, feature: Feature | None) -> int:
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
    tools.give_item(PC, BOW, damage="1d8")
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
    assert hit["harm"] == {"hp": 4 + _modifier(pc, "dex")}
    assert _character(app, campaign_id, FOE).harm == hit["harm"]

    miss_event, hit_event = _struck(app, campaign_id)
    assert (miss_event.hit, miss_event.to_hit) == (False, 12)
    assert (hit_event.hit, hit_event.to_hit) == (True, 13)


def test_a_miss_still_does_shock_to_a_low_ac_target(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=20, armor=13)
    tools.give_item(PC, WAR_AXE, damage="1d10")
    pc = _pc(app, campaign_id)
    feature = _feature(WAR_AXE)
    shock = feature.shock + _modifier(pc, "str")
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
    tools.give_item(PC, WAR_AXE, damage="1d10")
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
    tools.give_item(PC, WAR_AXE, damage="1d10")
    pc = _pc(app, campaign_id)
    feature = _feature(WAR_AXE)
    shock = feature.shock + _modifier(pc, "str")
    bonus = _to_hit_bonus(pc, WAR_AXE, feature)

    # A damage roll of 1 plus the Strength modifier is below the Shock, so Shock is the damage.
    result = replace(tools, rng=Rolls(1, _roll_for_total(13, bonus))).player_attack(FOE, WAR_AXE)
    assert result["raw"] == shock
    assert result["harm"] == {"hp": shock}


def test_a_ranged_weapon_does_no_shock_on_a_miss(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=20, armor=13)
    tools.give_item(PC, BOW, damage="1d8")
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
    tools.give_item(PC, LONG_SWORD, damage="1d8")
    level = _raise(tools, "stab")
    pc = _pc(app, campaign_id)
    _feature(LONG_SWORD)
    attribute = max(WWN.character.modifier(pc.stats, stat) for stat in ("str", "dex"))
    expected = current(pc, "attack_bonus") + level + attribute

    replace(tools, rng=Rolls(3, 10)).player_attack(FOE, LONG_SWORD)
    [event] = _struck(app, campaign_id)
    assert event.to_hit == 10 + expected


def test_an_unskilled_attack_takes_the_penalty(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=20, armor=10)
    pc = _pc(app, campaign_id)
    weapon, feature = _unskilled_weapon(pc)
    if weapon != UNARMED:
        tools.give_item(PC, weapon, damage=feature.damage)
    attribute = _best_attribute(pc, feature)
    expected = current(pc, "attack_bonus") + attribute - WWN.combat.unskilled_attack_penalty

    replace(tools, rng=Rolls(3, 10)).player_attack(FOE, weapon)
    [event] = _struck(app, campaign_id)
    assert event.to_hit == 10 + expected


def test_damage_adds_the_weapon_attribute_and_damage_bonus_stat(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools = replace(tools, combat=replace(WWN.combat, damage_bonus_stat="attack_bonus"))
    _foe(tools, hp=20, armor=10)
    tools.give_item(PC, LONG_SWORD, damage="1d8")
    pc = _pc(app, campaign_id)
    feature = _feature(LONG_SWORD)
    expected = 5 + _best_attribute(pc, feature) + current(pc, "attack_bonus")

    result = replace(tools, rng=Rolls(5, 20)).player_attack(FOE, LONG_SWORD)
    assert result["raw"] == expected
    assert result["harm"] == {"hp": expected}


def test_damage_bonus_stat_adds_to_shock(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools = replace(tools, combat=replace(WWN.combat, damage_bonus_stat="attack_bonus"))
    _foe(tools, hp=20, armor=13)
    tools.give_item(PC, WAR_AXE, damage="1d10")
    pc = _pc(app, campaign_id)
    feature = _feature(WAR_AXE)
    shock = feature.shock + _modifier(pc, "str") + current(pc, "attack_bonus")
    bonus = _to_hit_bonus(pc, WAR_AXE, feature)

    result = replace(tools, rng=Rolls(5, _roll_for_total(12, bonus))).player_attack(FOE, WAR_AXE)
    assert result["hit"] is False
    assert result["harm"] == {"hp": shock}


def test_an_unarmed_strike_adds_punch_skill_and_attribute_to_damage(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=20, armor=10)
    level = _raise(tools, "punch")
    pc = _pc(app, campaign_id)
    attribute = max(WWN.character.modifier(pc.stats, stat) for stat in ("str", "dex"))
    expected = 2 + attribute + level

    result = replace(tools, rng=Rolls(2, 20)).player_attack(FOE, UNARMED)
    assert result["raw"] == expected
    assert result["harm"] == {"hp": expected}


def test_an_unarmed_strike_uses_the_best_attribute_on_the_hit_roll(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=20, armor=10)
    level = _raise(tools, "punch")
    pc = _pc(app, campaign_id)
    attribute = max(WWN.character.modifier(pc.stats, stat) for stat in ("str", "dex"))
    expected = current(pc, "attack_bonus") + level + attribute

    replace(tools, rng=Rolls(2, 10)).player_attack(FOE, UNARMED)
    [event] = _struck(app, campaign_id)
    assert event.to_hit == 10 + expected


def _unskilled_weapon(character) -> tuple[str, Feature | None]:
    """A weapon whose combat skill the character does not have, preferring a weapon feature."""
    options: Sequence[tuple[str, Feature | None]] = (
        (LONG_SWORD, _feature(LONG_SWORD)),
        (BOW, _feature(BOW)),
        (UNARMED, None),
    )
    for weapon, feature in options:
        skill = _attack_skill(weapon, feature)
        if WWN.character.skill_level(character.skills, skill) < 0:
            return weapon, feature
    pytest.skip("the created character has every combat skill")


def test_a_hit_that_takes_an_npc_to_zero_defeats_it_as_dead(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, hp=3, armor=10)
    tools.give_item(PC, LONG_SWORD, damage="1d8")

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
    tools.set_armor(PC, 13)
    foe = _character(app, campaign_id, FOE)
    d20 = _roll_for_total(12, _hit_bonus(foe, "attack_bonus"))

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

    result = replace(tools, rng=Rolls(4, d20)).attack(FOE, "Claw", PC)
    assert result["raw"] == 4
    assert result["harm"] == {"hp": min(4, current(pc, "hp"))}
    assert result["armor"] == 0

    [event] = _struck(app, campaign_id)
    assert (event.hit, event.to_hit) == (True, 20)
