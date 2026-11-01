from __future__ import annotations

import json
from collections.abc import Callable
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import FormData

from cygnusforge.content.character import build_character, check_choices
from cygnusforge.content.definitions import Definition
from cygnusforge.services.common import InvalidInput
from cygnusforge.web.character_form import creation_view, submission_from_form
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
        template["id"]: template
        for kind in view["kinds"]
        for template in kind["templates"]
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
def test_form_submission_rejects_a_template_when_any_gate_disallows_it(
    role: str, culture: str
):
    with pytest.raises(InvalidInput, match="cannot be chosen"):
        submission_from_form(_generic_spec(), _generic_form(role=role, culture=culture))


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


@pytest.mark.parametrize(
    ("world_template", "rule_set"),
    [
        ("twin-lakes", "cairn2e"),
        ("hogwarts", "hogwarts-rpg"),
        ("latter-earth", "worldswithoutnumber"),
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
        client, world_template="latter-earth", rule_set="worldswithoutnumber"
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
        "adventurer_expert_mage",
        "adventurer_warrior_mage",
        "adventurer_mage_mage",
    ]
    assert first["choice_key"] == second["choice_key"] == "high_mage"
    assert 'data-choice-panel="template-tradition"' in page
    assert 'data-choice-panel="template-tradition2"' in page
