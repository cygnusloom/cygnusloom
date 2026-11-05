from __future__ import annotations

import json
import random
from collections.abc import Callable
from dataclasses import replace
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import FormData

from cygnusloom.content.character import (
    FeatPick,
    TableOption,
    build_character,
    check_choices,
    resolve,
)
from cygnusloom.content.definitions import Definition
from cygnusloom.services.common import InvalidInput
from cygnusloom.web.character_form import creation_view, submission_from_form
from tests.test_campaigns import _new_campaign, _signed_in


def _template(ident: str, kind: str, **data) -> Definition:
    return Definition(
        "template",
        ident,
        {
            "type": "template",
            "id": ident,
            "kind": kind,
            "name": ident,
            "description": ident,
            **data,
        },
    )


def _generic_spec():
    return build_character(
        [
            _template(
                "dual",
                "role",
                template_requirements=[
                    {
                        "kinds": ["first_domain", "second_domain"],
                        "distinct": True,
                        "missing_message": "choose two domains",
                        "duplicate_message": "choose different domains",
                    }
                ],
            ),
            _template("single", "role"),
            _template("north", "culture"),
            _template("south", "culture"),
            _template(
                "fire_first",
                "first_domain",
                compatible_with={"role": ["dual"], "culture": ["north"]},
                choice_key="fire",
            ),
            _template("open_first", "first_domain"),
            _template(
                "water_second",
                "second_domain",
                compatible_with={"role": ["dual"], "culture": ["north"]},
                choice_key="water",
            ),
            _template(
                "fire_second",
                "second_domain",
                compatible_with={"role": ["dual"], "culture": ["north"]},
                choice_key="fire",
            ),
            _template("open_second", "second_domain"),
        ],
        {},
        {},
        "generic-form-test",
    )


def _reciprocal_spec():
    return build_character(
        [
            _template(
                "voyager",
                "profession",
                compatible_with={"heritage": ["coastal"]},
            ),
            _template(
                "scholar",
                "profession",
                compatible_with={"heritage": ["academy"]},
            ),
            _template(
                "coastal",
                "heritage",
                compatible_with={"profession": ["voyager"]},
            ),
            _template(
                "academy",
                "heritage",
                compatible_with={"profession": ["scholar"]},
            ),
        ],
        {},
        {},
        "reciprocal-form-test",
    )


def _skill_spec():
    return build_character(
        [
            Definition("skill", "lore", {"type": "skill", "id": "lore", "name": "Lore"}),
            Definition(
                "skill", "survival", {"type": "skill", "id": "survival", "name": "Survival"}
            ),
            Definition("skill", "craft", {"type": "skill", "id": "craft", "name": "Craft"}),
            _template(
                "adept",
                "calling",
                skills={"lore": 1},
                skill_picks=[
                    {"id": "basic_training", "count": 2, "table": "skills", "max_level": 0}
                ],
            ),
            _template(
                "settler",
                "heritage",
                skills={"lore": 1},
                skill_picks=[
                    {"id": "field_training", "count": 1, "table": "skills", "max_level": 1}
                ],
            ),
        ],
        {
            "skills": (
                TableOption(name=None, description="lore"),
                TableOption(name=None, description="survival"),
                TableOption(name=None, description="craft"),
            )
        },
        {"skills": "Skills"},
        "skill-form-test",
        skill_progression={
            "untrained": -1,
            "first_level": 0,
            "creation_maximum": 2,
            "maximum": 4,
        },
    )


def _generic_form(*, role: str = "dual", culture: str = "north") -> FormData:
    return FormData(
        [
            ("template-role", role),
            ("template-culture", culture),
            ("template-first_domain", "fire_first"),
            ("template-second_domain", "water_second"),
        ]
    )


def test_creation_view_exposes_generic_compatibility_requirements_and_choice_keys():
    spec = _generic_spec()
    view = creation_view(spec, {})
    templates = {
        template["id"]: template for kind in view["kinds"] for template in kind["templates"]
    }

    assert templates["fire_first"]["compatible_with"] == {
        "role": ["dual"],
        "culture": ["north"],
    }
    assert templates["fire_first"]["choice_key"] == "fire"
    assert templates["fire_second"]["choice_key"] == "fire"
    assert templates["open_first"]["choice_key"] == "open_first"
    assert templates["dual"]["template_requirements"] == [
        {
            "kinds": ["first_domain", "second_domain"],
            "distinct": True,
            "missing_message": "choose two domains",
            "duplicate_message": "choose different domains",
        }
    ]


def test_form_submission_accepts_non_class_gates_with_all_restrictions_satisfied():
    spec = _generic_spec()
    submitted = submission_from_form(spec, _generic_form())

    assert submitted["templates"] == {
        "culture": "north",
        "first_domain": "fire_first",
        "role": "dual",
        "second_domain": "water_second",
    }
    check_choices(spec, submitted)


@pytest.mark.parametrize(
    ("role", "culture"),
    [("dual", "south"), ("single", "north")],
)
def test_form_submission_rejects_a_template_when_any_gate_disallows_it(role: str, culture: str):
    with pytest.raises(InvalidInput, match="cannot be chosen"):
        submission_from_form(_generic_spec(), _generic_form(role=role, culture=culture))


def test_reciprocal_template_gates_allow_a_complete_pair_and_require_the_gate():
    spec = _reciprocal_spec()
    submitted = submission_from_form(
        spec,
        FormData([("template-profession", "voyager"), ("template-heritage", "coastal")]),
    )

    assert submitted["templates"] == {"heritage": "coastal", "profession": "voyager"}
    check_choices(spec, submitted)
    with pytest.raises(InvalidInput, match="Choose a heritage"):
        submission_from_form(spec, FormData([("template-profession", "voyager")]))


def test_unrequired_optional_template_kind_can_be_left_blank():
    spec = replace(
        build_character(
            [_template("warrior", "profession"), _template("mage", "tradition")],
            {},
            {},
            "optional-form-test",
        ),
        optional_kinds=("tradition",),
    )

    submitted = submission_from_form(
        spec,
        FormData([("template-profession", "warrior")]),
    )

    assert submitted["templates"] == {"profession": "warrior"}


def test_creation_view_exposes_skill_grants_progression_and_pick_caps():
    view = creation_view(_skill_spec(), {})
    templates = {
        template["id"]: template for kind in view["kinds"] for template in kind["templates"]
    }

    assert view["skill_progression"] == {"first_level": 0, "creation_maximum": 2}
    assert templates["adept"]["skill_grants"] == {"lore": 1}
    assert templates["adept"]["skill_picks"][0]["max_level"] == 0
    assert templates["settler"]["skill_grants"] == {"lore": 1}
    assert templates["settler"]["skill_picks"][0]["max_level"] == 1


def test_form_submission_preserves_manual_skill_picks_that_reach_each_cap():
    spec = _skill_spec()
    submitted = submission_from_form(
        spec,
        FormData(
            [
                ("template-calling", "adept"),
                ("template-heritage", "settler"),
                ("pick-skill-adept-basic_training-0", "survival"),
                ("pick-skill-adept-basic_training-1", "craft"),
                ("pick-skill-settler-field_training-0", "survival"),
            ]
        ),
    )

    assert submitted["skill_picks"] == {
        "basic_training": ["survival", "craft"],
        "field_training": ["survival"],
    }
    character = resolve(spec, submitted, random.Random(0), plus="sum")
    assert character.skills == {"lore": 1, "survival": 1, "craft": 0}


class _TemplateChoiceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.choice_data: dict[str, str | None] | None = None
        self.choices: list[dict[str, object]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "label":
            self.choice_data = (
                attributes if "choice" in (attributes.get("class") or "").split() else None
            )
        elif tag == "input" and self.choice_data and attributes.get("type") == "radio":
            self.choices.append(
                {
                    "name": attributes["name"],
                    "value": attributes["value"],
                    "kind": attributes.get("data-template-select"),
                    "choice_key": attributes.get("data-choice-key"),
                    "compatible_with": json.loads(
                        self.choice_data.get("data-compatible-with") or "{}"
                    ),
                    "requirements": json.loads(
                        self.choice_data.get("data-template-requirements") or "[]"
                    ),
                }
            )

    def handle_endtag(self, tag: str) -> None:
        if tag == "label":
            self.choice_data = None


class _SkillPickParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.form_attributes: dict[str, str | None] = {}
        self.grants: dict[tuple[str, str], dict[str, int]] = {}
        self.picks: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if tag == "form" and "creation-form" in classes:
            self.form_attributes = attributes
        if tag == "div" and "template-tables" in classes:
            self.grants[(attributes["data-template-group"], attributes["data-template"])] = (
                json.loads(attributes.get("data-skill-grants") or "{}")
            )
        if tag == "fieldset" and "skill-picks" in classes:
            self.picks.append(
                {
                    "template": attributes.get("data-pick-template"),
                    "id": attributes.get("data-pick-selects"),
                    "max_level": attributes.get("data-pick-max-level"),
                }
            )


@pytest.mark.parametrize(
    ("world_template", "rule_set"),
    [
        ("twin-lakes", "cairn2e"),
        ("unwritten-world", "worldswithoutnumber"),
        ("unwritten-academy", "academia-arcana"),
    ],
)
def test_creation_page_round_trips_template_metadata_for_each_ruleset(
    client_factory: Callable[[], TestClient], world_template: str, rule_set: str
):
    client = _signed_in(client_factory)
    response = _new_campaign(client, world_template=world_template, rule_set=rule_set)
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/campaigns/{campaign_id}").text

    parser = _TemplateChoiceParser()
    parser.feed(page)
    choices = {
        (choice["name"], choice["value"]): choice
        for choice in parser.choices
        if choice["kind"] is not None
    }
    spec = client.app.state.content.rule_sets[rule_set].character

    for template in spec.templates.values():
        choice = choices[("template-" + template.kind, template.id)]
        assert choice["compatible_with"] == {
            kind: list(ids) for kind, ids in template.compatible_with.items()
        }
        assert choice["requirements"] == [
            {
                "kinds": list(requirement.kinds),
                "distinct": requirement.distinct,
                "missing_message": requirement.missing_message,
                "duplicate_message": requirement.duplicate_message,
            }
            for requirement in template.template_requirements
        ]
        assert choice["choice_key"] == template.choice_key


def test_wwn_mage_mage_form_exposes_distinct_partial_traditions(
    client_factory: Callable[[], TestClient],
):
    client = _signed_in(client_factory)
    response = _new_campaign(
        client, world_template="unwritten-world", rule_set="worldswithoutnumber"
    )
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/campaigns/{campaign_id}").text

    parser = _TemplateChoiceParser()
    parser.feed(page)
    choices = {
        (choice["name"], choice["value"]): choice
        for choice in parser.choices
        if choice["kind"] is not None
    }
    mage = choices[("template-class", "adventurer_mage_mage")]
    first = choices[("template-tradition", "high_mage_partial")]
    second = choices[("template-tradition2", "high_mage_partial_2")]

    assert mage["requirements"] == [
        {
            "kinds": ["tradition", "tradition2"],
            "distinct": True,
            "missing_message": "a Mage/Mage Adventurer must choose two partial traditions",
            "duplicate_message": "a Mage/Mage Adventurer must choose two different traditions",
        }
    ]
    assert first["compatible_with"]["class"] == [
        "healer",
        "vowed",
        "adventurer_expert_mage",
        "adventurer_warrior_mage",
        "adventurer_mage_mage",
    ]
    assert first["choice_key"] == second["choice_key"] == "high_mage"
    assert 'data-choice-panel="template-tradition"' in page
    assert 'data-choice-panel="template-tradition2"' in page


def test_wwn_healer_and_vowed_form_requires_a_second_partial_class(
    client_factory: Callable[[], TestClient],
):
    client = _signed_in(client_factory)
    response = _new_campaign(
        client, world_template="unwritten-world", rule_set="worldswithoutnumber"
    )
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/campaigns/{campaign_id}").text

    parser = _TemplateChoiceParser()
    parser.feed(page)
    choices = {
        (choice["name"], choice["value"]): choice
        for choice in parser.choices
        if choice["kind"] is not None
    }
    healer = choices[("template-class", "healer")]
    vowed = choices[("template-class", "vowed")]
    warrior = choices[("template-class2", "warrior_partial_2")]
    mage = choices[("template-class2", "mage_partial_2")]

    for partial_class in (healer, vowed):
        assert partial_class["requirements"] == [
            {
                "kinds": ["class", "class2"],
                "distinct": True,
                "missing_message": f"a {partial_class['value'].capitalize()} "
                "must pair with another partial class",
                "duplicate_message": f"a {partial_class['value'].capitalize()} "
                "must pair with a different partial class",
            }
        ]
    assert warrior["compatible_with"] == {
        "class": ["healer", "vowed"],
        "tradition": ["no_tradition"],
    }
    assert mage["compatible_with"]["tradition"] == [
        "high_mage_partial",
        "necromancer_partial",
        "elementalist_partial",
    ]
    assert warrior["choice_key"] == "warrior"
    assert mage["choice_key"] == "mage"
    assert 'data-choice-panel="template-class2"' in page
    assert '<h3 id="panel-kind-class2">Second Class</h3>' in page


def test_wwn_skill_pick_form_exposes_progression_grants_and_caps(
    client_factory: Callable[[], TestClient],
):
    client = _signed_in(client_factory)
    response = _new_campaign(
        client, world_template="unwritten-world", rule_set="worldswithoutnumber"
    )
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/campaigns/{campaign_id}").text

    parser = _SkillPickParser()
    parser.feed(page)
    spec = client.app.state.content.rule_sets["worldswithoutnumber"].character

    assert spec.skill_progression is not None
    assert parser.form_attributes["data-skill-first-level"] == str(
        spec.skill_progression.first_level
    )
    assert parser.form_attributes["data-skill-creation-maximum"] == str(
        spec.skill_progression.creation_maximum
    )
    assert parser.grants == {
        (template.kind, template.id): dict(template.skills) for template in spec.templates.values()
    }
    assert parser.picks == [
        {
            "template": template.id,
            "id": pick.id,
            "max_level": str(pick.max_level),
        }
        for kind in spec.kinds()
        for template in spec.templates.values()
        if template.kind == kind
        for pick in template.skill_picks
    ]


@pytest.mark.parametrize(
    ("tag", "feature_type", "label"),
    [("focus", "", "focus"), ("", "spell", "spell"), ("", "", "feature")],
)
def test_feature_pick_labels_use_the_configured_kind_or_a_generic_fallback(
    tag: str, feature_type: str, label: str
):
    spec = _skill_spec()
    template = spec.templates["adept"]
    pick = FeatPick(id="choice", count=2, tag=tag, feature_type=feature_type)
    spec = replace(
        spec,
        templates={**spec.templates, template.id: replace(template, feat_picks=(pick,))},
    )
    view = creation_view(spec, {})
    calling = next(group for group in view["kinds"] if group["kind"] == "calling")
    adept = next(item for item in calling["templates"] if item["id"] == "adept")

    assert adept["feat_picks"][0]["tag"] == label
    assert adept["feat_picks"][0]["hint"].startswith(f"Pick 2 {label}s")


def test_wwn_multitag_spell_picks_are_labeled_in_the_creation_form(
    client_factory: Callable[[], TestClient],
):
    client = _signed_in(client_factory)
    response = _new_campaign(
        client, world_template="unwritten-world", rule_set="worldswithoutnumber"
    )
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/campaigns/{campaign_id}").text
    picks = page.split('data-pick-selects="tradition_spells_elementalist"', 1)[1]
    picks = picks.split("</fieldset>", 1)[0]

    assert "<h3>Spell</h3>" in picks
    assert "Randomize spell" in picks
    assert "Pick 4 spells of rank 1 or less" in picks
    assert "Spell 1" in picks
