"""Academia Arcana: content, creation, Year packages, year-end advancement, and the College Cup."""

from __future__ import annotations

import asyncio
import json
import random
import re
from collections import Counter
from collections.abc import Callable, Mapping
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusloom.content.character import (
    CharacterError,
    CharacterSpec,
    Sheet,
    check_choices,
    resolve,
)
from cygnusloom.content.library import RuleSet
from cygnusloom.services import level_up as level_up_service
from cygnusloom.services import turns as turns_service
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import Character, MoveChoiceMade, MoveMade, decode, events_since
from cygnusloom.services.tools import Ask, RollAnswer, ToolRequest, run_tool
from tests.conftest import content_library
from tests.factories import (
    answer,
    choose_campaign_options,
    create_campaign,
    create_character,
    db,
    open_campaign,
    send_message,
    signed_in_client,
    tools_for,
)
from tests.test_tools import _script

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
    client_factory: Callable[[], TestClient], *, name: str = NAME, major: str = "Charms"
) -> tuple[TestClient, int]:
    """A signed-in client and a campaign with one Year 1 Prodigy made through the app."""
    client = signed_in_client(client_factory)
    campaign_id = create_campaign(client, world_template=WORLD, rule_set=RULE_SET)
    choose_campaign_options(client, campaign_id, RULE_SET)
    fields = _creation_fields()
    fields["field-major"] = major
    created = create_character(
        client,
        campaign_id,
        name=name,
        fields=fields,
        templates={"playbook": "prodigy", "starting_year": "year-1"},
    )
    assert created.status_code == 303, created.text
    return client, campaign_id


def _pc(app: FastAPI, campaign_id: int) -> Character:
    character = open_campaign(app, campaign_id).pc()
    assert character is not None
    return character


def _advancement_page(app: FastAPI, campaign_id: int):
    return level_up_service.page_view(
        app.state.engine,
        campaign_id,
        _academy(),
        content_library().world_content(WORLD, RULE_SET),
        NAME,
    )


def test_natural_gift_raises_a_spell_roll_once_per_week(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _client, campaign_id = _campaign(client_factory)
    tools = tools_for(app, campaign_id, rule_set=RULE_SET)

    first = tools.roll_move("cast_a_spell", NAME, roll=3)
    assert first["band"] == "7-9"
    assert "Natural Gift" in str(first["reaction"])
    assert _pc(app, campaign_id).reaction_uses == {"week:talent-prodigy:natural-gift": 1}

    second = tools.roll_move("cast_a_spell", NAME, roll=3)
    assert second["band"] == "6-"
    assert "reaction" not in second

    tools.end_period("school-week")
    assert _pc(app, campaign_id).reaction_uses == {}
    third = tools.roll_move("cast_a_spell", NAME, roll=3)
    assert third["band"] == "7-9"


def test_the_signature_spell_reaction_wins_on_a_signature_move(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _client, campaign_id = _campaign(client_factory)
    tools = tools_for(app, campaign_id, rule_set=RULE_SET)

    result = tools.roll_move("signature_spell", NAME, roll=3)

    assert result["band"] == "7-9"
    assert "Signature Spell" in str(result["reaction"])
    assert _pc(app, campaign_id).reaction_uses == {"week:signature-spell:signature-spell": 1}


def test_academia_move_prompts_for_the_players_own_dice(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    """The player rolls their own 2d6 for a move; the engine adds the stat and resolves the band."""
    _client, campaign_id = _campaign(client_factory)
    tools = tools_for(app, campaign_id, rule_set=RULE_SET)
    ctx = SimpleNamespace(world=tools)
    request = ToolRequest(
        id="call_1",
        name="roll_move",
        arguments=json.dumps(
            {
                "prompt": "Roll to cast your spell.",
                "move": "cast_a_spell",
                "character": NAME,
            }
        ),
    )

    ask = asyncio.run(run_tool(_academy().tool_calls, request, None, ctx=ctx))
    assert isinstance(ask, Ask)
    assert ask.dice == "2d6"
    assert "stat" in (ask.details or "")

    run = asyncio.run(run_tool(_academy().tool_calls, request, RollAnswer(8), ctx=ctx))
    assert not isinstance(run, Ask)
    result = json.loads(run.output)
    assert result["roll"] == 8
    assert result["total"] == 8 + result["bonus"]


def test_the_move_prompt_leaves_condition_penalties_to_the_engine() -> None:
    """The GM adds only story modifiers; the engine already counts every condition once."""
    call = _academy().tool_calls["roll_move"]
    modifier = call.parameters["properties"]["modifier"]["description"]
    assert "engine adds" in call.description
    assert "do not count a condition" in call.description
    assert "not counting conditions" in modifier


def test_a_seven_to_nine_spell_asks_the_player_to_choose(
    client_factory: Callable[[], TestClient], app: FastAPI, monkeypatch: pytest.MonkeyPatch
):
    """A 7-9 spell waits for the player's pick; the choice applies its effects once."""
    client, campaign_id = _campaign(client_factory)
    answer(client, campaign_id, "continue")
    script = _script(
        {
            "rounds": [
                [
                    {
                        "name": "roll_move",
                        "arguments": {
                            "prompt": "Roll to cast your spell.",
                            "move": "cast_a_spell",
                            "character": NAME,
                        },
                    }
                ]
            ],
            "text": "The spell takes hold.",
        }
    )
    assert send_message(client, campaign_id, script).status_code == 303
    assert _pc(app, campaign_id).counters.get("stress", 0) == 0

    # Practice is +2, so a reported 5 reaches the 7-9 band.
    monkeypatch.setattr(turns_service, "roll_values", lambda *args, **kwargs: ([5], 5))
    events = answer(client, campaign_id, "roll")

    assert events[-1]["type"] == "prompt"
    turn = open_campaign(app, campaign_id).turn
    assert turn is not None and turn.prompt is not None
    assert [
        str(choice["id"]) if isinstance(choice, Mapping) else choice.id
        for choice in turn.prompt.choices
    ] == [
        "attention",
        "side-effect",
        "stress",
    ]
    assert "choose one" in turn.prompt.text
    with db(app) as session:
        made = [
            decode(row) for row in events_since(session, campaign_id) if row.kind == MoveMade.kind
        ]
    assert len(made) == 1

    events = answer(client, campaign_id, "choice", "stress")

    assert events[-1]["type"] == "saved"
    assert _pc(app, campaign_id).counters.get("stress", 0) == 1
    with db(app) as session:
        chosen = [
            event
            for event in (
                decode(row)
                for row in events_since(session, campaign_id)
                if row.kind == MoveChoiceMade.kind
            )
            if isinstance(event, MoveChoiceMade)
        ]
    assert [(event.choice, event.band) for event in chosen] == [("stress", "7-9")]

    rejected = answer(client, campaign_id, "choice", "attention")
    assert rejected[-1]["type"] == "error"


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

    assert rules.clock is not None
    assert rules.clock.wage_currency == "silver"
    assert rules.rests
    assert rules.marketplace is not None
    assert rules.marketplace.currency == "silver"
    assert rules.character.features["silver"].name == "Silver Pieces"

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


SUBJECTS = (
    "Charms",
    "Transfiguration",
    "Warding",
    "Divination",
    "Healing",
    "Illusion",
    "Elemental",
)
FORBIDDEN_SPELLS = (
    "command-the-will",
    "sever-the-thread",
    "borrowed-years",
    "hollow-name",
    "open-the-deep",
)


def _year_picks(
    subjects: int,
    spells: int,
    potions: int,
    enchantments: int,
    training: int = 0,
    training_tag: str = "",
) -> list[tuple[object, ...]]:
    """The feat picks the bundled compact `starting_years` table expands into for one year."""
    picks: list[tuple[object, ...]] = [
        ("subjects", subjects, SUBJECTS, "", "", "subject", ()),
        ("spells", spells, SUBJECTS, "", "subjects", "spell", FORBIDDEN_SPELLS),
        ("potions", potions, (), "Potions", "", "potion", ()),
        ("enchantments", enchantments, (), "Enchantments", "", "enchantment", ()),
    ]
    if training:
        picks.append(("training", training, (), training_tag, "", "trait", ()))
    return picks


@pytest.mark.parametrize(
    ("year", "picks"),
    [
        ("year-1", _year_picks(1, 2, 1, 1)),
        ("year-3", _year_picks(2, 4, 2, 1, 1, "training-1-3")),
        ("year-4", _year_picks(2, 5, 3, 2, 1, "training-4-6")),
        ("year-7", _year_picks(4, 8, 4, 3, 3, "training-7")),
    ],
)
def test_a_bundled_year_template_keeps_its_authored_picks(
    year: str, picks: list[tuple[object, ...]]
) -> None:
    template = _academy().character.templates[year]
    assert [
        (pick.id, pick.count, pick.tags, pick.tag, pick.tags_from, pick.feature_type, pick.exclude)
        for pick in template.feat_picks
    ] == picks


def test_every_bundled_starting_year_expands_from_one_table() -> None:
    """All seven years keep their names, year counters, and pick counts."""
    years = {
        template.id: template
        for template in _academy().character.templates.values()
        if template.kind == "starting_year"
    }
    expected = [
        ("year-1", 0, 1, 2, 1, 1, 0),
        ("year-2", 1, 1, 3, 2, 1, 0),
        ("year-3", 2, 2, 4, 2, 1, 1),
        ("year-4", 3, 2, 5, 3, 2, 1),
        ("year-5", 4, 3, 6, 3, 2, 2),
        ("year-6", 5, 3, 7, 4, 2, 2),
        ("year-7", 6, 4, 8, 4, 3, 3),
    ]
    assert list(years) == [ident for ident, *_ in expected]
    for ident, counter, subjects, spells, potions, enchantments, training in expected:
        template = years[ident]
        assert (template.kind, template.name) == ("starting_year", f"Year {counter + 1}")
        assert template.counters == {"year": counter}
        counts = {
            "subjects": subjects,
            "spells": spells,
            "potions": potions,
            "enchantments": enchantments,
        }
        if training:
            counts["training"] = training
        assert {pick.id: pick.count for pick in template.feat_picks} == counts


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


def test_a_stat_at_its_advancement_cap_is_not_offered(
    client_factory: Callable[[], TestClient], app: FastAPI
) -> None:
    _client, campaign_id = _campaign(client_factory)
    tools = tools_for(app, campaign_id, rule_set=RULE_SET)
    tools.end_period("school-year")
    tools.take_advancement(NAME, "stat", stat="theory")
    assert _pc(app, campaign_id).stats["theory"] == 2

    page = _advancement_page(app, campaign_id)

    assert page is not None and page.kind == level_up_service.OPTION
    stat_option = next(option for option in page.options if option["value"] == "stat")
    assert "theory" not in {stat["id"] for stat in stat_option["stats"]}
    assert "alchemy" in {stat["id"] for stat in stat_option["stats"]}


def test_advancement_refuses_a_stat_past_its_cap_without_spending_a_point(
    client_factory: Callable[[], TestClient], app: FastAPI
) -> None:
    _client, campaign_id = _campaign(client_factory)
    tools = tools_for(app, campaign_id, rule_set=RULE_SET)
    tools.end_period("school-year")
    tools.take_advancement(NAME, "stat", stat="theory")
    before = _pc(app, campaign_id)

    with pytest.raises(InvalidInput, match="already at 2"):
        tools.take_advancement(NAME, "stat", stat="theory")

    after = _pc(app, campaign_id)
    assert after.stats["theory"] == 2
    assert after.counters["advancements"] == before.counters["advancements"] == 3
    assert after.advancements_taken == before.advancements_taken == {"stat": 1}


def test_the_major_subject_is_not_offered_or_learned(
    client_factory: Callable[[], TestClient], app: FastAPI
) -> None:
    _client, campaign_id = _campaign(client_factory, major="Illusion")
    tools = tools_for(app, campaign_id, rule_set=RULE_SET)
    tools.end_period("school-year")
    before = _pc(app, campaign_id)

    page = _advancement_page(app, campaign_id)

    assert page is not None and page.kind == level_up_service.OPTION
    subject_option = next(option for option in page.options if option["value"] == "subject")
    known = {feature["id"] for feature in subject_option["features"]}
    assert "subject-illusion" not in known
    assert "subject-transfiguration" in known

    with pytest.raises(InvalidInput, match="needs a subject"):
        tools.take_advancement(NAME, "subject", feature="subject-illusion")

    after = _pc(app, campaign_id)
    assert after.counters["advancements"] == before.counters["advancements"] == 4
    assert after.advancements_taken == {}
    assert after.features == before.features


def test_the_character_tab_shows_active_conditions(
    client_factory: Callable[[], TestClient],
) -> None:
    client, campaign_id = _campaign(client_factory)
    tools = tools_for(client.app, campaign_id, rule_set=RULE_SET)
    tools.add_condition(NAME, "burnout", "too much study")

    page = client.get(f"/campaigns/{campaign_id}").text

    assert re.search(r"Conditions: (?:<a[^>]*>)?Burnout", page)


def test_school_year_stops_at_seven(
    client_factory: Callable[[], TestClient],
) -> None:
    client, campaign_id = _campaign(client_factory)
    tools = tools_for(client.app, campaign_id, rule_set=RULE_SET)

    for _ in range(8):
        tools.end_period("school-year")

    assert _pc(client.app, campaign_id).counters["year"] == 7


def test_downtime_rest_ends_burnout(
    client_factory: Callable[[], TestClient],
) -> None:
    client, campaign_id = _campaign(client_factory)
    tools = tools_for(client.app, campaign_id, rule_set=RULE_SET)
    tools.add_condition(NAME, "burnout", "too much study")

    tools.rest("downtime", reason="a week away from the bench")

    assert "burnout" not in _pc(client.app, campaign_id).conditions


def test_downtime_rest_clears_stress(
    client_factory: Callable[[], TestClient],
) -> None:
    client, campaign_id = _campaign(client_factory)
    tools = tools_for(client.app, campaign_id, rule_set=RULE_SET)
    tools.adjust_counter(NAME, "stress", 3)
    assert _pc(client.app, campaign_id).counters["stress"] == 3

    tools.rest("downtime", reason="a week away from the bench")

    assert _pc(client.app, campaign_id).counters["stress"] == 0


def test_the_gm_can_set_and_clear_the_college_field(
    client_factory: Callable[[], TestClient],
) -> None:
    client, campaign_id = _campaign(client_factory)
    tools = tools_for(client.app, campaign_id, rule_set=RULE_SET)

    result = tools.set_character_field(NAME, "College", "The Lantern College")

    assert result == {"character": NAME, "field": "college", "value": "The Lantern College"}
    assert _pc(client.app, campaign_id).fields["college"] == "The Lantern College"

    tools.set_character_field(NAME, "college", "")

    assert "college" not in _pc(client.app, campaign_id).fields


def test_the_academy_market_sells_for_silver(
    client_factory: Callable[[], TestClient],
) -> None:
    client, campaign_id = _campaign(client_factory)
    tools = tools_for(client.app, campaign_id, rule_set=RULE_SET)
    tools.change_currency(NAME, 10, "a test purse")

    result = tools.buy_item(NAME, "Candle")

    assert result["price"] == 1
    assert result["currency"] == "silver"
    assert result["currency_left"] == 9
    assert "Candle" in [item.name for item in _pc(client.app, campaign_id).items]


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


def test_creation_form_limits_spells_to_the_chosen_subjects(
    client_factory: Callable[[], TestClient],
) -> None:
    client = signed_in_client(client_factory)
    campaign_id = create_campaign(client, world_template=WORLD, rule_set=RULE_SET)
    choose_campaign_options(client, campaign_id, RULE_SET)

    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'data-tags-from="subjects"' in page
    assert 'data-feature-tag="Charms"' in page
    assert "Choose subjects first" in page


def test_a_creation_spell_must_belong_to_a_chosen_subject() -> None:
    rules = _academy()
    submitted = _submitted()
    submitted["feature_picks"]["subjects"] = ["subject-warding"]

    with pytest.raises(CharacterError, match="no spell tagged"):
        check_choices(rules.character, submitted)
