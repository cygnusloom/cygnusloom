"""Contract checks for the Worlds Without Number ruleset.

These pin the loaded content and tool catalog the engine builds on: the project and hireling
tools, the separate Readied and Stowed pools, the combat defaults, and the structured bestiary
fields.
"""

from __future__ import annotations

import re
from dataclasses import replace

from cygnusloom.content.library import RuleSet
from cygnusloom.rules.wwn_progression import require_wwn_progression
from cygnusloom.services.events import Character
from cygnusloom.services.inventory import capacity_of, readied_capacity
from tests.conftest import content_library


def _wwn() -> RuleSet:
    return content_library().rule_sets["worldswithoutnumber"]


def test_wwn_tool_catalog_exposes_project_and_hireling_tools() -> None:
    tools = _wwn().tool_calls
    expected = {
        "hire_hireling",
        "hire_existing_npc",
        "set_hireling_wage",
        "dismiss_hireling",
        "start_project",
        "advance_project",
        "abandon_project",
        "list_projects",
        "add_faction_asset",
        "update_faction_asset",
        "remove_faction_asset",
        "faction_asset_acted",
        "damage_faction_asset",
        "repair_faction_asset",
        "take_faction_turn",
    }
    assert expected <= set(tools)
    for tool_id in expected:
        call = tools[tool_id]
        assert (call.function is None) != (call.world_method is None), tool_id


def test_wwn_important_npc_defeat_label_loads() -> None:
    combat = _wwn().combat
    assert combat is not None
    assert combat.defeat["important_npc"] == "mortally wounded"
    assert combat.defeat["npc"] == "dead"


def test_wwn_readied_and_stowed_capacities_are_separate() -> None:
    rules = _wwn().inventory
    assert rules is not None
    assert [pool.id for pool in rules.pools] == ["stowed", "readied"]
    assert [pool.ready for pool in rules.pools] == [False, True]
    assert rules.default_pool == "stowed"
    assert rules.size_pools["massive"] == "stowed"

    character = Character(
        kind="pc",
        name="Parity Probe",
        location=None,
        description="",
        personality="",
        accent="",
        quirks=[],
        stats={"str": 18},
    )
    assert capacity_of(character, rules) == 27
    assert readied_capacity(character, rules) == 9

    odd = replace(character, stats={"str": 11})
    assert capacity_of(odd, rules) == 16
    assert readied_capacity(odd, rules) == 5


def test_wwn_statblock_multi_attack_uses_each_attack() -> None:
    combat = _wwn().combat
    assert combat is not None
    assert combat.multi_attack == "each"


def test_wwn_bestiary_abilities_are_structured_fields() -> None:
    monsters = _wwn().monsters
    spore = monsters["ember_hound"].abilities[0]
    assert (spore.kind, spore.trigger, spore.save) == ("save_or_condition", "hit", "save_physical")
    cry = monsters["choir_wraith"].abilities[0]
    assert (cry.kind, cry.damage) == ("action", "1d4")
    assert monsters["hollow_guard"].abilities[0].kind == "immunity"


def test_wwn_bestiary_attack_counts_are_structured_fields() -> None:
    monsters = _wwn().monsters
    assert monsters["rot_shambler"].attacks_per_action == 1
    assert monsters["ember_hound"].attacks_per_action == 2
    assert monsters["tide_grasper"].attacks_per_action == 4
    phrase = re.compile(r"(\d+)\s+attacks?\s+per\s+action", re.IGNORECASE)
    for monster in monsters.values():
        match = phrase.search(monster.description)
        if match is not None:
            assert monster.attacks_per_action == int(match.group(1)), monster.id


def test_wwn_healer_and_vowed_commit_their_own_effort_pools() -> None:
    progression = require_wwn_progression(_wwn())
    assert progression.tradition_effort_stats["healer"] == "effort_healer"
    assert progression.tradition_effort_stats["vowed"] == "effort_vowed"


def test_wwn_uses_the_source_travel_rules() -> None:
    rules = _wwn()
    assert rules.wilderness is None
    assert "travel" in rules.tool_calls
    assert rules.tool_calls["travel"].world_method == "travel"
    assert rules.journey is not None
    assert rules.journey.model == "terrain_days"
