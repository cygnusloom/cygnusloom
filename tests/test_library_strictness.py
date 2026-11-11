"""Strict definition schemas and the important-NPC defeat label.

These tests use the test-only fixture rule set, so production content is not changed merely to
exercise a loader.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from cygnusloom.content.definitions import ContentError
from cygnusloom.content.library import ContentLibrary
from cygnusloom.services.events import (
    NPC_KIND,
    Character,
    defeat_status,
)
from tests.conftest import content_library

NPC_TABLES = ("personality", "accent", "quirk")


def _write(root: Path, files: dict[str, Any]) -> None:
    for rel, payload in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        path.write_text(text, encoding="utf-8")


def _ruleset(ident: str = "testset") -> list[dict[str, Any]]:
    return [{"type": "rule_set", "id": ident, "name": "Test set", "description": "For tests."}]


def _core() -> list[dict[str, Any]]:
    tables = [{"type": "table", "id": name, "options": ["one"]} for name in NPC_TABLES]
    return [*_ruleset("core"), *tables]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A minimal valid content tree. Tests break one piece at a time."""
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


def _stats() -> list[dict[str, Any]]:
    return [
        {"type": "stat", "id": "hp", "name": "HP", "formula": "1d6"},
        {"type": "stat", "id": "dex", "name": "DEX", "formula": "1d6"},
    ]


def _combat(**overrides: Any) -> dict[str, Any]:
    combat: dict[str, Any] = {
        "type": "combat",
        "id": "test",
        "check_die": "1d20",
        "initiative_stat": "dex",
        "damage": [{"kind": "harm", "stat": "hp"}],
        "defeat": {"player_character": "wounded", "npc": "dead"},
    }
    combat.update(overrides)
    return combat


def _tool_call(**overrides: Any) -> list[dict[str, Any]]:
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
    return [
        {"type": "python_module", "id": "m", "module": "tests.rolls"},
        tool,
    ]


def test_wwn_combat_loads_the_important_npc_defeat_label():
    combat = content_library().rule_sets["worldswithoutnumber"].combat
    assert combat is not None
    assert combat.defeat["important_npc"] == "mortally wounded"
    assert combat.defeat["npc"] == "dead"


def test_wwn_combat_loads_every_defeat_label_in_the_mapping():
    combat = content_library().rule_sets["worldswithoutnumber"].combat
    assert combat is not None
    assert set(combat.defeat) == {
        "player_character",
        "player_character_dead",
        "important_npc",
        "npc",
    }


def test_an_important_npc_is_mortally_wounded_not_dead():
    combat = content_library().rule_sets["worldswithoutnumber"].combat
    assert combat is not None
    npc = Character(
        kind=NPC_KIND,
        name="Elder Marrow",
        location="Old Yard",
        description="A named elder.",
        personality="",
        accent="",
        quirks=[],
        important=True,
    )
    assert defeat_status(npc, combat.defeat) == "mortally wounded"
    assert defeat_status(replace(npc, important=False), combat.defeat) == "dead"


def test_a_future_defeat_label_is_loaded_not_dropped(tree: Path):
    _write(
        tree,
        {
            "rulesets/testset/stats.json": _stats(),
            "rulesets/testset/combat.json": [
                _combat(
                    defeat={
                        "player_character": "wounded",
                        "player_character_dead": "dead",
                        "important_npc": "captured",
                        "npc": "dead",
                        "faction": "routed",
                    }
                )
            ],
        },
    )
    combat = ContentLibrary.load(tree).rule_sets["testset"].combat
    assert combat is not None
    assert combat.defeat["faction"] == "routed"


def test_combat_defeat_still_needs_the_core_labels(tree: Path):
    _write(
        tree,
        {
            "rulesets/testset/stats.json": _stats(),
            "rulesets/testset/combat.json": [
                _combat(defeat={"player_character": "wounded", "faction": "routed"})
            ],
        },
    )
    with pytest.raises(ContentError, match="'npc' must be a non-empty string"):
        ContentLibrary.load(tree)


@pytest.mark.parametrize(
    ("files", "message"),
    [
        (
            {"rulesets/testset/rule_set.json": [{**_ruleset()[0], "typo": True}]},
            "rule_set: unknown key(s) 'typo'",
        ),
        (
            {
                "rulesets/testset/stats.json": _stats(),
                "rulesets/testset/combat.json": [_combat(typo=True)],
            },
            "combat test: unknown key(s) 'typo'",
        ),
        (
            {"rulesets/testset/module.json": _tool_call(typo=True)},
            "tool_call social: unknown key(s) 'typo'",
        ),
        (
            {
                "rulesets/testset/templates.json": [
                    {
                        "type": "template",
                        "id": "fighter",
                        "kind": "class",
                        "name": "Fighter",
                        "description": "A fighter.",
                        "typo": True,
                    }
                ]
            },
            "template fighter: unknown key(s) 'typo'",
        ),
        (
            {
                "rulesets/testset/features.json": [
                    {
                        "type": "feature",
                        "id": "sword",
                        "name": "Sword",
                        "feature_type": "item",
                        "typo": True,
                    }
                ]
            },
            "feature sword: unknown key(s) 'typo'",
        ),
    ],
)
def test_unknown_definition_keys_are_rejected(tree: Path, files: dict[str, Any], message: str):
    _write(tree, files)
    with pytest.raises(ContentError, match=re.escape(message)):
        ContentLibrary.load(tree)


def test_unknown_nested_character_keys_are_rejected(tree: Path):
    _write(
        tree,
        {
            "rulesets/testset/templates.json": [
                {
                    "type": "template",
                    "id": "fighter",
                    "kind": "class",
                    "name": "Fighter",
                    "description": "A fighter.",
                    "choose": {
                        "count": 1,
                        "feature_type": "item",
                        "tag_field": "tag",
                        "typo": True,
                    },
                }
            ]
        },
    )
    with pytest.raises(ContentError, match="choose: unknown key\\(s\\) 'typo'"):
        ContentLibrary.load(tree)


@pytest.mark.parametrize(
    "reaction, message",
    [
        (
            {
                "id": "gift",
                "trigger": "move",
                "effect": {"op": "raise_band", "to": "7-9"},
                "typo": True,
            },
            "reaction: unknown key(s) 'typo'",
        ),
        (
            {
                "id": "gift",
                "trigger": "move",
                "effect": {"op": "raise_band", "to": "7-9"},
                "uses": 1,
            },
            "reaction gift needs a 'reset' when it has 'uses'",
        ),
        (
            {
                "id": "gift",
                "trigger": "move",
                "effect": {"op": "set_reaction", "to": "friendly"},
            },
            "set_reaction only applies to a reaction roll",
        ),
        (
            {
                "id": "gift",
                "trigger": "reaction",
                "effect": {"op": "add_total", "amount": 0},
            },
            "add_total 'amount' must be a non-zero whole number",
        ),
        (
            {
                "id": "gift",
                "trigger": "attack",
                "effect": {"op": "add_total", "amount": 1},
            },
            "trigger must be one of move, check, reaction, save",
        ),
        (
            {
                "id": "gift",
                "trigger": "move",
                "when": {"move_tags": ["spell"]},
                "effect": {"op": "raise_band", "to": "7-9"},
            },
            "no move matches this reaction's 'when' filters",
        ),
    ],
)
def test_feature_reaction_errors_are_rejected(tree: Path, reaction: dict[str, Any], message: str):
    _write(
        tree,
        {
            "rulesets/testset/features.json": [
                {
                    "type": "feature",
                    "id": "talent",
                    "name": "Talent",
                    "feature_type": "trait",
                    "reactions": [reaction],
                }
            ]
        },
    )
    with pytest.raises(ContentError, match=re.escape(message)):
        ContentLibrary.load(tree)


def test_unknown_tool_player_input_keys_are_rejected(tree: Path):
    _write(
        tree,
        {
            "rulesets/testset/module.json": _tool_call(
                player_input={"dice": "1d20", "param": "roll", "typo": True},
            )
        },
    )
    with pytest.raises(ContentError, match=re.escape("player_input: unknown key(s) 'typo'")):
        ContentLibrary.load(tree)
