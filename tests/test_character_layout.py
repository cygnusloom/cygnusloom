"""The generic creation and advancement layouts, and the ready-made character presets."""

from __future__ import annotations

import random

import pytest
from starlette.datastructures import FormData

from cygnusloom.content.character import character_schema, resolve, roll_stats
from cygnusloom.services.tools import check_arguments
from cygnusloom.web.character_form import creation_view, submission_from_form
from tests.conftest import bundled_library

RULE_SETS = ("dnd-srd-2024", "worldswithoutnumber", "cairn2e", "academia-arcana")


def _rolled_stats(spec, rule_set) -> dict[str, int]:
    """A legal score for every non-derived stat, including hidden and assigned stats."""
    rolled = roll_stats(spec, random.Random(11), plus=rule_set.plus)
    for stat in spec.stats.values():
        if stat.values:
            rolled[stat.id] = stat.values[0]
    return rolled


def _preset_form(spec, rule_set, preset) -> FormData:
    """The form a preset stands for: the same names a player's submission uses."""
    rolled = _rolled_stats(spec, rule_set)
    entries: list[tuple[str, str]] = [
        ("name", preset.suggested_name or "Ready-made"),
        ("pronouns", preset.pronouns or "they/them"),
    ]
    entries.extend((f"stat-{stat}", str(value)) for stat, value in rolled.items())
    entries.extend((f"template-{kind}", template) for kind, template in preset.templates.items())
    entries.extend((f"field-{field}", value) for field, value in preset.fields.items())
    entries.extend((f"field-{table}", value) for table, value in preset.tables.items())
    entries.extend((f"grant-{template}", option) for template, option in preset.grants.items())
    for template_id in preset.templates.values():
        template = spec.templates[template_id]
        for pick in template.skill_picks:
            for index, value in enumerate(preset.picks.get(pick.id, ())):
                entries.append((f"pick-skill-{template_id}-{pick.id}-{index}", value))
        for pick in template.feat_picks:
            for index, value in enumerate(preset.picks.get(pick.id, ())):
                entries.append((f"pick-feature-{template_id}-{pick.id}-{index}", value))
    for key, values in preset.feature_choices.items():
        base = f"feature-choice-{key.replace(':', '-')}"
        if key.endswith(":pattern"):
            entries.append((base, values[0]))
        else:
            entries.extend((f"{base}-{index}", value) for index, value in enumerate(values))
    return FormData(entries)


@pytest.mark.parametrize("rule_set_id", RULE_SETS)
def test_a_ready_made_character_builds_without_a_manual_choice(rule_set_id: str) -> None:
    rule_set = bundled_library().rule_sets[rule_set_id]
    spec = rule_set.character
    for preset in spec.presets.values():
        submitted = submission_from_form(spec, _preset_form(spec, rule_set, preset))
        problem = check_arguments(character_schema(spec), submitted)
        assert problem is None, (preset.id, problem)
        sheet = resolve(spec, submitted, random.Random(3), plus=rule_set.plus)
        assert sheet.stats, preset.id
        assert sheet.fields, preset.id


def test_a_respec_hides_identity_and_flavor_panels() -> None:
    rule_set = bundled_library().rule_sets["worldswithoutnumber"]
    spec = rule_set.character
    full = creation_view(spec, _rolled_stats(spec, rule_set))
    respec = creation_view(spec, _rolled_stats(spec, rule_set), respec=True)
    full_panels = {panel["id"] for section in full["sections"] for panel in section["panels"]}
    respec_panels = {panel["id"] for section in respec["sections"] for panel in section["panels"]}
    assert {"name", "pronouns"} <= full_panels
    assert not ({"name", "pronouns"} & respec_panels)
    assert "field-look" in full_panels
    assert "field-look" not in respec_panels
    assert "stats" in respec_panels
