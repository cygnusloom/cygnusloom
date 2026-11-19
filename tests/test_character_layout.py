"""The generic creation and advancement layouts, and the ready-made character presets."""

from __future__ import annotations

import json
import random

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import FormData

from cygnusloom.content.character import character_schema, resolve
from cygnusloom.rules.dice import roll_total
from cygnusloom.services.tools import check_arguments
from cygnusloom.web.app import create_app
from cygnusloom.web.character_form import creation_view, submission_from_form
from tests.conftest import bundled_library
from tests.factories import create_campaign_direct, signed_in_client

RULE_SETS = ("dnd-srd-2024", "worldswithoutnumber", "cairn2e", "academia-arcana")


def _rolled_stats(spec, rule_set) -> dict[str, int]:
    """A legal score for every non-derived stat, including hidden and assigned stats."""
    rng = random.Random(11)
    rolled: dict[str, int] = {}
    for stat in spec.stats.values():
        if stat.derived is not None:
            continue
        if stat.formula is not None:
            rolled[stat.id] = roll_total(stat.formula, rng, plus=rule_set.plus)
        elif stat.values:
            rolled[stat.id] = stat.values[0]
        else:
            rolled[stat.id] = 0
    return rolled


def _preset_form(spec, rule_set, preset) -> FormData:
    """The form a preset stands for: the same names a player's submission uses."""
    rolled = _rolled_stats(spec, rule_set)
    entries: list[tuple[str, str]] = [
        ("name", preset.suggested_name or "Ready-made"),
        ("pronouns", "they/them"),
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


def test_every_shipped_rule_set_has_a_complete_creation_layout() -> None:
    library = bundled_library()
    for rule_set_id in RULE_SETS:
        rule_set = library.rule_sets[rule_set_id]
        spec = rule_set.character
        view = creation_view(spec, _rolled_stats(spec, rule_set))
        panels = [panel["id"] for section in view["sections"] for panel in section["panels"]]
        assert len(panels) == len(set(panels)), rule_set_id
        expected = {
            "name",
            "pronouns",
            "stats",
            *{f"template-{kind}" for kind in spec.kinds()},
            *{f"field-{field_id}" for field_id in spec.fields},
        }
        if view["feature_choices"]:
            expected.add("feature-choices")
        if view["skill_replacements"]:
            expected.add("skill-replacements")
        assert set(panels) == expected, rule_set_id
        assert view["sections"][0]["id"] == "identity", rule_set_id


def test_every_shipped_rule_set_offers_three_archetypes() -> None:
    library = bundled_library()
    for rule_set_id in RULE_SETS:
        presets = library.rule_sets[rule_set_id].character.presets
        assert {preset.role for preset in presets.values()} == {
            "warrior",
            "magic",
            "support",
        }, rule_set_id
        assert len(presets) == 3, rule_set_id
        assert all(preset.templates for preset in presets.values()), rule_set_id


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


def test_advancement_layouts_name_real_picks() -> None:
    library = bundled_library()
    for rule_set_id in RULE_SETS:
        rule_set = library.rule_sets[rule_set_id]
        layout = rule_set.character.layouts.get("advancement")
        if layout is None:
            continue
        known = set(rule_set.progression.level_ups["level"].picks)
        for section in layout.sections:
            assert set(section.items) <= known, (rule_set_id, section.id)


def test_the_creation_page_groups_panels_and_offers_ready_made_characters(settings, fake) -> None:
    app = create_app(settings, fake_provider=fake, content=bundled_library())
    with TestClient(app) as client:
        signed_in = signed_in_client(lambda: client)
        campaign_id = create_campaign_direct(
            signed_in, world_template="unwritten-world", rule_set="dnd-srd-2024"
        )
        page = signed_in.get(f"/campaigns/{campaign_id}").text

    # Campaign options stay on the page; only the big optional panels start collapsed.
    assert page.index("Campaign options") < page.index('name="setting"')
    assert '<details class="optional-options">' in page
    assert '<summary id="panel-setting">Setting / story (optional)</summary>' in page
    assert "Use character template" in page
    assert 'id="dialog-character-templates"' in page
    # The 2024 SRD order: class first, then origin, then ability scores.
    assert page.index('id="category-class"') < page.index('id="category-origin"')
    assert page.index('id="category-origin"') < page.index('id="category-abilities"')
    assert page.count("data-character-template-data") == 3
    for name in ("The Paladin", "The Wizard", "The Cleric"):
        assert name in page
    payloads = [
        json.loads(segment.split(">", 1)[1].split("</script>", 1)[0])
        for segment in page.split('<script type="application/json" data-character-template-data=')[
            1:
        ]
    ]
    assert all(payload["templates"] for payload in payloads)
    assert {payload["templates"].get("class") for payload in payloads} == {
        "paladin",
        "wizard",
        "cleric",
    }


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
