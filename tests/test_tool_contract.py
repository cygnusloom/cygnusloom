"""Semantic contracts for the tool catalog.

These tests describe invariants every rule set must satisfy. They deliberately do not pin a
tool count or a full literal catalog, so a rule set can inherit, add, override, or disable tools
without making unrelated tests fail.
"""

from __future__ import annotations

import inspect

import pytest

from cygnusloom.content.library import ContentLibrary
from cygnusloom.services.tools import tool_specs
from cygnusloom.services.world import WorldTools
from tests.conftest import bundled_library
from tests.support import content_tree

RULE_SETS = sorted(bundled_library().rule_sets)


@pytest.mark.parametrize("rule_set_id", RULE_SETS)
def test_every_tool_has_one_resolvable_handler(rule_set_id: str) -> None:
    calls = bundled_library().rule_sets[rule_set_id].tool_calls
    names: set[str] = set()
    for call in calls.values():
        assert (call.function is None) != (call.world_method is None), call.id
        assert call.name not in names, call.name
        names.add(call.name)

        schema = call.parameters
        assert schema["type"] == "object"
        assert set(schema.get("required", [])) <= set(schema["properties"])

        if call.world_method is not None:
            method = getattr(WorldTools, call.world_method)
            assert callable(method), call.world_method
            params = inspect.signature(method).parameters
            assert set(call.bind) <= set(params), call.id
            assert set(call.rename.values()) <= set(params), call.id
            assert call.takes_context is False

        if call.function is not None:
            params = inspect.signature(call.function).parameters
            assert call.world_method is None
            assert call.takes_context == ("ctx" in params)


@pytest.mark.parametrize("rule_set_id", RULE_SETS)
def test_tool_specs_match_the_enabled_catalog_for_each_step(rule_set_id: str) -> None:
    calls = bundled_library().rule_sets[rule_set_id].tool_calls

    def offered(**step: bool) -> set[str]:
        return {spec["function"]["name"] for spec in tool_specs(calls, **step) or []}

    regular = {c.name for c in calls.values() if not c.journal_step and not c.review_step}
    journal = {c.name for c in calls.values() if c.journal_step}
    review = {
        c.name
        for c in calls.values()
        if not c.journal_step and c.name not in {"speak", "ooc"}
    }

    assert offered() == regular
    assert offered(journal_step=True) == journal
    assert offered(review_step=True) == review


NOTE_TOOL = {
    "type": "tool_call",
    "id": "note",
    "name": "note",
    "world_method": "add_note",
    "description": "Keep a note.",
    "parameters": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
    "requires": ["social"],
}


def test_capabilities_gate_inherited_tools_without_changing_core(tmp_path) -> None:
    # No capabilities block: inherited tools stay ungated for compatibility.
    plain = ContentLibrary.load(content_tree(tmp_path, core=[NOTE_TOOL]))
    assert "note" in plain.rule_sets["testset"].tool_calls

    # A declared block is authoritative: the inherited tool is dropped.
    gated_tree = content_tree(
        tmp_path / "gated",
        core=[NOTE_TOOL],
        rule_set={"capabilities": {"social": []}},
    )
    gated = ContentLibrary.load(gated_tree)
    assert "note" not in gated.rule_sets["testset"].tool_calls
    assert "note" in gated.core.tool_calls


def test_a_ruleset_can_override_a_core_tool_by_id(tmp_path) -> None:
    override = {
        **NOTE_TOOL,
        "description": "Keep a ruleset-specific note.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "subject": {"type": "string"},
            },
            "required": ["text"],
        },
    }
    library = ContentLibrary.load(
        content_tree(tmp_path, core=[NOTE_TOOL], definitions=[override])
    )
    calls = library.rule_sets["testset"].tool_calls
    assert calls["note"].description == "Keep a ruleset-specific note."
    assert set(calls["note"].parameters["properties"]) == {"text", "subject"}
    assert sum(call.name == "note" for call in calls.values()) == 1


def test_a_ruleset_can_disable_an_inherited_core_tool(tmp_path) -> None:
    library = ContentLibrary.load(
        content_tree(
            tmp_path,
            core=[NOTE_TOOL],
            definitions=[{"type": "tool_call", "id": "note", "disabled": True}],
        )
    )
    assert "note" not in library.rule_sets["testset"].tool_calls
    assert "note" in library.core.tool_calls
