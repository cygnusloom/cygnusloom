from __future__ import annotations

import json
import random
import re
import shutil
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from cygnusforge.content.character import NO_CAMPAIGN, TableOption, campaign_schema
from cygnusforge.content.definitions import ContentError, Definition, read_definitions
from cygnusforge.content.library import (
    DATA_DIR,
    TERRAIN_PATTERNS,
    ContentLibrary,
    ItemKind,
    PlayerInput,
    _load_magic,
    _load_monster,
    _load_rule_hooks,
)
from cygnusforge.services.hooks import HookContext, call_hook
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
    "update_npc_knowledge",
    "delete_npc",
    "change_currency",
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
    "lookup_feature",
    "lookup_condition",
    "add_condition",
    "end_condition",
    "list_lookups",
    "roll_creative",
    "record_setting",
    "record_settings",
    "undo_narration",
    "create_local_map",
    "place_map_token",
    "remove_map_token",
    "set_map_cells",
    "list_dice_tables",
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
    assert set(library.world_templates) == {"twin-lakes", "hogwarts", "unwritten-world"}
    assert library.world_templates["twin-lakes"].name == "Twin Lakes"
    twin_lakes = library.world_templates["twin-lakes"]
    assert twin_lakes.locations
    assert set(library.rule_sets) == {"cairn2e", "hogwarts-rpg", "worldswithoutnumber"}
    assert library.world_content("twin-lakes", "cairn2e").monsters
    assert library.rule_sets["worldswithoutnumber"].character.skills
    assert library.rule_sets["worldswithoutnumber"].monsters


def _load_test_hooks(module: ModuleType, **overrides: Any):
    data = {
        "type": "hooks",
        "id": "test",
        "python_module": "test",
        **overrides,
    }
    return _load_rule_hooks(
        [Definition("hooks", "test", data)],
        {"test": module},
        content_library().rule_sets["cairn2e"].character,
        {},
        "testset",
    )


def test_unconfigured_rule_hooks_leave_feature_options_uninterpreted():
    module = ModuleType("test_rules")
    options = {"feature": {"not": "a feature reference"}, "payload": [1, 2]}

    hooks = _load_test_hooks(module, options=options)

    assert hooks is not None
    assert hooks.options == options


@pytest.mark.parametrize(
    "module_configurator",
    [None, "not callable"],
    ids=["missing", "non-callable"],
)
def test_rule_hooks_reject_missing_or_non_callable_configurators(module_configurator):
    module = ModuleType("test_rules")
    if module_configurator is not None:
        module.configure_hooks = module_configurator

    with pytest.raises(ContentError, match="hook configurator 'configure_hooks' is not callable"):
        _load_test_hooks(
            module,
            configure="configure_hooks",
            options={},
        )


@pytest.mark.parametrize(
    ("configured_options", "message"),
    [
        (None, "must return a mapping"),
        (["not", "a", "mapping"], "must return a mapping"),
        ({1: "not a string key"}, "non-string key"),
    ],
)
def test_rule_hooks_reject_invalid_configurator_results(configured_options, message):
    module = ModuleType("test_rules")
    module.configure_hooks = lambda options, character, label: configured_options

    with pytest.raises(ContentError, match=message):
        _load_test_hooks(
            module,
            configure="configure_hooks",
            options={},
        )


def test_rule_hooks_preserve_configurator_content_errors():
    module = ModuleType("test_rules")
    expected = "testset: hooks test: invalid hook options"

    def configure_hooks(options, character, label):
        raise ContentError(expected)

    module.configure_hooks = configure_hooks

    with pytest.raises(ContentError) as exc_info:
        _load_test_hooks(module, configure="configure_hooks", options={})

    assert str(exc_info.value) == expected


def test_rule_hooks_wrap_runtime_configurator_failures_with_cause():
    module = ModuleType("test_rules")

    def configure_hooks(options, character, label):
        raise RuntimeError("invalid hook settings")

    module.configure_hooks = configure_hooks

    with pytest.raises(
        ContentError,
        match="testset: hooks test: hook configurator 'configure_hooks' failed: invalid hook settings",
    ) as exc_info:
        _load_test_hooks(module, configure="configure_hooks", options={})

    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert str(exc_info.value.__cause__) == "invalid hook settings"


def test_rule_hooks_do_not_wrap_unexpected_configurator_errors():
    module = ModuleType("test_rules")

    def configure_hooks(options, character, label):
        raise AssertionError("hook configurator invariant failed")

    module.configure_hooks = configure_hooks

    with pytest.raises(AssertionError, match="hook configurator invariant failed"):
        _load_test_hooks(module, configure="configure_hooks", options={})


def test_cairn_hooks_resolve_rations_and_keep_the_rest_hook():
    hooks = content_library().rule_sets["cairn2e"].hooks

    assert hooks is not None
    assert hooks.functions["before_rest"] == "before_rest"
    assert set(hooks.functions) == {
        "new_day",
        "before_rest",
        "held_back",
        "after_consume",
        "lodge",
        "status",
    }
    assert "options" not in hooks.functions
    assert hooks.options == {
        "after_days": 1,
        "feature": "rations",
        "item": "Rations",
    }


def test_a_rule_set_can_declare_a_new_hook_point():
    module = ModuleType("test_rules")
    module.dawn = lambda hc: "dawned"

    hooks = _load_test_hooks(module, dawn="dawn")

    assert hooks is not None
    assert hooks.function_name("dawn") == "dawn"
    hc = HookContext.for_rendering(hooks, None)
    assert call_hook(hooks, "dawn", hc, default=None) == "dawned"


def test_an_undeclared_hook_returns_its_default():
    hooks = _load_test_hooks(ModuleType("test_rules"))

    assert hooks is not None
    hc = HookContext.for_rendering(hooks, None)
    assert call_hook(hooks, "new_day", hc, 1, default=None) is None


def test_a_hook_naming_a_missing_function_is_rejected():
    module = ModuleType("test_rules")

    with pytest.raises(ContentError, match="'missing' is not a function in 'test'"):
        _load_test_hooks(module, dawn="missing")


def test_a_hook_payload_must_name_non_empty_text():
    module = ModuleType("test_rules")

    with pytest.raises(ContentError, match="'dawn' must be non-empty text"):
        _load_test_hooks(module, dawn="")


@pytest.mark.parametrize(
    ("feature", "message"),
    [
        ("not-a-feature", "'feature' names no character feature"),
        ("spellbook-thicket", "'feature' must be a consumable item with uses"),
        ("awl", "'feature' must be a consumable item with uses"),
        ([], "'feature' must be a non-empty string"),
    ],
)
def test_cairn_hook_configurator_validates_consumable_features(feature, message):
    from cygnusforge.rules.cairn2e import configure_hooks

    module = ModuleType("cairn2e")
    module.configure_hooks = configure_hooks

    with pytest.raises(ContentError, match=re.escape(message)):
        _load_test_hooks(
            module,
            configure="configure_hooks",
            options={"feature": feature},
        )


def test_bundled_worlds_say_which_rule_set_they_use():
    library = ContentLibrary.load()
    assert library.world_templates["twin-lakes"].rule_set == "cairn2e"
    assert library.world_templates["hogwarts"].rule_set == "hogwarts-rpg"
    assert library.world_templates["twin-lakes"].blurb
    assert library.world_templates["hogwarts"].blurb
    assert library.rule_sets["cairn2e"].blurb
    assert library.rule_sets["hogwarts-rpg"].blurb


def test_hogwarts_world_has_a_map_the_gm_can_grow():
    """Every top-level place in the Hogwarts world has a map point, and its ground is drawn from
    kinds the rule set defines, so the world map shows the grounds and the GM can add regions."""
    library = ContentLibrary.load()
    world = library.world_templates["hogwarts"]
    top_level = [place for place in world.locations if place.parent is None]
    assert top_level
    assert all(place.x is not None and place.y is not None for place in top_level)
    assert world.regions
    # The ground reads as land, not as rectangles: every region has several uneven sides.
    assert all(len(region.points) >= 6 for region in world.regions)
    kinds = library.rule_sets["hogwarts-rpg"].world_terrain
    assert kinds
    assert {region.terrain for region in world.regions} <= set(kinds)
    # Both bundled rule sets define ground kinds, so create_region works in either world.
    assert all(rule_set.world_terrain for rule_set in library.rule_sets.values())


def test_create_region_asks_for_varied_shapes_cut_to_the_ground():
    tool = ContentLibrary.load().core.tool_calls["create_region"]
    assert "six or more corners" in tool.description
    assert "uneven" in tool.description
    assert "cut to fit" in tool.description


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


def test_rule_set_blurb_is_read(tree: Path):
    rule_set = [{**_rule_set()[0], "blurb": "A short line."}]
    _write(tree, {"rulesets/testset/rule_set.json": rule_set})
    assert ContentLibrary.load(tree).rule_sets["testset"].blurb == "A short line."


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


def test_tool_call_enum_must_come_from_a_list_the_rule_set_has(tree: Path):
    properties = {"relationship": {"type": "string", "enum_from": "bogus"}}
    parameters = {"type": "object", "properties": properties}
    _write(tree, {"rulesets/testset/module.json": _module(parameters=parameters)})
    with pytest.raises(ContentError, match="has enum_from 'bogus'"):
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


def test_world_reads_the_rule_set_it_is_played_with(tree: Path):
    world = [{**_world()[0], "rule_set": "testset", "blurb": "A short line."}]
    _write(tree, {"worlds/testland/world.json": world})
    loaded = ContentLibrary.load(tree).world_templates["testland"]
    assert loaded.rule_set == "testset"
    assert loaded.blurb == "A short line."


def test_world_may_name_no_rule_set(tree: Path):
    assert ContentLibrary.load(tree).world_templates["testland"].rule_set is None


def test_world_rule_set_must_be_available(tree: Path):
    world = [{**_world()[0], "rule_set": "nowhere"}]
    _write(tree, {"worlds/testland/world.json": world})
    with pytest.raises(ContentError, match="world names unknown rule set 'nowhere'"):
        ContentLibrary.load(tree)


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


def test_a_rule_set_cannot_add_review_prompts(tree: Path):
    _write(
        tree,
        {
            "rulesets/testset/prompts.json": [
                {"type": "prompt", "id": "review", "text": "Check it.", "step": "review"}
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
        "encounter_start",
        "player_rolls",
        "conditions",
        "hirelings",
        "monster_abilities",
        "setting_forge",
        "setting_continuity",
        "local_maps",
    ]
    core = ContentLibrary.load().core.prompt_fragments
    assert [fragment.id for fragment in core] == [
        "setting_forge",
        "setting_continuity",
        "local_maps",
    ]
    assert all(fragment.unwritten_only for fragment in core if fragment.id != "local_maps")


def _monster_definition(**changes: Any) -> Definition:
    character = content_library().rule_sets["cairn2e"].character
    stats = {stat: 10 for stat in character.stats}
    data: dict[str, Any] = {
        "type": "monster",
        "id": "ghoul",
        "name": "Ghoul",
        "description": "A ghoul.",
        "stats": stats,
    }
    data.update(changes)
    return Definition(type="monster", id="ghoul", data=data)


def test_a_monster_needs_at_least_one_hit_point():
    character = content_library().rule_sets["cairn2e"].character
    stats = {stat: 10 for stat in character.stats}
    stats["hp"] = 0
    definition = _monster_definition(stats=stats)
    with pytest.raises(ContentError, match="stat 'hp' must be at least 1"):
        _load_monster(definition, character, "testland", pool="hp")
    stats["hp"] = 1
    assert _load_monster(definition, character, "testland", pool="hp").stats["hp"] == 1


def test_a_monster_attack_count_is_a_structured_field():
    character = content_library().rule_sets["cairn2e"].character
    definition = _monster_definition(
        description="A ghoul that strikes twice. (2 attacks per action.)",
        attacks_per_action=2,
    )
    monster = _load_monster(definition, character, "testland", pool="hp")
    assert monster.attacks_per_action == 2
    assert "2 attacks per action" in monster.description


def test_a_monster_attack_count_defaults_to_one():
    character = content_library().rule_sets["cairn2e"].character
    monster = _load_monster(_monster_definition(), character, "testland", pool="hp")
    assert monster.attacks_per_action == 1


@pytest.mark.parametrize("count", [0, -1, "2", 2.5, True])
def test_a_monster_attack_count_must_be_a_positive_whole_number(count: Any):
    character = content_library().rule_sets["cairn2e"].character
    definition = _monster_definition(attacks_per_action=count)
    message = "'attacks_per_action' must be a whole number, at least 1"
    with pytest.raises(ContentError, match=message):
        _load_monster(definition, character, "testland", pool="hp")


def test_a_monster_attack_count_left_as_prose_is_refused():
    character = content_library().rule_sets["cairn2e"].character
    definition = _monster_definition(description="A ghoul. (2 attacks per action.)")
    with pytest.raises(ContentError, match="structured 'attacks_per_action' field"):
        _load_monster(definition, character, "testland", pool="hp")


MAGIC_SIZES = {"petty": 0, "normal": 1, "bulky": 2}


def _magic_definitions(**changes: Any) -> list[Definition]:
    data: dict[str, Any] = {
        "reusable": {"kind": "spellbook", "size": "normal", "icon": "book"},
        "single_use": {"kind": "scroll", "size": "petty", "icon": "scroll"},
        "charged": {"kind": "relic"},
        "cast_penalty": 1,
    }
    data.update(changes)
    return [Definition(type="magic", id="cairn", data=data)]


def test_magic_roles_are_read_with_their_kinds_sizes_and_icons():
    magic = _load_magic(_magic_definitions(), MAGIC_SIZES, "testland")
    assert magic is not None
    assert magic.reusable == ItemKind("spellbook", "normal", "book")
    assert magic.single_use == ItemKind("scroll", "petty", "scroll")
    assert magic.charged == ItemKind("relic")
    assert magic.item_kind_icons == {"spellbook": "book", "scroll": "scroll"}
    assert magic.cast_penalty == 1


def test_magic_icon_must_be_a_name():
    data = _magic_definitions(charged={"kind": "relic", "icon": ""})
    with pytest.raises(ContentError, match="'icon' must be the name of an icon"):
        _load_magic(data, MAGIC_SIZES, "testland")


def test_a_charged_role_rejects_a_size():
    with pytest.raises(ContentError, match="unknown key\\(s\\) 'size'"):
        _load_magic(
            _magic_definitions(charged={"kind": "wand", "size": "bulky"}),
            MAGIC_SIZES,
            "testland",
        )


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
        (
            {"single_use": {"kind": "spellbook", "size": "petty"}},
            "magic item roles must use distinct kinds",
        ),
        ({"charged": {"kind": "scroll"}}, "magic item roles must use distinct kinds"),
        ({"cast_penalty": -1}, "'cast_penalty' must be a whole number"),
    ],
)
def test_a_bad_magic_role_is_rejected(changes, message):
    with pytest.raises(ContentError, match=re.escape(message)):
        _load_magic(_magic_definitions(**changes), MAGIC_SIZES, "testland")


def _twin_lakes_copy(tmp_path: Path) -> Path:
    """A copy of the bundled content, so a test can change one location and load it."""
    root = tmp_path / "data"
    shutil.copytree(DATA_DIR, root)
    return root


def _edit_twin_lakes_location(root: Path, location_id: str, **changes: object) -> None:
    path = root / "worlds" / "twin-lakes" / "locations.json"
    locations = json.loads(path.read_text(encoding="utf-8"))
    (entry,) = [loc for loc in locations if loc.get("id") == location_id]
    entry.update(changes)
    path.write_text(json.dumps(locations, indent=2), encoding="utf-8")


def _valid_route(**overrides: object) -> dict[str, object]:
    route: dict[str, object] = {"path": "trails", "distance": "short", "terrain": "easy"}
    route.update(overrides)
    return route


def test_bundled_twin_lakes_routes_load_on_the_location_that_authors_them():
    place = content_library().world_templates["twin-lakes"]
    isthmus = next(loc for loc in place.locations if loc.id == "isthmus-town")
    assert isthmus.routes["dunston"] == _valid_route()


def test_an_authored_route_may_carry_vast(tmp_path: Path):
    root = _twin_lakes_copy(tmp_path)
    _edit_twin_lakes_location(
        root, "isthmus-town", routes={"dunston": _valid_route(path="wilderness", vast=1)}
    )
    place = ContentLibrary.load(root).world_templates["twin-lakes"]
    isthmus = next(loc for loc in place.locations if loc.id == "isthmus-town")
    assert isthmus.routes == {
        "dunston": {"path": "wilderness", "distance": "short", "terrain": "easy", "vast": 1}
    }


@pytest.mark.parametrize(
    ("routes", "message"),
    [
        ({"nowhere": _valid_route()}, "'nowhere' is not a location in this world"),
        ({"isthmus-town": _valid_route()}, "cannot lead to itself"),
        ({"dunston": _valid_route(path="lanes")}, "path 'lanes' is not in the rule set's travel"),
        (
            {"dunston": _valid_route(distance="huge")},
            "distance 'huge' is not in the rule set's travel",
        ),
        (
            {"dunston": _valid_route(terrain="swamp")},
            "terrain 'swamp' is not in the rule set's travel",
        ),
        ({"dunston": _valid_route(vast=3)}, "vast 3 is more than the rule set's 2"),
        ({"dunston": _valid_route(speed="fast")}, "has unknown keys \\['speed'\\]"),
        ({"dunston": {"distance": "short", "terrain": "easy"}}, "'path'"),
        ({"dunston": "trails"}, "must be an object"),
        (["dunston"], "'routes' must map destination location ids to route objects"),
    ],
)
def test_an_invalid_authored_route_is_rejected(tmp_path: Path, routes: object, message: str):
    root = _twin_lakes_copy(tmp_path)
    _edit_twin_lakes_location(root, "isthmus-town", routes=routes)
    with pytest.raises(ContentError, match=message):
        ContentLibrary.load(root).world_content("twin-lakes", "cairn2e")


def test_the_twin_lakes_features_have_their_listed_sizes_uses_and_damage():
    features = content_library().world_content("twin-lakes", "cairn2e").features
    fletcher = features["fletcher-s-ire"]
    assert (fletcher.name, fletcher.size, fletcher.uses) == ("Fletcher's Ire", "bulky", 3)
    moon = features["moon-cloth"]
    assert (moon.name, moon.size) == ("Moon Cloth", "petty")
    dagger = features["curved-dagger"]
    assert (dagger.name, dagger.damage) == ("Curved Dagger", "1d6")
    assert features["echo-spark"].uses == 1
    assert features["watersnatch"].uses == 1


def test_the_suraru_imperators_venomous_bite_is_a_blast():
    monster = content_library().world_content("twin-lakes", "cairn2e").monsters["suraru-imperator"]
    assert monster.attack_flags == {"venomous bite": ("blast",)}
    assert monster.attacks["venomous bite"] == "1d6"


def _edit_hogwarts_templates(root: Path, edit: Any) -> None:
    path = root / "rulesets" / "hogwarts-rpg" / "templates.json"
    templates = json.loads(path.read_text(encoding="utf-8"))
    edit(templates)
    path.write_text(json.dumps(templates, indent=2), encoding="utf-8")


@pytest.mark.parametrize(
    "edit,message",
    [
        (lambda t: t[0]["stats"].update({"charm": 1}), "unknown stat 'charm' in 'stats'"),
        (lambda t: t[-1]["counters"].update({"mana": 1}), "unknown counter 'mana' in 'counters'"),
        (lambda t: t[-1]["counters"].update({"experience": -1}), "amounts must be at least 0"),
    ],
)
def test_a_template_grant_must_name_a_known_stat_or_counter(tmp_path: Path, edit, message: str):
    root = _twin_lakes_copy(tmp_path)
    _edit_hogwarts_templates(root, edit)
    with pytest.raises(ContentError, match=re.escape(message)):
        ContentLibrary.load(root)
