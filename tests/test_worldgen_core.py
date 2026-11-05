"""World creation belongs to every rule set, not only Worlds Without Number.

The tables that build a setting, and the instructions that tell the GM to invent one, live in
the `core` rule set so each rule set inherits them. The setting instructions stay off for a world
that already carries a setting; only a blank world such as `unwritten-world` gets them.
"""

from __future__ import annotations

import json

import pytest

from cygnusloom.content.library import DATA_DIR, ContentLibrary
from cygnusloom.services.prompts import state_instruction
from tests.conftest import content_library

BUNDLED_RULE_SETS = ("academia-arcana", "cairn2e", "worldswithoutnumber")
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
    assert [fragment.id for fragment in core] == [
        "setting_forge",
        "setting_continuity",
        "local_maps",
    ]
    assert all(fragment.unwritten_only for fragment in core if fragment.id != "local_maps")


def test_a_generic_only_rule_set_leaves_the_fantasy_tables_out():
    library = content_library()
    rules = library.rule_sets["rolltest"]
    assert rules.table_genres == ("generic",)
    assert "action" in rules.dice_tables
    assert "setting_premise" not in rules.dice_tables
    # The sourcebook keeps every table even when a rule set's choice leaves one out.
    assert "setting_premise" in library.core.dice_tables


def test_the_setting_forge_names_no_table_a_genre_choice_could_hide():
    core = ContentLibrary.load(DATA_DIR).core
    prompt = next(
        fragment.text for fragment in core.prompt_fragments if fragment.id == "setting_forge"
    )
    assert SETTING_FORGE in prompt
    assert "Roll every creative table with roll_creative" in prompt
    assert "the selected genres offer" in prompt
    assert "significant_terrain" not in prompt


def test_the_unwritten_world_plays_with_any_rule_set():
    library = content_library()
    world = library.world_templates["unwritten-world"]
    assert world.rule_set is None
    assert not world.has_setting
    for rule_set in ("academia-arcana", "cairn2e", "worldswithoutnumber", "rolltest"):
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
    ],
)
def test_a_world_with_a_setting_gets_no_setting_forge(world: str, rule_set: str):
    library = content_library()
    template = library.world_templates[world]
    assert template.has_setting
    assert SETTING_FORGE not in state_instruction(library.rule_sets[rule_set], template)
