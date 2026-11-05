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

# Core tools that keep a Python function: the ones that need ToolContext and the ones that
# check a rule or read the lookups. Every other core tool binds a WorldTools method.
KEPT_PYTHON = {
    "create_location",
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


def _context(world: Any) -> ToolContext:
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
    result = _run(
        _note_tool(), {"text": "The bridge sways.", "subject": "Spore Bridge"}, _context(world)
    )
    assert result == {"note_id": 1, "text": "The bridge sways.", "subject": "Spore Bridge"}
    assert world.calls == [("The bridge sways.", "Spore Bridge")]


def test_a_bound_tool_needs_a_campaign():
    result = _run(_note_tool(), {"text": "The bridge sways."}, None)
    assert result == {"error": "this tool cannot run without a campaign"}


def test_core_migrates_every_wrapper_that_fits_and_keeps_the_rest():
    calls = content_library().core.tool_calls
    assert len(calls) == 43
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


# Cairn tool calls that keep a Python function: only the ruleset's own condition logic. Every
# other Cairn tool binds a WorldTools method, with `bind` for fixed arguments.
CAIRN_KEPT_PYTHON = {
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
    assert set(calls) - set(library.core.tool_calls) == CAIRN_TOOLS - {"set_map_cells"}
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


# WWN tools that used to wrap a method to fix or rename an argument now bind it directly.
WWN_BOUND = {"create_encounter_map", "roll_check", "roll_save", "set_ac"}


def test_wwn_binds_the_tools_it_used_to_wrap():
    calls = content_library().rule_sets["worldswithoutnumber"].tool_calls
    for call_id in WWN_BOUND:
        assert calls[call_id].world_method is not None
        assert calls[call_id].function is None
    assert calls["roll_check"].bind == {"check": "skill"}
    assert calls["set_ac"].rename == {"ac": "armor"}
    assert calls["roll_save"].rename == {
        "character": "name",
        "save": "stat",
        "cause": "reason",
    }


def test_a_fixed_argument_loads_onto_the_tool(tree: Path):
    _write(
        tree,
        {
            "rulesets/testset/module.json": _tool(
                bind={"subject": "Spire"},
                parameters={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            )
        },
    )
    tool = ContentLibrary.load(tree).rule_sets["testset"].tool_calls["note"]
    assert tool.bind == {"subject": "Spire"}
    assert tool.rename == {}


def test_a_rename_loads_onto_the_tool(tree: Path):
    _write(
        tree,
        {
            "rulesets/testset/module.json": _tool(
                world_method="set_armor",
                rename={"ac": "armor"},
                parameters={
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "ac": {"type": "integer"}},
                    "required": ["name", "ac"],
                },
            )
        },
    )
    tool = ContentLibrary.load(tree).rule_sets["testset"].tool_calls["note"]
    assert tool.rename == {"ac": "armor"}
    assert tool.bind == {}


def test_a_fixed_argument_the_method_does_not_take_is_rejected(tree: Path):
    _write(tree, {"rulesets/testset/module.json": _tool(bind={"body": "text"})})
    with pytest.raises(ContentError, match="'bind' names 'body'"):
        ContentLibrary.load(tree)


def test_a_fixed_argument_cannot_also_be_a_schema_argument(tree: Path):
    _write(tree, {"rulesets/testset/module.json": _tool(bind={"text": "fixed"})})
    with pytest.raises(ContentError, match="'bind' target 'text' is also a schema argument"):
        ContentLibrary.load(tree)


def test_an_object_binding_must_name_schema_arguments(tree: Path):
    _write(
        tree,
        {
            "rulesets/testset/module.json": _tool(
                world_method="set_armor",
                bind={"armor": {"note": "body"}},
                parameters={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
            )
        },
    )
    with pytest.raises(ContentError, match="'bind' 'armor' names 'body'"):
        ContentLibrary.load(tree)


def test_a_rename_must_name_a_schema_argument(tree: Path):
    _write(tree, {"rulesets/testset/module.json": _tool(rename={"body": "text"})})
    with pytest.raises(ContentError, match="'rename' names 'body'"):
        ContentLibrary.load(tree)


def test_a_rename_target_must_be_a_method_parameter(tree: Path):
    _write(tree, {"rulesets/testset/module.json": _tool(rename={"text": "body"})})
    with pytest.raises(ContentError, match="'rename' target 'body' is not a parameter"):
        ContentLibrary.load(tree)


def test_a_rename_target_cannot_also_be_a_schema_argument(tree: Path):
    _write(tree, {"rulesets/testset/module.json": _tool(rename={"text": "subject"})})
    with pytest.raises(ContentError, match="'rename' target 'subject' is also a schema argument"):
        ContentLibrary.load(tree)


def test_bindings_need_a_world_method(tree: Path):
    _write(
        tree,
        {
            "rulesets/testset/module.json": [
                {"type": "python_module", "id": "m", "module": "tests.rolls"},
                {
                    "type": "tool_call",
                    "id": "social",
                    "name": "social",
                    "python_module": "m",
                    "function": "social_roll",
                    "description": "Talk it out.",
                    "bind": {"subject": "Spire"},
                    "parameters": {"type": "object", "properties": {}},
                },
            ]
        },
    )
    with pytest.raises(ContentError, match="'bind' and 'rename' need 'world_method'"):
        ContentLibrary.load(tree)


class _ArmorWorld:
    """The one WorldTools method the bound set_ac tool calls, under its own argument name."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def set_armor(self, name: str, armor: int) -> dict[str, Any]:
        self.calls.append((name, armor))
        return {"character": name, "armor": armor}


def test_a_bound_tool_renames_an_argument_for_the_method():
    tool = content_library().rule_sets["worldswithoutnumber"].tool_calls["set_ac"]
    world = _ArmorWorld()
    result = _run(tool, {"name": "Bo", "ac": 15}, _context(world))
    assert result == {"character": "Bo", "armor": 15}
    assert world.calls == [("Bo", 15)]


class _SpellWorld:
    """The one WorldTools method the bound give_spellbook tool calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def give_spell_item(
        self, name: str, spell: str, description: str, *, role: str
    ) -> dict[str, Any]:
        self.calls.append((name, spell, role))
        return {"name": name, "spell": spell, "role": role, "description": description}


def test_a_bound_tool_fixes_an_argument_the_gm_never_gives():
    tool = content_library().rule_sets["cairn2e"].tool_calls["give_spellbook"]
    world = _SpellWorld()
    result = _run(
        tool,
        {"name": "Bo", "spell": "Read Omens", "description": "A spellbook."},
        _context(world),
    )
    assert result["role"] == "reusable"
    assert world.calls == [("Bo", "Read Omens", "reusable")]


class _MonsterWorld:
    """The one WorldTools method the bound create_monster tool calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create_monster(
        self,
        name: str,
        location: str,
        description: str | None = None,
        template: str | None = None,
        stats: dict[str, int | None] | None = None,
        armor: int | None = None,
        attacks: list[dict[str, Any]] | None = None,
        force: bool = False,
        detachment: bool | None = None,
        rules: list[str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"name": name, "location": location, "stats": stats})
        return {"name": name, "stats": stats}


def test_a_bound_tool_builds_an_object_from_flat_arguments():
    tool = content_library().rule_sets["cairn2e"].tool_calls["create_monster"]
    world = _MonsterWorld()
    _run(
        tool,
        {"name": "Wolf", "location": "Ford", "hit_points": 3, "strength": 6},
        _context(world),
    )
    assert world.calls == [
        {
            "name": "Wolf",
            "location": "Ford",
            "stats": {"hp": 3, "str": 6, "dex": None, "wil": None},
        }
    ]
