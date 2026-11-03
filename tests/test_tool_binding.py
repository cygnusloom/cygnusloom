"""Tool calls can bind a WorldTools method instead of shipping a Python wrapper."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from cygnusforge.content.definitions import ContentError
from cygnusforge.content.library import ContentLibrary, ToolCall
from cygnusforge.llm.base import ToolRequest
from cygnusforge.rules.context import ToolContext
from cygnusforge.services.tools import ToolRun, run_tool
from tests.conftest import content_library
from tests.test_library_strictness import _core, _ruleset, _write

# Core tools that keep a Python function: the ones that need ToolContext, the one that runs a
# loader hook, and the ones whose schema or signature does not fit the WorldTools method.
KEPT_PYTHON = {
    "create_location",
    "roll_creative",
    "lookup_rule",
    "list_lookups",
    "speak",
    "ooc",
}


def _tool(**overrides: Any) -> list[dict[str, Any]]:
    tool: dict[str, Any] = {
        "type": "tool_call",
        "id": "note",
        "name": "note",
        "world_method": "add_note",
        "description": "Keep a note.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "subject": {"type": "string"},
            },
            "required": ["text"],
        },
    }
    tool.update(overrides)
    return [tool]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A minimal valid content tree, as the loader tests use."""
    _write(
        tmp_path,
        {
            "worlds/testland/world.json": [
                {
                    "type": "world",
                    "id": "testland",
                    "name": "Testland",
                    "description": "A test world.",
                }
            ],
            "rulesets/core/rule_set.json": _core(),
            "rulesets/testset/rule_set.json": _ruleset(),
        },
    )
    return tmp_path


def test_a_world_method_binding_loads(tree: Path):
    _write(tree, {"rulesets/testset/module.json": _tool()})
    tool = ContentLibrary.load(tree).rule_sets["testset"].tool_calls["note"]
    assert tool.world_method == "add_note"
    assert tool.function is None
    assert tool.takes_context is False


def test_an_unknown_world_method_is_rejected_at_load(tree: Path):
    _write(tree, {"rulesets/testset/module.json": _tool(world_method="add_notes")})
    with pytest.raises(ContentError, match="'add_notes' is not a method of WorldTools"):
        ContentLibrary.load(tree)


def test_a_binding_and_a_function_cannot_both_be_given(tree: Path):
    _write(
        tree,
        {
            "rulesets/testset/module.json": _tool(
                python_module="m",
                function="social_roll",
            )
        },
    )
    with pytest.raises(ContentError, match="not both"):
        ContentLibrary.load(tree)


def test_a_tool_needs_a_binding_or_a_function(tree: Path):
    tool = _tool()
    del tool[0]["world_method"]
    _write(tree, {"rulesets/testset/module.json": tool})
    with pytest.raises(ContentError, match="give 'world_method'"):
        ContentLibrary.load(tree)


def test_a_schema_argument_the_method_does_not_take_is_rejected(tree: Path):
    _write(
        tree,
        {
            "rulesets/testset/module.json": _tool(
                parameters={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "mood": {"type": "string"},
                    },
                    "required": ["text"],
                }
            )
        },
    )
    with pytest.raises(ContentError, match="does not accept parameter 'mood'"):
        ContentLibrary.load(tree)


def test_a_method_argument_with_no_default_must_be_required(tree: Path):
    _write(
        tree,
        {
            "rulesets/testset/module.json": _tool(
                world_method="delete_note",
                parameters={
                    "type": "object",
                    "properties": {"note_id": {"type": "integer"}},
                },
            )
        },
    )
    with pytest.raises(ContentError, match="parameter 'note_id' has no default"):
        ContentLibrary.load(tree)


def _run(tool: ToolCall, arguments: dict[str, Any], ctx: ToolContext | None) -> dict[str, Any]:
    request = ToolRequest(id="call_1", name=tool.name, arguments=json.dumps(arguments))
    run = asyncio.run(run_tool({tool.id: tool}, request, None, ctx))
    assert isinstance(run, ToolRun)
    return json.loads(run.output)


def _note_tool() -> ToolCall:
    return ToolCall(
        id="note",
        name="note",
        description="Keep a note.",
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "subject": {"type": "string"},
            },
            "required": ["text"],
        },
        world_method="add_note",
    )


class _World:
    """The one WorldTools method the bound test tool calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def add_note(self, text: str, subject: str | None = None) -> dict[str, Any]:
        self.calls.append((text, subject))
        return {"note_id": 1, "text": text, "subject": subject}


async def _quiet_speak(speaker: str, profile: str, intent: str, replace: bool) -> None:
    return None


def _context(world: _World) -> ToolContext:
    speak: Callable[[str, str, str, bool], Awaitable[None]] = _quiet_speak
    return ToolContext(
        world=world,  # type: ignore[arg-type]
        speak=speak,
        ooc=lambda narrate: None,
        rules={},
        lookup_tools={},
    )


def test_a_bound_tool_calls_the_world_method_with_its_arguments():
    world = _World()
    result = _run(_note_tool(), {"text": "The bridge sways.", "subject": "Spore Bridge"}, _context(world))
    assert result == {"note_id": 1, "text": "The bridge sways.", "subject": "Spore Bridge"}
    assert world.calls == [("The bridge sways.", "Spore Bridge")]


def test_a_bound_tool_needs_a_campaign():
    result = _run(_note_tool(), {"text": "The bridge sways."}, None)
    assert result == {"error": "this tool cannot run without a campaign"}


def test_core_migrates_every_wrapper_that_fits_and_keeps_the_rest():
    calls = content_library().core.tool_calls
    assert len(calls) == 35
    assert {call_id for call_id, call in calls.items() if call.function is None} == (
        set(calls) - KEPT_PYTHON
    )
    for call_id, call in calls.items():
        if call_id in KEPT_PYTHON:
            assert call.function is not None
            assert call.world_method is None
        else:
            assert call.world_method == call_id
            assert call.function is None
            assert call.takes_context is False


def test_core_binding_leaves_the_schemas_unchanged():
    calls = content_library().core.tool_calls
    assert sorted(calls["create_npc"].parameters["properties"]) == [
        "accent",
        "character",
        "description",
        "force",
        "location",
        "name",
        "personality",
        "pronouns",
        "quirks",
    ]
    assert calls["view_journal"].parameters["required"] == ["start_line"]
    assert calls["place_object"].parameters["required"] == [
        "name",
        "place",
        "contents",
        "hidden",
        "cause",
    ]


# Cairn tool calls that keep a Python function: the five whose schema does not fit the
# WorldTools method, the ruleset's own condition logic, and the two spell-item kinds that pick a
# role.
CAIRN_KEPT_PYTHON = {
    "create_encounter_map",
    "create_monster",
    "give_scroll",
    "give_spellbook",
    "player_initiative",
    "player_retreat",
    "player_save",
    "set_condition",
}

CAIRN_TOOLS = {
    "add_participant",
    "add_penalty",
    "advance_time",
    "attack",
    "buy_item",
    "buy_service",
    "cast_spell",
    "create_encounter_map",
    "create_monster",
    "delete_encounter_map",
    "dismiss_hireling",
    "drop_item",
    "dungeon_cycle",
    "end_encounter",
    "give_charged_item",
    "give_item",
    "give_scroll",
    "give_spellbook",
    "harm_character",
    "heal_character",
    "hire_hireling",
    "place_participant",
    "player_attack",
    "player_initiative",
    "player_retreat",
    "player_save",
    "recharge_charged_item",
    "recover_character",
    "remove_participant",
    "resolve_attacks",
    "resolve_group_break",
    "rest",
    "rest_history",
    "retreat",
    "roll_reaction",
    "roll_save",
    "roll_tables",
    "search_marketplace",
    "search_monster",
    "set_armor",
    "set_condition",
    "set_map_cells",
    "stabilize",
    "start_encounter",
    "surrender",
    "update_encounter_map",
    "use_charge",
    "use_item",
    "wilderness_action",
}


def test_cairn_migrates_every_wrapper_that_fits_and_keeps_the_rest():
    library = content_library()
    calls = library.rule_sets["cairn2e"].tool_calls
    assert set(calls) - set(library.core.tool_calls) == CAIRN_TOOLS
    assert {
        call_id for call_id, call in calls.items() if call_id in CAIRN_TOOLS and call.function
    } == CAIRN_KEPT_PYTHON
    for call_id in CAIRN_TOOLS:
        call = calls[call_id]
        if call_id in CAIRN_KEPT_PYTHON:
            assert call.world_method is None
        else:
            assert call.world_method is not None
            assert call.function is None
            assert call.takes_context is False
    # The one tool whose id and method name differ.
    assert calls["set_map_cells"].world_method == "set_encounter_map_cells"
