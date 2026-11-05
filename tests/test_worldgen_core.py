"""World creation belongs to every rule set, not only Worlds Without Number.

The tables that build a setting, and the instructions that tell the GM to invent one, live in
the `core` rule set so each rule set inherits them. The setting instructions stay off for a world
that already carries a setting; only a blank world such as `unwritten-world` gets them.
"""

from __future__ import annotations

import json

import pytest

from cygnusforge.content.library import DATA_DIR, ContentLibrary
from cygnusforge.services.prompts import state_instruction
from tests.conftest import content_library

BUNDLED_RULE_SETS = ("cairn2e", "hogwarts-rpg", "worldswithoutnumber")
SETTING_FORGE = "There is no published setting"


def _own_table_ids(rule_set: str) -> set[str]:
    """The ids of the dice and creative tables a rule set's own files define."""
    ids: set[str] = set()
    for path in sorted((DATA_DIR / "rulesets" / rule_set).glob("*.json")):
        for definition in json.loads(path.read_text(encoding="utf-8")):
            if definition.get("type") in ("dice_table", "creative_table"):
                ids.add(definition["id"])
    return ids


def test_core_carries_the_world_creation_tables():
    tables = ContentLibrary.load().core.dice_tables
    assert {"significant_terrain", "dungeon_reaction", "village_rationale"} <= set(tables)
    assert "setting_premise" in tables
    assert tables["setting_premise"].creative


@pytest.mark.parametrize("rule_set", BUNDLED_RULE_SETS)
def test_every_bundled_ruleset_inherits_the_core_worldgen_tables(rule_set: str):
    rules = ContentLibrary.load().rule_sets[rule_set]
    assert "significant_terrain" in rules.dice_tables
    assert "setting_premise" in rules.dice_tables
    assert "significant_terrain" not in _own_table_ids(rule_set)


def test_core_carries_the_setting_instructions_for_a_blank_world():
    core = ContentLibrary.load().core.prompt_fragments
    assert [fragment.id for fragment in core] == ["setting_forge", "setting_continuity"]
    assert all(fragment.unwritten_only for fragment in core)


def test_the_unwritten_world_plays_with_any_rule_set():
    library = content_library()
    world = library.world_templates["unwritten-world"]
    assert world.rule_set is None
    assert not world.has_setting
    for rule_set in ("cairn2e", "hogwarts-rpg", "rolltest"):
        content = library.world_content("unwritten-world", rule_set)
        assert content.monsters == {}
        assert content.dice_tables == {}


@pytest.mark.parametrize("rule_set", BUNDLED_RULE_SETS)
def test_the_setting_forge_runs_for_the_unwritten_world(rule_set: str):
    library = content_library()
    prompt = state_instruction(
        library.rule_sets[rule_set], library.world_templates["unwritten-world"]
    )
    assert SETTING_FORGE in prompt


@pytest.mark.parametrize(
    ("world", "rule_set"),
    [
        ("testhollow", "rolltest"),
        ("twin-lakes", "cairn2e"),
        ("hogwarts", "hogwarts-rpg"),
    ],
)
def test_a_world_with_a_setting_gets_no_setting_forge(world: str, rule_set: str):
    library = content_library()
    template = library.world_templates[world]
    assert template.has_setting
    assert SETTING_FORGE not in state_instruction(library.rule_sets[rule_set], template)
