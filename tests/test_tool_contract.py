"""Semantic contracts for the tool catalog.

These tests describe invariants every rule set must satisfy. They deliberately do not pin a
tool count or a full literal catalog, so a rule set can inherit, add, override, or disable tools
without making unrelated tests fail.
"""

from __future__ import annotations

import inspect
import json

import pytest

from cygnusloom.content.library import DATA_DIR, ContentLibrary
from cygnusloom.services.tools import tool_specs
from cygnusloom.services.world import WorldTools
from tests.conftest import bundled_library
from tests.support import content_tree

RULE_SETS = sorted(bundled_library().rule_sets)

# Universal game-loop tools that core owns so no rule set repeats their schema or binding. A
# rule set that does not offer one names it in a `disabled` stub, which keeps the loaded catalog
# exactly as it was before consolidation.
CONSOLIDATED_TOOLS = {
    "advance_time": frozenset(),
    "rest": frozenset(),
    "rest_history": frozenset({"worldswithoutnumber"}),
    "roll_tables": frozenset({"academia-arcana"}),
    "give_item": frozenset({"academia-arcana"}),
    "drop_item": frozenset({"academia-arcana"}),
    "use_item": frozenset({"worldswithoutnumber", "academia-arcana"}),
    "buy_item": frozenset(),
    "buy_service": frozenset(),
    "search_marketplace": frozenset(),
    "hire_hireling": frozenset({"academia-arcana"}),
    "dismiss_hireling": frozenset({"academia-arcana"}),
    "search_monster": frozenset({"academia-arcana"}),
}

# Universal tools should be inherited, not redefined. Keep this empty unless a genuine full
# override is unavoidable; an `extends` definition is preferred and does not need an entry.
FULL_TOOL_OVERRIDES: set[tuple[str, str]] = set()


def _tool_definitions(rule_set_id: str) -> list[dict[str, object]]:
    """Every raw tool_call definition in a bundled rule set's JSON files."""
    directory = DATA_DIR / "rulesets" / rule_set_id
    found: list[dict[str, object]] = []
    for path in sorted(directory.rglob("*.json")):
        for definition in json.loads(path.read_text()):
            if isinstance(definition, dict) and definition.get("type") == "tool_call":
                found.append(definition)
    return found


def test_core_owns_full_universal_tool_definitions() -> None:
    core = bundled_library().core.tool_calls
    for tool_id in CONSOLIDATED_TOOLS:
        assert tool_id in core, tool_id
        call = core[tool_id]
        assert call.function is None
        assert call.world_method is not None
        assert call.world_method == tool_id
        # A universal tool must pass the gate core's own capabilities satisfy.
        assert call.requires == ()
        assert call.enabled_by == {}

    raw = {str(definition.get("id")): definition for definition in _tool_definitions("core")}
    for tool_id in CONSOLIDATED_TOOLS:
        assert "extends" not in raw[tool_id]
        assert not raw[tool_id].get("disabled")


def test_every_bundled_ruleset_inherits_or_opts_out_of_universal_tools() -> None:
    library = bundled_library()
    for rule_set_id, rule_set in library.rule_sets.items():
        for tool_id, opted_out in CONSOLIDATED_TOOLS.items():
            if rule_set_id in opted_out:
                assert tool_id not in rule_set.tool_calls, (rule_set_id, tool_id)
            else:
                assert tool_id in rule_set.tool_calls, (rule_set_id, tool_id)
                assert rule_set.tool_calls[tool_id].name == tool_id


def test_rulesets_do_not_redefine_universal_tools() -> None:
    for rule_set_id in RULE_SETS:
        for definition in _tool_definitions(rule_set_id):
            tool_id = str(definition.get("id"))
            if tool_id not in CONSOLIDATED_TOOLS:
                continue
            if definition.get("disabled"):
                assert rule_set_id in CONSOLIDATED_TOOLS[tool_id], (rule_set_id, tool_id)
                continue
            if "extends" in definition:
                continue
            assert (rule_set_id, tool_id) in FULL_TOOL_OVERRIDES, (rule_set_id, tool_id)


def test_rest_length_choices_come_from_each_rulesets_rest_data() -> None:
    for rule_set_id, rule_set in bundled_library().rule_sets.items():
        call = rule_set.tool_calls.get("rest")
        if call is None:
            continue
        length = call.parameters["properties"]["length"]
        assert length["enum"] == list(rule_set.time.rests), rule_set_id


def test_item_size_choices_come_from_each_rulesets_item_data() -> None:
    for rule_set_id, rule_set in bundled_library().rule_sets.items():
        for tool_id in ("buy_item", "give_item"):
            call = rule_set.tool_calls.get(tool_id)
            if call is None:
                continue
            size = call.parameters["properties"]["size"]
            assert size["enum"] == list(rule_set.item_sizes), (rule_set_id, tool_id)


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
        c.name for c in calls.values() if not c.journal_step and c.name not in {"speak", "ooc"}
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
    library = ContentLibrary.load(content_tree(tmp_path, core=[NOTE_TOOL], definitions=[override]))
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
