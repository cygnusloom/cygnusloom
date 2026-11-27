"""World creation belongs to every rule set, not only Worlds Without Number.

The tables that build a setting, and the instructions that tell the GM to invent one, live in
the `core` rule set so each rule set inherits them. The setting instructions stay off for a world
that already carries a setting; only a blank world such as `unwritten-world` gets them.
"""

from __future__ import annotations

import pytest

from cygnusloom.services.prompts import state_instruction
from tests.conftest import content_library

BUNDLED_RULE_SETS = ("academia-arcana", "cairn2e", "dnd-srd-2024", "worldswithoutnumber")
SETTING_FORGE = "There is no published setting"


@pytest.mark.parametrize("rule_set", BUNDLED_RULE_SETS)
def test_exactly_one_world_forge_runs_for_the_unwritten_world(rule_set: str):
    library = content_library()
    rules = library.rule_sets[rule_set]
    prompt = state_instruction(rules, library.world_templates["unwritten-world"])
    core_forge = next(
        fragment for fragment in library.core.prompt_fragments if SETTING_FORGE in fragment.text
    )
    replacements = [
        fragment.text for fragment in rules.prompt_fragments if core_forge.id in fragment.supersedes
    ]
    if replacements:
        assert core_forge.text not in prompt
        assert all(text in prompt for text in replacements)
    else:
        assert core_forge.text in prompt


def test_the_academy_forge_replaces_the_core_setting_forge():
    library = content_library()
    academy = library.rule_sets["academia-arcana"]
    prompt = state_instruction(academy, library.world_templates["unwritten-world"])
    core_forge = next(
        fragment for fragment in library.core.prompt_fragments if SETTING_FORGE in fragment.text
    )
    forge = next(
        fragment for fragment in academy.prompt_fragments if core_forge.id in fragment.supersedes
    )
    assert forge.text in prompt
    assert core_forge.text not in prompt
    # Only the forge is replaced; the rest of the core setting instructions still run.
    continuity = next(
        fragment
        for fragment in library.core.prompt_fragments
        if fragment.id != core_forge.id and fragment.unwritten_only
    )
    assert [fragment.id for fragment in academy.prompt_fragments].count(continuity.id) == 1
    assert continuity.text in prompt


def test_the_hordes_opening_replaces_the_core_setting_forge():
    library = content_library()
    rules = library.rule_sets["hordes"]
    prompt = state_instruction(rules, library.world_templates["unwritten-world"])
    core_forge = next(
        fragment for fragment in library.core.prompt_fragments if SETTING_FORGE in fragment.text
    )
    forge = next(
        fragment for fragment in rules.prompt_fragments if fragment.id == "first_winter_opening"
    )
    assert core_forge.id in forge.supersedes
    assert forge.text in prompt
    assert core_forge.text not in prompt


@pytest.mark.parametrize(
    ("world", "rule_set"),
    [
        ("testhollow", "rolltest"),
        ("twin-lakes", "cairn2e"),
    ],
)
def test_a_world_with_a_setting_gets_no_setting_forge(world: str, rule_set: str):
    library = content_library()
    template = library.setting_template(world)
    assert template.has_setting
    assert SETTING_FORGE not in state_instruction(library.rule_sets[rule_set], template)
