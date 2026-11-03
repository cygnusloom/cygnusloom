from __future__ import annotations

import random
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusforge.content.library import ALL_CONDITIONS
from cygnusforge.services.common import InvalidInput, session_scope
from cygnusforge.services.events import (
    EffectNoted,
    MoveMade,
    decode,
    events_since,
    modifier_value,
)
from cygnusforge.services.injuries import EffectContext, run_effects
from cygnusforge.services.world import WorldTools
from tests.conftest import content_library
from tests.factories import Rolls
from tests.test_campaigns import _create_character, _new_campaign
from tests.test_character_creation import _signed_in
from tests.test_world import _state

LIBRARY = content_library()
HOGWARTS = LIBRARY.rule_sets["hogwarts-rpg"]
HARRY = "Harry"
# Harry is a Gryffindor half-blood. Gryffindor adds 1 to Bravery and half-blood starts with 2 Experience.
TRAITS = {
    "stat-bravery": "2",
    "stat-cunning": "1",
    "stat-intellect": "1",
    "stat-loyalty": "0",
    "stat-magic": "-1",
    "template-house": "gryffindor",
    "template-blood": "half-blood",
    "field-complexion": "Pale",
    "field-hair": "Black",
    "field-look": "Thin",
    "field-wand-look": "Plain",
    "field-wand-core": "Phoenix",
    "field-ambition": "Friendship",
    "field-pet": "Owl",
    "field-year": "5th Year",
    "field-favourite-subject": "Defence Against the Dark Arts",
    "field-patronus": "Lion",
    "field-surname": "Quinn",
}
HARRY_EXPERIENCE = 2


def _campaign(client_factory: Callable[[], TestClient]) -> tuple[FastAPI, int]:
    client = _signed_in(client_factory)
    response = _new_campaign(client, world_template="hogwarts", rule_set="hogwarts-rpg")
    assert response.status_code == 303
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    created = _create_character(client, campaign_id, name=HARRY, fields=TRAITS)
    assert created.status_code == 303
    return client.app, campaign_id


def _tools(app: FastAPI, campaign_id: int, rng: Any = None) -> WorldTools:
    world = LIBRARY.world_content("hogwarts", "hogwarts-rpg")
    return WorldTools(
        app.state.engine,
        campaign_id,
        {**LIBRARY.core.tables, **world.tables},
        rng or random.Random(0),
        HOGWARTS.character,
        grid=HOGWARTS.grid,
        terrain=HOGWARTS.terrain,
        encounter_objects=HOGWARTS.encounter_objects,
        encounter_templates=HOGWARTS.encounter_templates,
        combat=HOGWARTS.combat,
        monsters={**HOGWARTS.monsters, **world.monsters},
        features={**world.features},
        conditions=HOGWARTS.conditions,
        moves=HOGWARTS.moves,
        counters=HOGWARTS.counters,
        dice_tables={**HOGWARTS.dice_tables, **world.dice_tables},
        clock_rules=HOGWARTS.clock,
        rests=HOGWARTS.rests,
        interrupted_rest=HOGWARTS.interrupted_rest,
        inventory=HOGWARTS.inventory,
        item_sizes=HOGWARTS.item_sizes,
        magic=HOGWARTS.magic,
        hooks=HOGWARTS.hooks,
        marketplace=HOGWARTS.marketplace,
        plus=HOGWARTS.plus,
        npc_hp=HOGWARTS.npc_hp,
        travel=HOGWARTS.travel,
        wilderness=HOGWARTS.wilderness,
        world_terrain=HOGWARTS.world_terrain,
        advancements=HOGWARTS.advancements,
        periods=HOGWARTS.periods,
        tallies=HOGWARTS.tallies,
        relations=HOGWARTS.relations,
    )


def _harry(app: FastAPI, campaign_id: int):
    return _state(app, campaign_id).characters[HARRY.casefold()]


def _counter(app: FastAPI, campaign_id: int, counter: str) -> int:
    # A counter a character has never changed is absent from her state and sits at its initial value.
    return _harry(app, campaign_id).counters.get(counter, HOGWARTS.counters[counter].initial)


def test_the_bundled_ruleset_has_its_counters_conditions_moves_and_tools():
    assert set(HOGWARTS.counters) == {"luck", "experience", "advancements"}
    assert HOGWARTS.counters["luck"].maximum == 3
    assert HOGWARTS.counters["experience"].maximum == 4
    assert len(HOGWARTS.conditions) == 8
    assert HOGWARTS.conditions["scared"].modifies == {"bravery": -2}
    assert HOGWARTS.conditions["injured"].modifies == {"*": -1}
    assert len(HOGWARTS.moves) == 14
    assert HOGWARTS.moves["catch_the_golden_snitch"].stat_choice == (
        "bravery",
        "cunning",
        "intellect",
        "loyalty",
    )
    assert HOGWARTS.moves["get_what_you_seek"].stat_choice == ("bravery", "cunning")
    assert HOGWARTS.moves["stand_in_face_of_danger"].stat == "bravery"
    assert {"roll_move", "adjust_counter", "add_condition", "end_condition"} <= set(
        HOGWARTS.tool_calls
    )


def test_the_hogwarts_world_is_its_own_template():
    template = LIBRARY.world_templates["hogwarts"]
    assert template.name == "Hogwarts & Grounds"
    assert template.start is not None
    assert template.start.location == "great-hall"
    assert LIBRARY.world_content("hogwarts", "hogwarts-rpg").monsters == {}


def test_a_character_takes_the_traits_assigned_to_her(client_factory):
    app, campaign_id = _campaign(client_factory)
    character = _harry(app, campaign_id)
    assert {stat: modifier_value(character, stat) for stat in character.stats} == {
        "bravery": 3,
        "cunning": 1,
        "intellect": 1,
        "loyalty": 0,
        "magic": -1,
    }


def test_a_move_rolls_the_trait_it_names(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id, Rolls(3, 4))
    result = tools.roll_move("stand_in_face_of_danger", HARRY)
    assert result["bonus"] == 3
    assert result["total"] == 10


def test_a_move_the_player_chooses_takes_the_trait_chosen(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id, Rolls(3, 4))
    result = tools.roll_move("get_what_you_seek", HARRY, stat="cunning")
    assert result["bonus"] == 1


def test_a_move_the_player_chooses_refuses_a_trait_outside_its_choice(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id, Rolls(3, 4))
    with pytest.raises(InvalidInput, match="pick one of"):
        tools.roll_move("get_what_you_seek", HARRY, stat="magic")


def test_a_move_that_rolls_a_trait_of_your_choice_needs_one(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id, Rolls(3, 4))
    with pytest.raises(InvalidInput, match="pick one of"):
        tools.roll_move("roll_trait", HARRY)


def test_a_move_takes_a_situational_modifier(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id, Rolls(3, 4))
    result = tools.roll_move("stand_in_face_of_danger", HARRY, modifier=1)
    assert result["bonus"] == 4


def test_a_scared_character_rolls_bravery_with_a_penalty(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id, Rolls(3, 4))
    tools.add_condition(HARRY, "scared", "Hexed by a rival.")
    result = tools.roll_move("stand_in_face_of_danger", HARRY)
    assert result["bonus"] == 1


def test_luck_is_spent_and_regained_within_its_bounds(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    assert _counter(app, campaign_id, "luck") == 3
    tools.adjust_counter(HARRY, "luck", -1)
    assert _counter(app, campaign_id, "luck") == 2
    tools.adjust_counter(HARRY, "luck", -5)
    assert _counter(app, campaign_id, "luck") == 0
    tools.adjust_counter(HARRY, "luck", 10)
    assert _counter(app, campaign_id, "luck") == 3


def test_four_experience_is_cleared_when_it_fills(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    assert _counter(app, campaign_id, "experience") == HARRY_EXPERIENCE
    tools.adjust_counter(HARRY, "experience", 4 - HARRY_EXPERIENCE - 1)
    assert _counter(app, campaign_id, "experience") == 3
    tools.adjust_counter(HARRY, "experience", 1)
    assert _counter(app, campaign_id, "experience") == 0


def test_a_failed_move_marks_experience(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id, Rolls(1, 2))
    result = tools.roll_move("stand_in_face_of_danger", HARRY)
    assert result["total"] == 3 + 3
    assert _counter(app, campaign_id, "experience") == HARRY_EXPERIENCE + 1


def test_a_failed_move_renders_without_a_doubled_full_stop(client_factory):
    # The band text already ends in a full stop, so the rendered line must not add a second one.
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id, Rolls(1, 2))
    tools.roll_move("stand_in_face_of_danger", HARRY)
    with session_scope(app.state.engine) as db:
        made = [row for row in events_since(db, campaign_id) if row.kind == MoveMade.kind]
        rendered = decode(made[-1]).render()
    assert rendered.endswith("as a result.")
    assert ".." not in rendered


def test_the_bundled_ruleset_has_the_book_rules_for_the_narrator_and_the_players():
    assert {
        "narrator-moves",
        "serious-consequences",
        "house-points",
        "luck",
        "experience",
        "advancements",
        "friends-and-rivals",
        "exiting-the-story",
        "unforgivable-curses",
        "healing",
        "pets",
        "patronus",
    } <= set(HOGWARTS.rules)
    assert all(text.strip() for text in HOGWARTS.rules.values())
    assert "exit_the_story" in HOGWARTS.moves


def test_the_roll_move_tool_offers_exactly_the_moves_and_traits_the_rule_set_defines():
    schema = HOGWARTS.tool_calls["roll_move"].parameters["properties"]
    assert set(schema["move"]["enum"]) == set(HOGWARTS.moves)
    assert set(schema["stat"]["enum"]) == {"bravery", "cunning", "intellect", "loyalty", "magic"}


def test_the_statblock_shows_luck_and_experience(client_factory):
    app, campaign_id = _campaign(client_factory)
    statblock = _tools(app, campaign_id).get_statblock(HARRY)
    assert "Luck 3 of 3" in str(statblock)
    assert f"Experience {HARRY_EXPERIENCE} of 4" in str(statblock)


ALL_EIGHT = (
    "scared",
    "angry",
    "stressed",
    "jealous",
    "embarrassed",
    "injured",
    "hexed",
    "unconscious",
)


def _mark_all_conditions(tools: WorldTools) -> None:
    for condition in ALL_EIGHT:
        tools.add_condition(HARRY, condition, "A bad day.")


def test_the_exit_roll_is_closed_until_every_condition_is_marked(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id, Rolls(5, 5))
    tools.add_condition(HARRY, "scared", "A bad day.")
    with pytest.raises(InvalidInput, match="only open once every condition is marked"):
        tools.roll_move("exit_the_story", HARRY)


def test_a_ten_on_the_exit_roll_clears_every_condition(client_factory):
    # Scared (-2) and Injured (-1) take Harry's bravery to -1 here, so 6 and 6 make 11.
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id, Rolls(6, 6))
    _mark_all_conditions(tools)
    result = tools.roll_move("exit_the_story", HARRY)
    assert result["band"] == "Return with everything cleared"
    assert _harry(app, campaign_id).conditions == []
    assert not _harry(app, campaign_id).retired


def test_a_seven_to_nine_on_the_exit_roll_clears_only_unconscious(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id, Rolls(4, 4))
    _mark_all_conditions(tools)
    result = tools.roll_move("exit_the_story", HARRY)
    assert result["band"] == "Return, Unconscious cleared"
    assert sorted(_harry(app, campaign_id).conditions) == sorted(
        c for c in ALL_EIGHT if c != "unconscious"
    )
    assert not _harry(app, campaign_id).retired


def test_a_six_or_less_on_the_exit_roll_takes_the_character_out_of_the_story(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id, Rolls(1, 2))
    _mark_all_conditions(tools)
    result = tools.roll_move("exit_the_story", HARRY)
    assert result["band"] == "Exit the story"
    harry = _harry(app, campaign_id)
    assert harry.retired
    assert harry.out_of_play
    assert not harry.dead


def test_a_character_who_left_the_story_is_no_longer_the_player_character(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id, Rolls(1, 2))
    _mark_all_conditions(tools)
    tools.roll_move("exit_the_story", HARRY)
    state = _state(app, campaign_id)
    assert state.pc() is not None
    assert state.pc().out_of_play


def test_ending_every_condition_when_none_are_held_is_noted_not_an_error(client_factory):
    app, campaign_id = _campaign(client_factory)
    with session_scope(app.state.engine) as db:
        run_effects(
            db,
            campaign_id,
            HARRY,
            [{"op": "end_condition", "id": ALL_CONDITIONS}],
            EffectContext(
                rng=random.Random(0), combat=None, counters={}, conditions=HOGWARTS.conditions
            ),
            "A test",
            plus="",
        )
        noted = [row for row in events_since(db, campaign_id) if row.kind == EffectNoted.kind]
        assert any("has no conditions to end" in decode(row).text for row in noted)
    assert _harry(app, campaign_id).conditions == []


def test_a_quidditch_match_keeps_control_on_a_ten(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id, Rolls(5, 5))
    result = tools.roll_move("play_a_quidditch_match", HARRY, stat="bravery")
    assert result["total"] == 13
    assert result["band"] == "Keep control"


def test_a_quidditch_match_needs_a_trait_it_names(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id, Rolls(5, 5))
    with pytest.raises(InvalidInput, match="pick one of"):
        tools.roll_move("play_a_quidditch_match", HARRY, stat="magic")


def test_a_missed_snitch_catch_marks_experience(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id, Rolls(1, 2))
    result = tools.roll_move("catch_the_golden_snitch", HARRY, stat="cunning")
    assert result["total"] == 3 + 1
    assert result["band"] == "Lost it"
    assert _counter(app, campaign_id, "experience") == HARRY_EXPERIENCE + 1


def test_creation_needs_a_house_a_blood_status_and_every_table_field(client_factory):
    client = _signed_in(client_factory)
    response = _new_campaign(client, world_template="hogwarts", rule_set="hogwarts-rpg")
    campaign_id = int(response.headers["location"].rsplit("/", 1)[1])
    no_house = {key: value for key, value in TRAITS.items() if key != "template-house"}
    assert _create_character(client, campaign_id, name=HARRY, fields=no_house).status_code == 400
    no_patronus = {key: value for key, value in TRAITS.items() if key != "field-patronus"}
    assert _create_character(client, campaign_id, name=HARRY, fields=no_patronus).status_code == 400
    created = _create_character(client, campaign_id, name=HARRY, fields=TRAITS)
    assert created.status_code == 303


def test_hogwarts_has_no_hooks(client_factory):
    assert HOGWARTS.hooks is None
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    with pytest.raises(InvalidInput):
        tools.rest("night", reason="camp")
