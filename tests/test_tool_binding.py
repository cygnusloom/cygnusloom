"""Tool calls can bind a WorldTools method instead of shipping a Python wrapper.

The loader supplies quiet defaults: the name is the tool id, the bound WorldTools method
is the id when no Python handler is named, an omitted parameter schema is empty, and a root
parameter schema with no type is an object."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from cygnusloom.content.definitions import ContentError
from cygnusloom.content.library import ContentLibrary, ToolCall
from cygnusloom.llm.base import ToolRequest
from cygnusloom.rules.context import ToolContext
from cygnusloom.services.tools import ToolRun, run_tool
from cygnusloom.services.world import WorldTools
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


def test_a_tool_name_defaults_to_its_id(tree: Path):
    tool = _tool()
    del tool[0]["name"]
    _write(tree, {"rulesets/testset/module.json": tool})
    loaded = ContentLibrary.load(tree).rule_sets["testset"].tool_calls["note"]
    assert loaded.name == "note"


def test_the_default_tool_name_is_validated_like_any_other(tree: Path):
    tool = _tool(id="not a name")
    del tool[0]["name"]
    _write(tree, {"rulesets/testset/module.json": tool})
    with pytest.raises(ContentError, match="'name' must be 1 to 64"):
        ContentLibrary.load(tree)


def test_a_tool_world_method_defaults_to_its_id(tree: Path):
    tool = _tool(id="add_note")
    del tool[0]["name"]
    del tool[0]["world_method"]
    _write(tree, {"rulesets/testset/module.json": tool})
    loaded = ContentLibrary.load(tree).rule_sets["testset"].tool_calls["add_note"]
    assert loaded.world_method == "add_note"
    assert loaded.function is None
    assert loaded.takes_context is False


def test_a_tool_with_no_arguments_may_omit_its_parameters(tree: Path):
    _write(
        tree,
        {
            "rulesets/testset/module.json": [
                {"type": "tool_call", "id": "list_factions", "description": "List factions."}
            ]
        },
    )
    loaded = ContentLibrary.load(tree).rule_sets["testset"].tool_calls["list_factions"]
    assert loaded.parameters == {"type": "object", "properties": {}}


def test_a_schema_may_omit_required(tree: Path):
    _write(
        tree,
        {
            "rulesets/testset/module.json": _tool(
                world_method="view_journal",
                parameters={"type": "object", "properties": {"start_line": {"type": "integer"}}},
            )
        },
    )
    loaded = ContentLibrary.load(tree).rule_sets["testset"].tool_calls["note"]
    assert "required" not in loaded.parameters


def test_a_schema_may_omit_its_type(tree: Path):
    _write(
        tree,
        {
            "rulesets/testset/module.json": _tool(
                world_method="view_journal",
                parameters={"properties": {"start_line": {"type": "integer"}}},
            )
        },
    )
    loaded = ContentLibrary.load(tree).rule_sets["testset"].tool_calls["note"]
    assert loaded.parameters["type"] == "object"
    assert loaded.parameters["properties"] == {"start_line": {"type": "integer"}}


def test_a_tool_whose_id_is_not_a_method_needs_a_binding_or_a_function(tree: Path):
    tool = _tool(id="keep_note")
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


def test_core_tools_keep_one_resolvable_handler():
    calls = content_library().core.tool_calls
    assert KEPT_PYTHON <= set(calls)
    for call_id, call in calls.items():
        # Handler XOR: a tool is either a Python function or a WorldTools method binding.
        assert (call.function is None) != (call.world_method is None), call_id
        if call.world_method is not None:
            assert callable(getattr(WorldTools, call.world_method))
            assert call.takes_context is False
        if call_id in KEPT_PYTHON:
            assert call.function is not None
            assert call.world_method is None


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


def test_cairn_tools_keep_one_resolvable_handler():
    library = content_library()
    calls = library.rule_sets["cairn2e"].tool_calls
    assert CAIRN_KEPT_PYTHON <= set(calls)
    python_tools = {call_id for call_id, call in calls.items() if call.function is not None}
    assert python_tools - KEPT_PYTHON == CAIRN_KEPT_PYTHON
    for call_id, call in calls.items():
        assert (call.function is None) != (call.world_method is None), call_id
        if call_id not in CAIRN_KEPT_PYTHON | KEPT_PYTHON:
            assert call.world_method is not None
            assert call.function is None
            assert call.takes_context is False
    # Cairn inherits the core map tool, which handles both local and encounter maps.
    assert calls["set_map_cells"].world_method == "set_map_cells"


# WWN tools whose ids or argument names need an explicit binding.
WWN_BOUND = {
    "add_strain",
    "create_encounter_map",
    "heal_strain",
    "player_attack",
    "player_roll_save",
    "rest",
    "roll_check",
    "roll_save",
    "roll_starting_silver",
    "set_ac",
}


def test_wwn_binds_the_tools_it_used_to_wrap():
    calls = content_library().rule_sets["worldswithoutnumber"].tool_calls
    assert WWN_BOUND <= set(calls)
    for call_id in WWN_BOUND:
        assert calls[call_id].world_method is not None
        assert calls[call_id].function is None
    assert calls["roll_check"].bind == {"check": "skill"}
    assert calls["set_ac"].rename == {"ac": "armor"}
    # roll_save now uses the shared canonical argument names, so it needs no mapping.
    assert calls["roll_save"].world_method == "roll_save"
    assert calls["roll_save"].rename == {}
    assert calls["heal_strain"].world_method == "restore_stat"
    assert calls["heal_strain"].bind == {"stat": "strain"}
    assert calls["add_strain"].world_method == "harm_stat"
    assert calls["add_strain"].bind == {"stat": "strain"}
    assert calls["player_attack"].player_input.param == "attack_roll"
    assert calls["player_roll_save"].player_input.param == "natural"
    assert calls["roll_starting_silver"].world_method == "roll_currency"
    assert calls["roll_starting_silver"].bind == {
        "dice": "3d6",
        "multiplier": 10,
        "cause": "starting silver",
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
