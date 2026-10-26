from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Any

import pytest

from cygnusforge.content.character import NO_CAMPAIGN, TableOption, campaign_schema
from cygnusforge.content.definitions import ContentError, Definition, read_definitions
from cygnusforge.content.library import (
    TERRAIN_PATTERNS,
    ContentLibrary,
    ItemKind,
    PlayerInput,
    _load_magic,
    _load_monster,
)
from tests.conftest import content_library
from tests.rolls import SocialRoll, social_roll


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
        {"type": "python_module", "id": "m", "module": "tests.rolls"},
        _social_tool(**tool_overrides),
    ]


def _rule_set(ident: str = "testset") -> list[dict[str, str]]:
    return [{"type": "rule_set", "id": ident, "name": "Test set", "description": "For tests."}]


CORE_TOOLS = {
    "create_location",
    "create_region",
    "create_npc",
    "update_location",
    "discover_location",
    "delete_location",
    "update_npc",
    "delete_npc",
    "hire_hireling",
    "dismiss_hireling",
    "change_gold",
    "place_object",
    "take_object",
    "move_actor",
    "all_at_location",
    "search_location",
    "search_npc",
    "get_statblock",
    "character_options",
    "add_note",
    "update_note",
    "delete_note",
    "search_notes",
    "append_journal",
    "replace_in_journal",
    "search_journal",
    "view_journal",
    "speak",
    "ooc",
    "lookup_rule",
    "list_lookups",
    "create_encounter_map",
    "update_encounter_map",
    "delete_encounter_map",
    "set_map_cells",
    "start_encounter",
    "end_encounter",
    "add_participant",
    "remove_participant",
    "place_participant",
    "end_turn",
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
    assert set(library.world_templates) == {"twin-lakes"}
    assert library.world_templates["twin-lakes"].name == "Twin Lakes"
    twin_lakes = library.world_templates["twin-lakes"]
    assert twin_lakes.locations
    assert set(library.rule_sets) == {"cairn2e"}
    assert library.world_content("twin-lakes", "cairn2e").monsters


def test_twin_lakes_map_matches_the_module_map():
    """The module's map has no scale bar. It gives travel times instead: Dunston is one hour
    from Isthmus Town, and the White Tower Fens are two hours from either. At four kilometres
    an hour on foot, the placed positions must agree with those times."""
    places = {loc.id: loc for loc in ContentLibrary.load().world_templates["twin-lakes"].locations}

    def km(a: str, b: str) -> float:
        pa, pb = places[a], places[b]
        return ((pa.x - pb.x) ** 2 + (pa.y - pb.y) ** 2) ** 0.5

    assert km("isthmus-town", "dunston") / 4 == pytest.approx(1, abs=0.25)
    assert km("isthmus-town", "white-tower-fens") / 4 == pytest.approx(2, abs=0.25)
    assert km("dunston", "white-tower-fens") / 4 == pytest.approx(2, abs=0.25)
    assert km("temple-of-the-fungal-lizards", "bone-sump") < 1


def test_test_fixtures_add_a_hidden_roll_rule_set():
    library = content_library()
    assert {"twin-lakes", "testhollow"} <= set(library.world_templates)
    rule_set = library.rule_sets["rolltest"]
    assert rule_set.name == "Roll Test"
    assert "consistency" in rule_set.rules
    assert set(rule_set.tool_calls) == CORE_TOOLS | {"social_roll"}


def test_social_roll_fixture_is_a_tool_call_backed_by_python():
    tool = content_library().rule_sets["rolltest"].tool_calls["social_roll"]
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


def test_important_rules_and_lookup_tools_are_flagged(tree: Path):
    _write(
        tree,
        {
            "rulesets/testset/rules.json": [
                {"type": "rule", "id": "r1", "text": "No magic.", "important": True},
                {"type": "rule", "id": "r2", "text": "Lots of detail."},
            ],
            "rulesets/testset/module.json": _module(lookup=True),
        },
    )
    rule_set = ContentLibrary.load(tree).rule_sets["testset"]
    assert rule_set.important == {"r1"}
    assert rule_set.tool_calls["social"].lookup is True


def test_unflagged_rules_and_tools_are_not_important_or_lookups(tree: Path):
    _write(
        tree,
        {
            "rulesets/testset/rules.json": [
                {"type": "rule", "id": "r1", "text": "No magic."},
            ],
            "rulesets/testset/module.json": _module(),
        },
    )
    rule_set = ContentLibrary.load(tree).rule_sets["testset"]
    assert rule_set.important == frozenset()
    assert rule_set.tool_calls["social"].lookup is False


@pytest.mark.parametrize(
    "rule_overrides, tool_overrides, message",
    [
        ({"important": "yes"}, {}, "'important' must be true or false"),
        ({}, {"lookup": "yes"}, "'lookup' must be true or false"),
    ],
)
def test_flags_must_be_true_or_false(
    tree: Path,
    rule_overrides: dict[str, Any],
    tool_overrides: dict[str, Any],
    message: str,
):
    _write(
        tree,
        {
            "rulesets/testset/rules.json": [
                {"type": "rule", "id": "r1", "text": "No magic.", **rule_overrides}
            ],
            "rulesets/testset/module.json": _module(**tool_overrides),
        },
    )
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
                {"type": "python_module", "id": "m", "module": "tests.rolls"},
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
    assert rule_set.tables["accent"] == (
        TableOption(name=None, description="burr"),
        TableOption(name=None, description="lilt"),
    )
    assert rule_set.tables["quirk"] == (TableOption(name=None, description="one"),)
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


def _region(ident: str, name: str, points: Any, terrain: str = "lake") -> dict[str, Any]:
    return {"type": "region", "id": ident, "name": name, "terrain": terrain, "points": points}


SQUARE = [[0, 0], [1, 0], [1, 1], [0, 1]]
TESTSET_TERRAIN = [
    {"type": "world_terrain", "id": "lake", "name": "Lake", "color": "#2d6a8f", "rule": "Water."}
]


def test_bundled_terrain_and_regions_are_read():
    library = ContentLibrary.load()
    terrain = library.rule_sets["cairn2e"].world_terrain
    lake = terrain["lake"]
    assert (lake.name, lake.color) == ("Lake", "#2d6a8f")
    assert {"lake", "open", "forest", "hills", "fen"} <= set(terrain)
    regions = {r.name: r for r in library.world_templates["twin-lakes"].regions}
    assert len(regions) == 12
    assert all(r.terrain in terrain for r in regions.values())
    assert regions["West Lake"].terrain == "lake"
    assert regions["Stone Forest"].terrain == "forest"
    assert regions["The Downs"].terrain == "hills"
    assert regions["White Tower Fens"].terrain == "fen"
    assert regions["Eastern Plain"].terrain == "open"


def test_bundled_terrain_kinds_have_a_texture_and_ink(tree: Path):
    terrain = ContentLibrary.load().rule_sets["cairn2e"].world_terrain
    assert all(kind.pattern in TERRAIN_PATTERNS for kind in terrain.values())
    assert all(kind.ink and kind.ink.startswith("#") for kind in terrain.values())
    assert (terrain["lake"].pattern, terrain["lake"].ink) == ("waves", "#a9d4ea")


def test_terrain_texture_must_be_one_the_engine_draws(tree: Path):
    bad = {**TESTSET_TERRAIN[0], "pattern": "bricks", "ink": "#000000"}
    _write(tree, {"rulesets/testset/world_terrain.json": [bad]})
    with pytest.raises(ContentError, match="pattern must be one of waves, trees"):
        ContentLibrary.load(tree)


def test_terrain_texture_needs_an_ink_colour(tree: Path):
    bad = {**TESTSET_TERRAIN[0], "pattern": "waves"}
    _write(tree, {"rulesets/testset/world_terrain.json": [bad]})
    with pytest.raises(ContentError, match="ink"):
        ContentLibrary.load(tree)


def test_region_points_must_outline_a_polygon(tree: Path):
    _write(tree, {"worlds/testland/regions.json": [_region("mere", "Mere", [[0, 0], [1, 1]])]})
    with pytest.raises(ContentError, match="testland: region mere: needs at least three points"):
        ContentLibrary.load(tree)


def test_region_points_must_be_number_pairs(tree: Path):
    _write(
        tree,
        {"worlds/testland/regions.json": [_region("mere", "Mere", [["a", 0], [1, 0], [1, 1]])]},
    )
    with pytest.raises(ContentError, match="points must be a list of \\[x, y\\] pairs"):
        ContentLibrary.load(tree)


def test_region_names_must_be_unique(tree: Path):
    regions = [_region("a", "Mere", SQUARE), _region("b", "mere", [[5, 5], [6, 5], [6, 6]])]
    _write(tree, {"worlds/testland/regions.json": regions})
    with pytest.raises(ContentError, match="duplicate region name 'mere'"):
        ContentLibrary.load(tree)


def test_data_regions_may_not_overlap(tree: Path):
    regions = [_region("a", "Mere", SQUARE), _region("b", "Pool", [[0.5, 0.5], [2, 0.5], [2, 2]])]
    _write(tree, {"worlds/testland/regions.json": regions})
    with pytest.raises(ContentError, match="region 'Pool' overlaps region 'Mere'"):
        ContentLibrary.load(tree)


def test_region_terrain_must_be_defined_by_the_rule_set(tree: Path):
    _write(tree, {"worlds/testland/regions.json": [_region("a", "Mere", SQUARE)]})
    library = ContentLibrary.load(tree)
    with pytest.raises(ContentError, match="has terrain 'lake', which the 'testset' rule set"):
        library.world_content("testland", "testset")


def test_region_terrain_is_accepted_when_the_rule_set_defines_it(tree: Path):
    _write(
        tree,
        {
            "worlds/testland/regions.json": [_region("a", "Mere", SQUARE)],
            "rulesets/testset/world_terrain.json": TESTSET_TERRAIN,
        },
    )
    library = ContentLibrary.load(tree)
    assert library.world_content("testland", "testset") is not None


def _start_world(start: Any) -> list[dict[str, Any]]:
    world = _world()
    world[0] = {**world[0], "start": start}
    return world


def test_world_start_is_read(tree: Path):
    _write(
        tree,
        {"worlds/testland/world.json": _start_world({"location": "town", "hooks": "A reward."})},
    )
    start = ContentLibrary.load(tree).world_templates["testland"].start
    assert start is not None
    assert (start.location, start.hooks) == ("town", "A reward.")


def test_world_without_start_has_none(tree: Path):
    assert ContentLibrary.load(tree).world_templates["testland"].start is None


def test_world_start_must_name_a_declared_location(tree: Path):
    _write(
        tree,
        {"worlds/testland/world.json": _start_world({"location": "nowhere", "hooks": "x"})},
    )
    with pytest.raises(ContentError, match="start names unknown location 'nowhere'"):
        ContentLibrary.load(tree)


def test_world_start_needs_hooks(tree: Path):
    _write(tree, {"worlds/testland/world.json": _start_world({"location": "town"})})
    with pytest.raises(ContentError, match="'hooks' must be a non-empty string"):
        ContentLibrary.load(tree)


def test_location_map_position_and_discovery_are_read(tree: Path):
    world = [
        *_world(),
        {
            "type": "location",
            "id": "mill",
            "name": "Mill",
            "description": "A water mill.",
            "discovered": True,
            "x": -1.5,
            "y": 2,
        },
    ]
    _write(tree, {"worlds/testland/world.json": world})
    town, mill = ContentLibrary.load(tree).world_templates["testland"].locations
    assert (town.discovered, town.x, town.y) == (False, None, None)
    assert (mill.discovered, mill.x, mill.y) == (True, -1.5, 2.0)


@pytest.mark.parametrize(
    "fields, message",
    [
        ({"discovered": "yes"}, "discovered must be true or false"),
        ({"x": 1}, "give both x and y, or neither"),
        ({"x": 1, "y": "north"}, "y must be a number of kilometers"),
        ({"x": True, "y": 0}, "x must be a number of kilometers"),
    ],
)
def test_bad_location_map_fields_are_rejected(tree: Path, fields: dict, message: str):
    location = {
        "type": "location",
        "id": "mill",
        "name": "Mill",
        "description": "A mill.",
        **fields,
    }
    _write(tree, {"worlds/testland/world.json": [*_world(), location]})
    with pytest.raises(ContentError, match=f"location mill: {message}"):
        ContentLibrary.load(tree)


def _place(id: str, **fields: Any) -> dict[str, Any]:
    return {"type": "location", "id": id, "name": id.title(), "description": "A place.", **fields}


def test_location_can_be_declared_inside_another(tree: Path):
    world = [*_world()[:1], _place("town", x=0, y=0), _place("inn", parent="town")]
    _write(tree, {"worlds/testland/world.json": world})
    town, inn = ContentLibrary.load(tree).world_templates["testland"].locations
    assert (town.parent, inn.parent) == (None, "town")
    assert (inn.x, inn.y) == (None, None)


@pytest.mark.parametrize(
    "places, message",
    [
        ([_place("inn", parent="nowhere")], "location inn: parent 'nowhere' is not a location"),
        ([_place("inn", parent="inn")], "location inn: cannot be inside itself"),
        (
            [_place("town"), _place("inn", parent="town"), _place("cellar", parent="inn")],
            "location cellar: parent 'inn' is itself inside 'town'",
        ),
        (
            [_place("town"), _place("inn", parent="town", x=1, y=2)],
            "location inn: a place inside another place has no map position",
        ),
    ],
)
def test_bad_parent_locations_are_rejected(tree: Path, places: list, message: str):
    _write(tree, {"worlds/testland/world.json": [*_world()[:1], *places]})
    with pytest.raises(ContentError, match=message):
        ContentLibrary.load(tree)


def test_named_table_options_keep_their_name_and_description(tree: Path):
    _write(
        tree,
        {
            "rulesets/testset/rules.json": [
                {
                    "type": "table",
                    "id": "accent",
                    "options": [{"name": "Burr", "description": "A rough accent."}, "Lilt"],
                },
            ],
        },
    )
    accent = ContentLibrary.load(tree).rule_sets["testset"].tables["accent"]
    assert accent == (
        TableOption(name="Burr", description="A rough accent."),
        TableOption(name=None, description="Lilt"),
    )
    assert [option.value for option in accent] == ["Burr", "Lilt"]


@pytest.mark.parametrize(
    "options",
    [
        [{"name": "Burr"}],
        [{"name": "", "description": "A rough accent."}],
        [{"name": "Burr", "description": "A rough accent.", "extra": "x"}],
        [""],
        [3],
        ["Burr", {"name": "Burr", "description": "Another."}],
    ],
)
def test_table_options_must_be_text_or_named_text(tree: Path, options: list[Any]):
    _write(
        tree,
        {"rulesets/testset/rules.json": [{"type": "table", "id": "accent", "options": options}]},
    )
    with pytest.raises(ContentError, match="table accent"):
        ContentLibrary.load(tree)


def test_cairn_campaign_options_are_the_omen_alone():
    cairn = ContentLibrary.load().rule_sets["cairn2e"]
    assert set(cairn.campaign.fields) == {"omen"}
    assert "omen" in cairn.campaign.tables
    assert "omen" not in cairn.character.fields
    schema = campaign_schema(cairn.campaign)
    assert schema["required"] == ["omen"]
    assert schema["additionalProperties"] is False
    omen = cairn.campaign.tables["omen"][0].value
    assert schema["properties"]["omen"]["enum"][0] == omen


def test_no_campaign_has_no_options():
    assert not NO_CAMPAIGN.fields
    assert campaign_schema(NO_CAMPAIGN)["required"] == []


def test_core_has_no_prompt_fragments_and_no_sentence_cap(tree: Path):
    rule_set = ContentLibrary.load(tree).rule_sets["testset"]
    assert rule_set.prompt_fragments == ()
    assert rule_set.sentence_cap is None


def test_prompt_fragments_keep_their_file_order(tree: Path):
    _write(
        tree,
        {
            "rulesets/testset/prompts.json": [
                {"type": "prompt", "id": "second", "text": "Two."},
                {"type": "prompt", "id": "first", "text": "One."},
            ]
        },
    )
    fragments = ContentLibrary.load(tree).rule_sets["testset"].prompt_fragments
    assert [(f.id, f.text) for f in fragments] == [("second", "Two."), ("first", "One.")]


def test_duplicate_prompt_fragment_ids_are_rejected(tree: Path):
    _write(
        tree,
        {
            "rulesets/testset/prompts.json": [
                {"type": "prompt", "id": "same", "text": "One."},
                {"type": "prompt", "id": "same", "text": "Two."},
            ]
        },
    )
    with pytest.raises(ContentError):
        ContentLibrary.load(tree)


def test_sentence_cap_is_read_from_narration(tree: Path):
    _write(
        tree,
        {
            "rulesets/testset/narration.json": [
                {"type": "narration", "id": "speech", "sentence_cap": 3}
            ]
        },
    )
    assert ContentLibrary.load(tree).rule_sets["testset"].sentence_cap == 3


def test_bundled_cairn_prompts_and_cap_are_read():
    cairn = ContentLibrary.load().rule_sets["cairn2e"]
    assert cairn.sentence_cap == 2
    ids = [fragment.id for fragment in cairn.prompt_fragments]
    assert ids == [
        "relic_recharge",
        "combat_speech",
        "armor_glancing_blows",
        "wilderness_action",
        "dungeon_cycle",
        "surrender",
        "group_break",
        "defeated_character",
        "time_and_rest",
        "needs",
        "give_item_from_place",
        "purchases",
        "attack_flags",
        "overland_weather",
    ]
    assert ContentLibrary.load().core.prompt_fragments == ()


def test_a_monster_needs_at_least_one_hit_point():
    character = content_library().rule_sets["cairn2e"].character
    stats = {stat: 10 for stat in character.stats}
    stats["hp"] = 0
    definition = Definition(
        type="monster",
        id="ghoul",
        data={
            "type": "monster",
            "id": "ghoul",
            "name": "Ghoul",
            "description": "A ghoul.",
            "stats": stats,
        },
    )
    with pytest.raises(ContentError, match="stat 'hp' must be at least 1"):
        _load_monster(definition, character, "testland", pool="hp")
    stats["hp"] = 1
    assert _load_monster(definition, character, "testland", pool="hp").stats["hp"] == 1


MAGIC_SIZES = {"petty": 0, "normal": 1, "bulky": 2}


def _magic_definitions(**changes: Any) -> list[Definition]:
    data: dict[str, Any] = {
        "reusable": {"kind": "spellbook", "size": "normal"},
        "single_use": {"kind": "scroll", "size": "petty"},
        "charged": {"kind": "relic"},
        "cast_penalty": 1,
    }
    data.update(changes)
    return [Definition(type="magic", id="cairn", data=data)]


def test_magic_roles_are_read_with_their_kinds_and_sizes():
    magic = _load_magic(_magic_definitions(), MAGIC_SIZES, "testland")
    assert magic is not None
    assert magic.reusable == ItemKind("spellbook", "normal")
    assert magic.single_use == ItemKind("scroll", "petty")
    assert magic.charged == ItemKind("relic")
    assert (magic.spellbook_kind, magic.spellbook_size) == ("spellbook", "normal")
    assert (magic.scroll_kind, magic.scroll_size) == ("scroll", "petty")
    assert magic.charged_kind == "relic"
    assert magic.cast_penalty == 1


def test_a_charged_role_takes_no_size_even_if_one_is_given():
    magic = _load_magic(
        _magic_definitions(charged={"kind": "wand", "size": "bulky"}), MAGIC_SIZES, "testland"
    )
    assert magic is not None
    assert magic.charged == ItemKind("wand")


def test_no_magic_definition_means_no_magic():
    assert _load_magic([], MAGIC_SIZES, "testland") is None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"reusable": "spellbook"}, "'reusable' must be an object with a 'kind'"),
        ({"single_use": None}, "'single_use' must be an object with a 'kind'"),
        ({"charged": {}}, "'kind' must be a non-empty string"),
        ({"reusable": {"kind": "spellbook"}}, "'size' must be a non-empty string"),
        ({"single_use": {"kind": "scroll", "size": "huge"}}, "names no item size ('huge')"),
        ({"cast_penalty": -1}, "'cast_penalty' must be a whole number"),
    ],
)
def test_a_bad_magic_role_is_rejected(changes, message):
    with pytest.raises(ContentError, match=re.escape(message)):
        _load_magic(_magic_definitions(**changes), MAGIC_SIZES, "testland")
