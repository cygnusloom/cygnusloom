from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from cygnusforge.content.definitions import ContentError, read_definitions
from cygnusforge.content.library import ContentLibrary, PlayerInput
from cygnusforge.rules.freeform import SocialRoll, social_roll


def _write(root: Path, files: dict[str, Any]) -> None:
    for rel, payload in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        path.write_text(text, encoding="utf-8")


def _world(name: str = "Testland", ident: str = "testland") -> list[dict[str, str]]:
    return [
        {
            "type": "world",
            "id": ident,
            "name": name,
            "description": "A test world.",
        },
        {"type": "location", "id": "town", "name": "Town", "description": "A small town."},
    ]


def _social_tool(**overrides: Any) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "type": "tool_call",
        "id": "social",
        "name": "social",
        "python_module": "m",
        "function": "social_roll",
        "description": "Talk it out.",
        "parameters": {
            "type": "object",
            "properties": {
                "relationship": {"type": "integer"},
                "circumstance": {"type": "integer"},
            },
            "required": ["relationship", "circumstance"],
        },
    }
    tool.update(overrides)
    return tool


def _module(**tool_overrides: Any) -> list[dict[str, Any]]:
    return [
        {"type": "python_module", "id": "m", "module": "cygnusforge.rules.freeform"},
        _social_tool(**tool_overrides),
    ]


def _rule_set(ident: str = "testset") -> list[dict[str, str]]:
    return [{"type": "rule_set", "id": ident, "name": "Test set", "description": "For tests."}]


CORE_TOOLS = {
    "create_location",
    "create_npc",
    "update_location",
    "delete_location",
    "update_npc",
    "delete_npc",
    "move_actor",
    "add_note",
    "update_note",
    "delete_note",
    "search_notes",
    "speak",
}


def _core() -> list[dict[str, Any]]:
    tables = [{"type": "table", "id": name, "options": ["one"]} for name in NPC_TABLES]
    return [*_rule_set("core"), *tables]


NPC_TABLES = ("personality", "accent", "quirk")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A minimal valid content tree. Tests break one piece at a time."""
    _write(
        tmp_path,
        {
            "worlds/testland/world.json": _world(),
            "rulesets/core/rule_set.json": _core(),
            "rulesets/testset/rule_set.json": _rule_set(),
        },
    )
    return tmp_path


def test_bundled_content_loads():
    library = ContentLibrary.load()
    assert set(library.world_templates) == {"gnomeforest", "kilharbor"}
    assert library.world_templates["gnomeforest"].name == "Gnome Forest"
    assert library.world_templates["kilharbor"].name == "Kilharbor"
    kilharbor = library.world_templates["kilharbor"]
    assert kilharbor.locations == ()
    assert kilharbor.npcs == ()
    freeform = library.rule_sets["freeform"]
    assert freeform.name == "Freeform"
    assert "consistency" in freeform.rules
    assert set(freeform.tool_calls) == CORE_TOOLS | {"social_roll"}


def test_bundled_social_roll_is_a_tool_call_backed_by_python():
    tool = ContentLibrary.load().rule_sets["freeform"].tool_calls["social_roll"]
    assert tool.name == "social_roll"
    assert tool.function is social_roll
    assert set(tool.parameters["properties"]) == {"prompt", "relationship", "circumstance"}
    assert tool.parameters["required"] == ["prompt", "relationship", "circumstance"]
    assert tool.player_input is not None
    assert (tool.player_input.dice, tool.player_input.param) == ("1d20", "roll")
    assert "{relationship:+d}" in (tool.player_input.details or "")
    assert tool.activity is not None


def test_social_roll_is_one_d20_plus_modifiers():
    for seed in range(50):
        result = social_roll(2, -1, rng=random.Random(seed))
        assert isinstance(result, SocialRoll)
        assert 1 <= result.die <= 20
        assert result.relationship == 2
        assert result.circumstance == -1
        assert result.total == result.die + 1


def test_every_json_file_in_a_directory_is_read(tmp_path: Path):
    _write(
        tmp_path,
        {
            "b.json": [{"type": "npc", "id": "bo", "name": "Bo"}],
            "nested/a.json": [{"type": "npc", "id": "al"}, {"type": "location", "id": "glade"}],
            "notes.txt": "ignored",
        },
    )
    found = [(d.type, d.id) for d in read_definitions(tmp_path)]
    assert found == [("npc", "bo"), ("npc", "al"), ("location", "glade")]


def test_library_loads_from_a_custom_directory(tree: Path):
    library = ContentLibrary.load(tree)
    assert library.world_templates["testland"].description == "A test world."
    assert library.rule_sets["testset"].rules == {}
    assert library.rule_sets["testset"].tool_calls == {}


def test_rules_and_tool_calls_are_read_from_their_definitions(tree: Path):
    _write(
        tree,
        {
            "rulesets/testset/rules.json": [{"type": "rule", "id": "r1", "text": "No magic."}],
            "rulesets/testset/module.json": _module(),
        },
    )
    rule_set = ContentLibrary.load(tree).rule_sets["testset"]
    assert rule_set.rules == {"r1": "No magic."}
    tool = rule_set.tool_calls["social"]
    assert tool.name == "social"
    assert tool.description == "Talk it out."
    assert tool.function is social_roll


ROLLED_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string"},
        "relationship": {"type": "integer"},
        "circumstance": {"type": "integer"},
    },
    "required": ["prompt", "relationship", "circumstance"],
}
ROLLED: dict[str, Any] = {
    "parameters": ROLLED_PARAMETERS,
    "player_input": {
        "dice": "1d20",
        "param": "roll",
        "details": "Modifiers: {relationship:+d} for {circumstance:+d}.",
    },
    "activity": "You rolled {die} for {total}.",
}


def _load_tool(tree: Path, **overrides: Any):
    _write(tree, {"rulesets/testset/module.json": _module(**{**ROLLED, **overrides})})
    return ContentLibrary.load(tree).rule_sets["testset"].tool_calls["social"]


def test_player_input_and_activity_are_read(tree: Path):
    tool = _load_tool(tree)
    assert tool.player_input == PlayerInput(
        dice="1d20", param="roll", details="Modifiers: {relationship:+d} for {circumstance:+d}."
    )
    assert tool.activity == "You rolled {die} for {total}."


@pytest.mark.parametrize(
    "overrides, message",
    [
        (
            {"player_input": {"dice": "d20", "param": "roll"}},
            "'player_input.dice' must be a formula",
        ),
        ({"player_input": {"dice": "1d20", "param": ""}}, "'player_input.param' must name"),
        ({"player_input": {"dice": "1d20", "param": "nope"}}, "does not accept parameter 'nope'"),
        (
            {"player_input": {"dice": "1d20", "param": "relationship"}},
            "must not be a schema parameter",
        ),
        ({"player_input": "1d20"}, "'player_input' must be an object"),
        ({"activity": "You rolled {total"}, "not a valid template"),
        (
            {"player_input": {"dice": "1d20", "param": "roll", "details": "Mod {relationship"}},
            "'player_input.details' is not a valid template",
        ),
        (
            {"player_input": {"dice": "1d20", "param": "roll", "details": "  "}},
            "'player_input.details' must be a non-empty text template",
        ),
        (
            {"player_input": {"dice": "1d20", "param": "roll", "details": "{nope}"}},
            "names unknown argument 'nope'",
        ),
        ({"activity": "   "}, "non-empty text template"),
        (
            {
                "parameters": {
                    **ROLLED_PARAMETERS,
                    "required": ["relationship", "circumstance"],
                }
            },
            "'prompt' must be required",
        ),
        (
            {
                "parameters": {
                    "type": "object",
                    "properties": {"relationship": {"type": "integer"}},
                    "required": ["relationship"],
                }
            },
            "needs a string 'prompt' parameter",
        ),
    ],
)
def test_bad_player_input_or_activity_is_rejected(
    tree: Path, overrides: dict[str, Any], message: str
):
    with pytest.raises(ContentError, match=message):
        _load_tool(tree, **overrides)


@pytest.mark.parametrize(
    "contents, message",
    [
        ("{not json", "a.json"),
        ('{"type": "npc", "id": "x"}', "top level must be a list"),
        ('["just a string"]', "must be an object"),
        ('[{"id": "x"}]', "'type' must be a non-empty string"),
        ('[{"type": "npc", "id": "  "}]', "'id' must be a non-empty string"),
    ],
)
def test_malformed_definitions_are_rejected(tmp_path: Path, contents: str, message: str):
    _write(tmp_path, {"a.json": contents})
    with pytest.raises(ContentError, match=message):
        read_definitions(tmp_path)


def test_duplicate_type_and_id_across_files_is_rejected(tmp_path: Path):
    _write(
        tmp_path,
        {
            "a.json": [{"type": "npc", "id": "bo"}],
            "b.json": [{"type": "npc", "id": "bo"}],
        },
    )
    with pytest.raises(ContentError, match="duplicate npc id 'bo'"):
        read_definitions(tmp_path)


def test_same_id_under_different_types_is_allowed(tmp_path: Path):
    _write(tmp_path, {"a.json": [{"type": "npc", "id": "bo"}, {"type": "item", "id": "bo"}]})
    assert len(read_definitions(tmp_path)) == 2


def test_directory_without_json_is_rejected(tmp_path: Path):
    _write(tmp_path, {"readme.txt": "nothing here"})
    with pytest.raises(ContentError, match="no JSON files"):
        read_definitions(tmp_path)


def test_missing_content_directory_is_rejected(tmp_path: Path):
    with pytest.raises(ContentError, match="missing content directory"):
        ContentLibrary.load(tmp_path)


def test_world_id_must_match_its_directory(tree: Path):
    _write(tree, {"worlds/testland/world.json": _world(ident="elsewhere")})
    with pytest.raises(ContentError, match="must match the directory name"):
        ContentLibrary.load(tree)


def test_world_needs_exactly_one_world_object(tree: Path):
    _write(tree, {"worlds/testland/world.json": [{"type": "npc", "id": "bo"}]})
    with pytest.raises(ContentError, match="expected exactly one 'world'"):
        ContentLibrary.load(tree)


def test_world_needs_a_name(tree: Path):
    _write(
        tree,
        {
            "worlds/testland/world.json": [
                {"type": "world", "id": "testland"},
                {"type": "location", "id": "town", "name": "Town", "description": "Here."},
            ]
        },
    )
    with pytest.raises(ContentError, match="'name' must be a non-empty string"):
        ContentLibrary.load(tree)


def test_rule_set_id_must_match_its_directory(tree: Path):
    _write(tree, {"rulesets/testset/rule_set.json": _rule_set(ident="other")})
    with pytest.raises(ContentError, match="must match the directory name"):
        ContentLibrary.load(tree)


def test_rule_set_needs_exactly_one_rule_set_object(tree: Path):
    _write(tree, {"rulesets/testset/rule_set.json": []})
    with pytest.raises(ContentError, match="expected exactly one 'rule_set'"):
        ContentLibrary.load(tree)


def test_python_module_must_import(tree: Path):
    _write(
        tree,
        {
            "rulesets/testset/module.json": [
                {"type": "python_module", "id": "m", "module": "cygnusforge.rules.nope"}
            ]
        },
    )
    with pytest.raises(ContentError, match="cannot import 'cygnusforge.rules.nope'"):
        ContentLibrary.load(tree)


def test_tool_call_must_name_a_declared_python_module(tree: Path):
    _write(tree, {"rulesets/testset/module.json": [_social_tool(python_module="missing")]})
    with pytest.raises(ContentError, match="unknown python_module 'missing'"):
        ContentLibrary.load(tree)


def test_tool_call_function_must_exist_in_its_module(tree: Path):
    _write(tree, {"rulesets/testset/module.json": _module(function="no_such_function")})
    with pytest.raises(ContentError, match="'no_such_function' is not a function"):
        ContentLibrary.load(tree)


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"name": "has spaces"}, "'name' must be 1 to 64"),
        ({"name": "x" * 65}, "'name' must be 1 to 64"),
        ({"description": ""}, "'description' must be a non-empty string"),
        ({"parameters": None}, "must be an object schema"),
        ({"parameters": {"type": "array"}}, "must be an object schema"),
        ({"parameters": {"type": "object"}}, "needs a 'properties' object"),
        (
            {"parameters": {"type": "object", "properties": {}, "required": ["ghost"]}},
            "'required' must list names from 'properties'",
        ),
    ],
)
def test_tool_call_definition_is_checked(tree: Path, overrides: dict[str, Any], message: str):
    _write(tree, {"rulesets/testset/module.json": _module(**overrides)})
    with pytest.raises(ContentError, match=message):
        ContentLibrary.load(tree)


def test_tool_names_must_be_unique_within_a_rule_set(tree: Path):
    _write(
        tree,
        {
            "rulesets/testset/module.json": [
                *_module(),
                _social_tool(id="social_again"),
            ]
        },
    )
    with pytest.raises(ContentError, match="duplicate tool name 'social'"):
        ContentLibrary.load(tree)


def test_schema_property_must_be_accepted_by_the_function(tree: Path):
    schema = {
        "type": "object",
        "properties": {"relationship": {"type": "integer"}, "mood": {"type": "integer"}},
        "required": ["relationship"],
    }
    _write(tree, {"rulesets/testset/module.json": _module(parameters=schema)})
    with pytest.raises(ContentError, match="does not accept parameter 'mood'"):
        ContentLibrary.load(tree)


def test_function_parameter_without_default_must_be_in_the_schema(tree: Path):
    schema = {
        "type": "object",
        "properties": {"relationship": {"type": "integer"}},
        "required": ["relationship"],
    }
    _write(tree, {"rulesets/testset/module.json": _module(parameters=schema)})
    with pytest.raises(ContentError, match="'circumstance' has no default"):
        ContentLibrary.load(tree)


def test_function_parameter_with_default_may_be_left_out_of_the_schema(tree: Path):
    schema = {
        "type": "object",
        "properties": {
            "relationship": {"type": "integer"},
            "circumstance": {"type": "integer"},
        },
        "required": ["relationship", "circumstance"],
    }
    _write(tree, {"rulesets/testset/module.json": _module(parameters=schema)})
    assert "social" in ContentLibrary.load(tree).rule_sets["testset"].tool_calls


def test_roll_type_is_no_longer_read(tree: Path):
    _write(
        tree,
        {
            "rulesets/testset/module.json": [
                {"type": "python_module", "id": "m", "module": "cygnusforge.rules.freeform"},
                {"type": "roll", "id": "social", "python_module": "m", "function": "social_roll"},
            ]
        },
    )
    assert ContentLibrary.load(tree).rule_sets["testset"].tool_calls == {}


def test_core_rules_and_tables_reach_every_rule_set(tree: Path):
    _write(tree, {"rulesets/core/rules.json": [{"type": "rule", "id": "speech", "text": "Core."}]})
    library = ContentLibrary.load(tree)
    rule_set = library.rule_sets["testset"]
    assert "testset" in library.rule_sets and "core" not in library.rule_sets
    assert rule_set.rules == {"speech": "Core."}
    assert set(rule_set.tables) == set(NPC_TABLES)


def test_rule_set_overrides_core_rules_and_tables(tree: Path):
    _write(
        tree,
        {
            "rulesets/core/rules.json": [{"type": "rule", "id": "speech", "text": "Core."}],
            "rulesets/testset/rules.json": [
                {"type": "rule", "id": "speech", "text": "Test set."},
                {"type": "table", "id": "accent", "options": ["burr", "lilt"]},
            ],
        },
    )
    library = ContentLibrary.load(tree)
    rule_set = library.rule_sets["testset"]
    assert rule_set.rules == {"speech": "Test set."}
    assert rule_set.tables["accent"] == ("burr", "lilt")
    assert rule_set.tables["quirk"] == ("one",)
    assert library.core.rules == {"speech": "Core."}


@pytest.mark.parametrize("missing", NPC_TABLES)
def test_core_must_declare_every_npc_table(tree: Path, missing: str):
    core = [d for d in _core() if d.get("id") != missing]
    _write(tree, {"rulesets/core/rule_set.json": core})
    with pytest.raises(ContentError, match=f"missing table {missing!r}"):
        ContentLibrary.load(tree)


def test_world_needs_no_locations_or_npcs(tree: Path):
    _write(tree, {"worlds/testland/world.json": [_world()[0]]})
    world = ContentLibrary.load(tree).world_templates["testland"]
    assert world.locations == ()
    assert world.npcs == ()


def test_npc_must_be_in_a_declared_location(tree: Path):
    world = [
        *_world(),
        {"type": "npc", "id": "bo", "name": "Bo", "location": "nowhere", "description": "A bo."},
    ]
    _write(tree, {"worlds/testland/world.json": world})
    with pytest.raises(ContentError, match="npc 'bo' is in unknown location 'nowhere'"):
        ContentLibrary.load(tree)
