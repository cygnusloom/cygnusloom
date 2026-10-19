from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from cygnusforge.content.definitions import ContentError, read_definitions
from cygnusforge.content.library import ContentLibrary
from cygnusforge.rules.freeform import SocialRoll, social_roll


def _write(root: Path, files: dict[str, Any]) -> None:
    for rel, payload in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        path.write_text(text, encoding="utf-8")


def _world(name: str = "Testland", ident: str = "testland") -> list[dict[str, str]]:
    return [{"type": "world", "id": ident, "name": name, "description": "A test world."}]


def _rule_set(ident: str = "testset") -> list[dict[str, str]]:
    return [{"type": "rule_set", "id": ident, "name": "Test set", "description": "For tests."}]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A minimal valid content tree. Tests break one piece at a time."""
    _write(
        tmp_path,
        {
            "worlds/testland/world.json": _world(),
            "rulesets/testset/rule_set.json": _rule_set(),
        },
    )
    return tmp_path


def test_bundled_content_loads():
    library = ContentLibrary.load()
    assert set(library.world_templates) == {"gnomeforest"}
    assert library.world_templates["gnomeforest"].name == "Gnome Forest"
    freeform = library.rule_sets["freeform"]
    assert freeform.name == "Freeform"
    assert "consistency" in freeform.rules
    assert set(freeform.rolls) == {"social"}


def test_bundled_social_roll_is_resolved_from_python():
    roll = ContentLibrary.load().rule_sets["freeform"].rolls["social"]
    assert roll.function is social_roll


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
    assert library.rule_sets["testset"].rolls == {}


def test_rules_and_rolls_are_read_from_their_definitions(tree: Path):
    _write(
        tree,
        {
            "rulesets/testset/rules.json": [{"type": "rule", "id": "r1", "text": "No magic."}],
            "rulesets/testset/module.json": [
                {"type": "python_module", "id": "m", "module": "cygnusforge.rules.freeform"},
                {
                    "type": "roll",
                    "id": "social",
                    "python_module": "m",
                    "function": "social_roll",
                    "description": "Talk it out.",
                },
            ],
        },
    )
    rule_set = ContentLibrary.load(tree).rule_sets["testset"]
    assert rule_set.rules == {"r1": "No magic."}
    assert rule_set.rolls["social"].description == "Talk it out."
    assert rule_set.rolls["social"].function is social_roll


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
    _write(tree, {"worlds/testland/world.json": [{"type": "world", "id": "testland"}]})
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


def test_roll_must_name_a_declared_python_module(tree: Path):
    _write(
        tree,
        {
            "rulesets/testset/roll.json": [
                {
                    "type": "roll",
                    "id": "social",
                    "python_module": "missing",
                    "function": "social_roll",
                    "description": "x",
                }
            ]
        },
    )
    with pytest.raises(ContentError, match="unknown python_module 'missing'"):
        ContentLibrary.load(tree)


def test_roll_function_must_exist_in_its_module(tree: Path):
    _write(
        tree,
        {
            "rulesets/testset/module.json": [
                {"type": "python_module", "id": "m", "module": "cygnusforge.rules.freeform"},
                {
                    "type": "roll",
                    "id": "social",
                    "python_module": "m",
                    "function": "no_such_function",
                    "description": "x",
                },
            ]
        },
    )
    with pytest.raises(ContentError, match="'no_such_function' is not a function"):
        ContentLibrary.load(tree)
