from __future__ import annotations

import pytest

from cygnusforge.services.common import InvalidInput
from tests.test_hogwarts import (
    HARRY,
    _campaign,
    _counter,
    _harry,
    _mark_all_conditions,
    _state,
    _tools,
)


def _grant_advancements(tools, count: int) -> None:
    tools.adjust_counter(HARRY, "advancements", count)


def test_a_trait_advancement_raises_the_chosen_stat(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    _grant_advancements(tools, 1)
    result = tools.take_advancement(HARRY, "trait", "cunning")
    assert result["stat"] == "cunning"
    assert _harry(app, campaign_id).stats["cunning"] == 2
    assert _counter(app, campaign_id, "advancements") == 0


def test_a_trait_advancement_stops_at_the_cap_of_three(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    _grant_advancements(tools, 2)
    tools.take_advancement(HARRY, "trait", "bravery")
    assert _harry(app, campaign_id).stats["bravery"] == 3
    tools.take_advancement(HARRY, "trait", "bravery")
    assert _harry(app, campaign_id).stats["bravery"] == 3


def test_a_character_takes_a_trait_advancement_at_most_twice(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    _grant_advancements(tools, 3)
    tools.take_advancement(HARRY, "trait", "cunning")
    tools.take_advancement(HARRY, "trait", "intellect")
    with pytest.raises(InvalidInput, match="as often as allowed"):
        tools.take_advancement(HARRY, "trait", "loyalty")


def test_a_trait_advancement_needs_a_stat(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    _grant_advancements(tools, 1)
    with pytest.raises(InvalidInput, match="needs a stat"):
        tools.take_advancement(HARRY, "trait", None)
    assert _counter(app, campaign_id, "advancements") == 1


def test_a_subject_advancement_refuses_a_stat(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    _grant_advancements(tools, 1)
    with pytest.raises(InvalidInput, match="does not take a stat"):
        tools.take_advancement(HARRY, "subject", "bravery")


def test_a_second_favourite_subject_is_refused_once_one_is_taken(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    _grant_advancements(tools, 2)
    tools.take_advancement(HARRY, "subject", None)
    with pytest.raises(InvalidInput, match="as often as allowed"):
        tools.take_advancement(HARRY, "subject", None)


def test_an_advancement_is_refused_without_a_point_to_spend(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    with pytest.raises(InvalidInput, match="no Advancement to spend"):
        tools.take_advancement(HARRY, "spell", None)


def test_an_unknown_advancement_option_is_refused(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    _grant_advancements(tools, 1)
    with pytest.raises(InvalidInput, match="no Advancement"):
        tools.take_advancement(HARRY, "wand-upgrade", None)


def test_regaining_luck_is_an_advancement_option(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    tools.adjust_counter(HARRY, "luck", -2)
    _grant_advancements(tools, 1)
    tools.take_advancement(HARRY, "luck", None)
    assert _counter(app, campaign_id, "luck") == 2


def test_filling_experience_grants_an_advancement(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    assert _counter(app, campaign_id, "advancements") == 0
    tools.adjust_counter(HARRY, "experience", 4 - _counter(app, campaign_id, "experience"))
    assert _counter(app, campaign_id, "experience") == 0
    assert _counter(app, campaign_id, "advancements") == 1


def test_ending_a_school_year_restores_luck_and_grants_an_advancement(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    tools.adjust_counter(HARRY, "luck", -3)
    assert _counter(app, campaign_id, "luck") == 0
    tools.end_period("school-year")
    assert _counter(app, campaign_id, "luck") == 3
    assert _counter(app, campaign_id, "advancements") == 1


def test_a_house_tally_takes_points_and_deducts_them(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    result = tools.adjust_tally("house-points", "gryffindor", 10, "won the Quidditch match")
    assert result["value"] == 10
    result = tools.adjust_tally("house-points", "gryffindor", -15, "broke curfew")
    assert result["before"] == 10
    assert result["value"] == -5
    assert _state(app, campaign_id).tallies["house-points"]["gryffindor"] == -5


def test_a_tally_entry_the_rule_set_does_not_define_is_refused(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    with pytest.raises(InvalidInput, match="no House Points entry"):
        tools.adjust_tally("house-points", "durmstrang", 5, "")


def test_a_friend_is_added_and_then_removed(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    tools.set_relation(HARRY, "friend", "Ron", True)
    assert _harry(app, campaign_id).relations["friend"] == ["Ron"]
    tools.set_relation(HARRY, "friend", "Ron", False)
    assert _harry(app, campaign_id).relations.get("friend", []) == []


def test_a_character_cannot_befriend_the_same_person_twice(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    tools.set_relation(HARRY, "friend", "Ron", True)
    with pytest.raises(InvalidInput, match="already has"):
        tools.set_relation(HARRY, "friend", "Ron", True)


def test_a_character_has_at_most_two_friends(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    tools.set_relation(HARRY, "friend", "Ron", True)
    tools.set_relation(HARRY, "friend", "Hermione", True)
    with pytest.raises(InvalidInput, match="at most 2 Friends"):
        tools.set_relation(HARRY, "friend", "Neville", True)


def test_a_character_has_at_most_one_rival(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    tools.set_relation(HARRY, "rival", "Draco", True)
    with pytest.raises(InvalidInput, match="at most 1 Rival"):
        tools.set_relation(HARRY, "rival", "Snape", True)


def test_a_relation_that_is_not_held_cannot_be_ended(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    with pytest.raises(InvalidInput, match="is not a Friend"):
        tools.set_relation(HARRY, "friend", "Ron", False)


def test_the_highest_trait_is_rolled_for_the_exit_roll_and_named(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    _mark_all_conditions(tools)
    result = tools.roll_move("exit_the_story", HARRY)
    assert result["stat"] == "bravery"


def test_the_exit_roll_refuses_a_stat_chosen_by_the_player(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    _mark_all_conditions(tools)
    with pytest.raises(InvalidInput):
        tools.roll_move("exit_the_story", HARRY, stat="cunning")
