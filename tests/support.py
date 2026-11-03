"""Builders for a content tree a loader test owns.

The shared library is the app's bundled content plus the test fixtures, so a test about one
definition, or about one broken definition, writes a tree of its own and loads it with
``ContentLibrary.load``. These helpers write the least content that loads: a world with one
place, the tables every rule set expects from core, and one rule set to add to.

The point is to assert what the public ``RuleSet`` carries back, rather than calling a
``_load_*`` function directly.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

DEFAULT_WORLD = "testland"
DEFAULT_RULE_SET = "testset"

# A world's NPCs draw traits from these tables, so every core rule set needs them.
NPC_TABLES = ("personality", "accent", "quirk")


def write_files(root: Path, files: Mapping[str, Any]) -> None:
    """Write each relative path under ``root``; a mapping or list is dumped as JSON."""
    for relative, payload in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        path.write_text(text, encoding="utf-8")


def world_definitions(name: str = "Testland", ident: str = DEFAULT_WORLD) -> list[dict[str, Any]]:
    """A world with a single place, which is all a loader test needs."""
    return [
        {"type": "world", "id": ident, "name": name, "description": "A test world."},
        {"type": "location", "id": "town", "name": "Town", "description": "A small town."},
    ]


def core_definitions() -> list[dict[str, Any]]:
    """The core rule set's own definition and the tables every rule set expects."""
    return [
        {"type": "rule_set", "id": "core", "name": "Core", "description": "Core."},
        *({"type": "table", "id": table, "options": ["one"]} for table in NPC_TABLES),
    ]


def rule_set_definition(ident: str = DEFAULT_RULE_SET, **overrides: Any) -> dict[str, Any]:
    """A rule set definition, with the given keys replacing or adding to the defaults."""
    definition: dict[str, Any] = {
        "type": "rule_set",
        "id": ident,
        "name": "Test set",
        "description": "For tests.",
    }
    definition.update(overrides)
    return definition


def content_tree(
    root: Path,
    *,
    rule_set: Mapping[str, Any] | None = None,
    definitions: Sequence[Mapping[str, Any]] = (),
    core: Sequence[Mapping[str, Any]] = (),
    world: Sequence[Mapping[str, Any]] | None = None,
) -> Path:
    """Write a minimal valid content tree and return its root.

    ``rule_set`` replaces keys of the rule set definition itself, such as ``condition_ends``.
    ``definitions`` stand beside it, such as a ``rest``, ``dice_table``, or ``creative_table``.
    ``core`` adds definitions to the core rule set that every rule set is built on, and
    ``world`` replaces the default world definitions.
    """
    write_files(
        root,
        {
            "worlds/testland/world.json": list(world or world_definitions()),
            "rulesets/core/rule_set.json": [*core_definitions(), *core],
            "rulesets/testset/rule_set.json": [
                rule_set_definition(**dict(rule_set or {})),
                *definitions,
            ],
        },
    )
    return root
