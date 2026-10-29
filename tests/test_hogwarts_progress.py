from __future__ import annotations

import pytest

from cygnusforge.services.common import InvalidInput, session_scope
from cygnusforge.services.events import (
    FeatureLearned,
    TallyReset,
    decode,
    events_since,
)
from tests.test_hogwarts import (
    HARRY,
    HOGWARTS,
    _campaign,
    _counter,
    _harry,
    _mark_all_conditions,
    _state,
    _tools,
)


def _grant_advancements(tools, count: int) -> None:
    tools.adjust_counter(HARRY, "advancements", count)


def _spell_name(spell_id: str) -> str:
    return HOGWARTS.character.features[spell_id].name


def _unheld_spells(app, campaign_id: int, count: int) -> list[str]:
    """Spells from Harry's year that she does not know. Creation rolls her starting spells, so the
    tests ask for ones she lacks rather than naming fixed ones."""
    harry = _harry(app, campaign_id)
    features = HOGWARTS.character.features
    pool = sorted(
        f.id
        for f in features.values()
        if f.feature_type == "spell"
        and f.tag == harry.fields["year"]
        and f.id not in harry.features
    )
    assert len(pool) >= count
    return pool[:count]


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


def _events_of(app, campaign_id: int, kind: str) -> list:
    with session_scope(app.state.engine) as db:
        return [decode(row) for row in events_since(db, campaign_id) if row.kind == kind]


def test_learning_a_spell_adds_it_from_the_characters_year(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    _grant_advancements(tools, 1)
    [spell] = _unheld_spells(app, campaign_id, 1)
    result = tools.take_advancement(HARRY, "spell", None, spell)
    assert result["feature"] == _spell_name(spell)
    assert spell in _harry(app, campaign_id).features
    assert _counter(app, campaign_id, "advancements") == 0
    learned = _events_of(app, campaign_id, FeatureLearned.kind)
    assert [(e.feature, e.name) for e in learned] == [(spell, HARRY)]


def test_a_spell_from_another_year_cannot_be_learned_as_an_advancement(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    _grant_advancements(tools, 1)
    with pytest.raises(InvalidInput, match="needs a spell from 5th Year"):
        tools.take_advancement(HARRY, "spell", None, "lumos")
    assert _counter(app, campaign_id, "advancements") == 1
    assert "lumos" not in _harry(app, campaign_id).features


def test_a_spell_already_known_cannot_be_learned_again(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    _grant_advancements(tools, 2)
    [spell] = _unheld_spells(app, campaign_id, 1)
    tools.take_advancement(HARRY, "spell", None, spell)
    with pytest.raises(InvalidInput, match=f"already has {_spell_name(spell)}"):
        tools.take_advancement(HARRY, "spell", None, spell)
    assert _counter(app, campaign_id, "advancements") == 1


def test_learning_spells_has_no_limit(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    _grant_advancements(tools, 2)
    first, second = _unheld_spells(app, campaign_id, 2)
    tools.take_advancement(HARRY, "spell", None, first)
    tools.take_advancement(HARRY, "spell", None, second)
    assert {first, second} <= set(_harry(app, campaign_id).features)


def test_a_learn_spell_advancement_needs_a_spell_to_be_named(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    _grant_advancements(tools, 1)
    with pytest.raises(InvalidInput, match="needs a spell from 5th Year"):
        tools.take_advancement(HARRY, "spell", None, None)
    assert _counter(app, campaign_id, "advancements") == 1


def test_a_feature_is_refused_for_an_option_that_teaches_none(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    _grant_advancements(tools, 1)
    with pytest.raises(InvalidInput, match="does not take a feature"):
        tools.take_advancement(HARRY, "subject", None, "aperio")


def test_a_period_end_starts_house_points_over_and_logs_the_final_totals(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    tools.adjust_tally("house-points", "gryffindor", 10, "won the Quidditch match")
    tools.adjust_tally("house-points", "slytherin", -4, "broke curfew")
    tools.end_period("school-year")
    assert _state(app, campaign_id).tallies["house-points"] == {}
    (reset,) = _events_of(app, campaign_id, TallyReset.kind)
    assert reset.final["gryffindor"] == 10
    assert reset.final["slytherin"] == -4
    assert reset.final["hufflepuff"] == 0
    assert reset.labels["gryffindor"] == "Gryffindor"


def test_an_award_key_cannot_be_paid_twice_on_a_tally(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    tools.adjust_tally("house-points", "gryffindor", 50, "won", award="match-1")
    with pytest.raises(InvalidInput, match="already paid the award 'match-1'"):
        tools.adjust_tally("house-points", "gryffindor", 50, "won again", award="match-1")
    assert _state(app, campaign_id).tallies["house-points"]["gryffindor"] == 50


def test_changes_without_an_award_key_are_not_guarded_by_key(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    tools.adjust_tally("house-points", "gryffindor", 10, "a")
    tools.adjust_tally("house-points", "gryffindor", 20, "b")
    assert _state(app, campaign_id).tallies["house-points"]["gryffindor"] == 30


def test_a_keyless_repeat_of_an_earlier_gain_is_still_refused(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    tools.adjust_tally("house-points", "gryffindor", 10, "a")
    with pytest.raises(InvalidInput, match="force=true"):
        tools.adjust_tally("house-points", "gryffindor", 10, "b")


def test_a_period_end_clears_the_award_keys_so_a_new_year_can_reuse_them(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    tools.adjust_tally("house-points", "gryffindor", 10, "won", award="match-1")
    tools.end_period("school-year")
    tools.adjust_tally("house-points", "gryffindor", 5, "won", award="match-1")
    assert _state(app, campaign_id).tallies["house-points"]["gryffindor"] == 5


def test_a_repeated_gain_of_the_same_amount_is_refused_without_force(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    tools.adjust_tally("house-points", "gryffindor", 50, "won", award="match-1")
    with pytest.raises(InvalidInput, match="force=true"):
        tools.adjust_tally("house-points", "gryffindor", 50, "won again", award="match-2")
    assert _state(app, campaign_id).tallies["house-points"]["gryffindor"] == 50


def test_a_repeated_gain_is_allowed_with_force(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    tools.adjust_tally("house-points", "gryffindor", 50, "won", award="match-1")
    tools.adjust_tally("house-points", "gryffindor", 50, "won again", award="match-2", force=True)
    assert _state(app, campaign_id).tallies["house-points"]["gryffindor"] == 100


def test_a_gain_of_a_different_amount_is_not_a_repeat(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    tools.adjust_tally("house-points", "gryffindor", 50, "won", award="match-1")
    tools.adjust_tally("house-points", "gryffindor", 10, "house cup", award="match-2")
    assert _state(app, campaign_id).tallies["house-points"]["gryffindor"] == 60


def test_a_gain_matching_another_entrys_gain_is_not_a_repeat(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    tools.adjust_tally("house-points", "gryffindor", 50, "won", award="match-1")
    tools.adjust_tally("house-points", "slytherin", 50, "won", award="match-2")
    assert _state(app, campaign_id).tallies["house-points"]["slytherin"] == 50


def test_a_deduction_matching_an_earlier_gain_is_not_refused(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    tools.adjust_tally("house-points", "gryffindor", 50, "won", award="match-1")
    tools.adjust_tally("house-points", "gryffindor", -50, "corrected")
    assert _state(app, campaign_id).tallies["house-points"]["gryffindor"] == 0


def test_a_period_end_clears_the_repeat_check_for_a_new_year(client_factory):
    app, campaign_id = _campaign(client_factory)
    tools = _tools(app, campaign_id)
    tools.adjust_tally("house-points", "gryffindor", 50, "won", award="match-1")
    tools.end_period("school-year")
    tools.adjust_tally("house-points", "gryffindor", 50, "won", award="match-2")
    assert _state(app, campaign_id).tallies["house-points"]["gryffindor"] == 50
