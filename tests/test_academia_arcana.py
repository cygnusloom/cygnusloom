"""Academia Arcana: content, creation, Year packages, year-end advancement, and the College Cup."""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Callable, Mapping

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusforge.content.character import CharacterSpec, Sheet, check_choices, resolve
from cygnusforge.content.library import RuleSet
from cygnusforge.services.events import Character
from tests.conftest import content_library
from tests.factories import (
    choose_campaign_options,
    create_campaign,
    create_character,
    open_campaign,
    signed_in_client,
    tools_for,
)

RULE_SET = "academia-arcana"
WORLD = "unwritten-academy"
NAME = "Wren"
SIGNATURE = "A lantern that burns only when someone needs it."
STAT_POOL = {"theory": 1, "practice": 2, "alchemy": 1, "charm": 0, "cunning": -1}

# The Year 1 picks the form submits, so the test knows the exact features the character holds.
YEAR_ONE_PICKS = {
    "pick-feature-year-1-spells-0": "light-step",
    "pick-feature-year-1-spells-1": "mending-word",
    "pick-feature-year-1-subjects-0": "subject-charms",
    "pick-feature-year-1-potions-0": "alertness-draught",
    "pick-feature-year-1-enchantments-0": "everlight",
}


def _academy() -> RuleSet:
    return content_library().rule_sets[RULE_SET]


def _submitted(year: str = "year-1") -> dict[str, object]:
    """The character a direct `check_choices`/`resolve` call receives, with the Year 1 picks
    fixed when the year is Year 1. Other years roll their picks."""
    submitted: dict[str, object] = {
        "stats": dict(STAT_POOL),
        "templates": {"playbook": "prodigy", "starting_year": year},
        "fields": {"major": "Charms", "signature-spell": SIGNATURE},
    }
    if year == "year-1":
        submitted["feature_picks"] = {
            "spells": ["light-step", "mending-word"],
            "subjects": ["subject-charms"],
            "potions": ["alertness-draught"],
            "enchantments": ["everlight"],
        }
    return submitted


def _sheet(rules: RuleSet, year: str) -> Sheet:
    submitted = _submitted(year)
    check_choices(rules.character, submitted)
    return resolve(rules.character, submitted, random.Random(7), plus=rules.plus)


def _creation_fields() -> dict[str, str]:
    fields = {f"stat-{stat}": str(value) for stat, value in STAT_POOL.items()}
    fields.update(
        {
            "field-major": "Charms",
            "field-signature-spell": SIGNATURE,
            **YEAR_ONE_PICKS,
        }
    )
    return fields


def _campaign(
    client_factory: Callable[[], TestClient], *, name: str = NAME
) -> tuple[TestClient, int]:
    """A signed-in client and a campaign with one Year 1 Prodigy made through the app."""
    client = signed_in_client(client_factory)
    campaign_id = create_campaign(client, world_template=WORLD, rule_set=RULE_SET)
    choose_campaign_options(client, campaign_id, RULE_SET)
    created = create_character(
        client,
        campaign_id,
        name=name,
        fields=_creation_fields(),
        templates={"playbook": "prodigy", "starting_year": "year-1"},
    )
    assert created.status_code == 303, created.text
    return client, campaign_id


def _pc(app: FastAPI, campaign_id: int) -> Character:
    character = open_campaign(app, campaign_id).pc()
    assert character is not None
    return character


def _features(spec: CharacterSpec, features: Mapping[str, int], feature_type: str) -> set[str]:
    return {feature for feature in features if spec.features[feature].feature_type == feature_type}


def test_the_ruleset_loads_its_academy_content() -> None:
    rules = _academy()
    assert list(rules.character.stats) == ["theory", "practice", "alchemy", "charm", "cunning"]
    assert rules.character.kinds() == ["playbook", "starting_year"]

    years = [
        template.id
        for template in rules.character.templates.values()
        if template.kind == "starting_year"
    ]
    playbooks = [
        template.id
        for template in rules.character.templates.values()
        if template.kind == "playbook"
    ]
    assert years == [f"year-{number}" for number in range(1, 8)]
    assert set(playbooks) == {"prodigy", "scholar", "alchemist", "rebel", "socialite"}

    assert len(rules.moves) == 17
    assert len(rules.conditions) == 9
    counts = Counter(feature.feature_type for feature in rules.character.features.values())
    assert counts["spell"] == 40
    assert counts["potion"] == 15
    assert counts["enchantment"] == 15

    cup = rules.tallies["college-cup"]
    assert cup.dynamic
    assert cup.label == "College Cup"

    assert rules.inventory is not None
    assert rules.inventory.capacity == 10
    assert rules.item_sizes == {"tiny": 0, "small": 1, "normal": 1, "bulky": 2}

    period = rules.periods["school-year"]
    assert period.tallies == ("college-cup",)
    assert any(
        effect.get("op") == "counter"
        and effect.get("id") == "advancements"
        and effect.get("delta") == 4
        for effect in period.effects
    )


def test_enchantments_are_workings_placed_on_objects() -> None:
    rules = _academy()
    workings = [
        feature
        for feature in rules.character.features.values()
        if feature.feature_type == "enchantment"
    ]
    assert len(workings) == 15
    assert all("place on" in feature.description.lower() for feature in workings)

    found = rules.content.dice_tables["magicschool_enchanted_object"]
    assert found.creative
    assert len(found.rows) == 15
    assert all(" with " in row.result.lower() for row in found.rows)


def test_character_creation_makes_a_year_one_prodigy(
    client_factory: Callable[[], TestClient],
) -> None:
    client, campaign_id = _campaign(client_factory)
    pc = _pc(client.app, campaign_id)
    rules = _academy()

    assert pc.name == NAME
    assert pc.templates == {"playbook": "prodigy", "starting_year": "year-1"}
    # The Prodigy's Practice bonus is added to the submitted score.
    assert {stat: pc.stats[stat] for stat in STAT_POOL} == {**STAT_POOL, "practice": 3}
    assert pc.counters["year"] == 1
    assert pc.fields["major"] == "Charms"
    assert pc.fields["signature-spell"] == SIGNATURE

    assert "talent-prodigy" in pc.features
    assert "signature-spell" in pc.features
    assert "subject-charms" in pc.features
    assert len(_features(rules.character, pc.features, "spell")) == 2
    assert _features(rules.character, pc.features, "potion")
    assert _features(rules.character, pc.features, "enchantment")


def test_a_year_seven_character_starts_with_the_larger_package() -> None:
    rules = _academy()
    spec = rules.character
    year_one = _sheet(rules, "year-1")
    year_seven = _sheet(rules, "year-7")

    assert year_one.counters["year"] == 1
    assert year_seven.counters["year"] == 7

    year_one_spells = _features(spec, year_one.features, "spell")
    year_seven_spells = _features(spec, year_seven.features, "spell")
    year_one_subjects = _features(spec, year_one.features, "subject")
    year_seven_subjects = _features(spec, year_seven.features, "subject")
    assert (len(year_one_spells), len(year_one_subjects)) == (2, 1)
    assert (len(year_seven_spells), len(year_seven_subjects)) == (8, 4)
    assert len(year_seven.features) > len(year_one.features)

    training = {
        feature for feature in year_seven.features if spec.features[feature].tag == "training-7"
    }
    assert len(training) == 3


def test_year_end_advances_the_year_and_grants_four_advancements(
    client_factory: Callable[[], TestClient],
) -> None:
    client, campaign_id = _campaign(client_factory)
    app = client.app
    tools = tools_for(app, campaign_id, rule_set=RULE_SET)
    assert _pc(app, campaign_id).counters["year"] == 1

    ended = tools.end_period("school-year")
    assert ended["period"] == "school year"

    advanced = _pc(app, campaign_id)
    assert advanced.counters["year"] == 2
    assert advanced.counters["advancements"] == 4

    assert tools.take_advancement(NAME, "stat", stat="theory")["remaining"] == 3
    assert tools.take_advancement(NAME, "spell", feature="tidy-room")["remaining"] == 2
    assert (
        tools.take_advancement(NAME, "subject", feature="subject-transfiguration")["remaining"] == 1
    )
    assert tools.take_advancement(NAME, "talent", feature="talent-scholar")["remaining"] == 0

    spent = _pc(app, campaign_id)
    assert spent.stats["theory"] == STAT_POOL["theory"] + 1
    assert {"tidy-room", "subject-transfiguration", "talent-scholar"} <= set(spent.features)
    assert spent.counters["advancements"] == 0


def test_the_college_cup_resets_at_year_end(
    client_factory: Callable[[], TestClient],
) -> None:
    client, campaign_id = _campaign(client_factory)
    tools = tools_for(client.app, campaign_id, rule_set=RULE_SET)

    tools.create_faction("House Ember", "A college of flame.", "Bold")
    tools.create_faction("House Tide", "A college of water.", "Patient")
    tools.adjust_faction_points("House Ember", 3, reason="a duel win")
    tools.adjust_faction_points("House Tide", 1, reason="a good answer")
    assert {faction["name"]: faction["points"] for faction in tools.list_factions()} == {
        "House Ember": 3,
        "House Tide": 1,
    }

    tools.end_period("school-year")

    after = tools.list_factions()
    assert {faction["name"] for faction in after} == {"House Ember", "House Tide"}
    assert all(faction["points"] == 0 for faction in after)


def test_a_found_enchanted_object_goes_into_the_pack(
    client_factory: Callable[[], TestClient],
) -> None:
    client, campaign_id = _campaign(client_factory)
    tools = tools_for(client.app, campaign_id, rule_set=RULE_SET)
    tools.create_location("The Stacks", "A library of bone tablets and wax boards.")
    tools.move_actor(NAME, "The Stacks")

    tools.place_object(
        "The Stacks",
        "A battered brass hand-lantern with Everlight worked into its glass.",
        False,
        "found behind a fallen shelf",
        name="brass hand-lantern",
    )
    taken = tools.take_object("brass hand-lantern", NAME, "she keeps it")

    assert taken["item"] == "brass hand-lantern"
    assert any(item.name == "brass hand-lantern" for item in _pc(client.app, campaign_id).items)


def test_creation_form_pluralizes_feature_picks(
    client_factory: Callable[[], TestClient],
) -> None:
    client = signed_in_client(client_factory)
    campaign_id = create_campaign(client, world_template=WORLD, rule_set=RULE_SET)
    choose_campaign_options(client, campaign_id, RULE_SET)

    page = client.get(f"/campaigns/{campaign_id}").text
    assert "Pick 2 potions" in page
    assert "Pick 1 enchantment" in page
    assert "Potionss" not in page
