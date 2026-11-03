"""Structural parity checks for the Worlds Without Number ruleset.

These pin the facts `PARITY.md` and `SPEC.md` rely on. They assert the loaded content and tool
catalog rather than scraping the documents' prose.
"""

from __future__ import annotations

from dataclasses import replace

from cygnusforge.content.library import RuleSet
from cygnusforge.rules.wwn_progression import require_wwn_progression
from cygnusforge.services.events import Character
from cygnusforge.services.inventory import capacity_of, readied_capacity
from tests.conftest import content_library


def _wwn() -> RuleSet:
    return content_library().rule_sets["worldswithoutnumber"]


def test_wwn_tool_catalog_exposes_project_and_hireling_tools() -> None:
    tools = _wwn().tool_calls
    expected = {
        "hire_hireling",
        "dismiss_hireling",
        "start_project",
        "advance_project",
        "abandon_project",
        "list_projects",
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
    assert (rules.capacity_stat, rules.capacity_multiplier) == ("str", 1.5)
    assert (rules.readied_stat, rules.readied_divisor) == ("str", 2)

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


def test_wwn_healer_and_vowed_commit_their_own_effort_pools() -> None:
    progression = require_wwn_progression(_wwn())
    assert progression.tradition_effort_stats["healer"] == "effort_healer"
    assert progression.tradition_effort_stats["vowed"] == "effort_vowed"
