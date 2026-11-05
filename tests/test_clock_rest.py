from __future__ import annotations

import pytest

from cygnusloom.content.definitions import ContentError, Definition
from cygnusloom.content.library import _load_rests
from cygnusloom.services.campaigns import _turn_has_day_end
from cygnusloom.services.clock import absolute, rest_target
from cygnusloom.services.common import InvalidInput, session_scope
from cygnusloom.services.events import RestTaken, events_since
from tests.test_combat import (
    CAIRN,
    PC,
    YARD,
    _character,
    _fight,
    _set_condition,
    _set_stat,
    _state,
)

PARTS = len(CAIRN.clock.parts)
HPP = CAIRN.clock.hours_per_part


def _wound(app, campaign_id, name=PC):
    _set_stat(app, campaign_id, name, "hp", 1)
    return _character(app, campaign_id, name).harm.get("hp", 0)


def _clock_position(app, campaign_id):
    return absolute(_state(app, campaign_id).clock, PARTS, HPP)


def _last_event_id(app, campaign_id):
    with session_scope(app.state.engine) as db:
        return max(event.id for event in events_since(db, campaign_id))


@pytest.mark.parametrize(
    ("rest_name", "start", "expected"),
    # A night begun in the morning stops at dusk; begun in the afternoon, it runs to morning.
    [("night", 0, 2), ("night", 3, 5), ("night", 1, 3), ("night", 2, 3), ("week", 0, 21)],
)
def test_a_rest_ends_at_its_target_part(rest_name, start, expected):
    assert rest_target(start * HPP, CAIRN.rests[rest_name], CAIRN.clock) == expected * HPP


def test_a_night_begun_in_the_morning_stops_at_dusk_in_the_cairn_ruleset():
    assert CAIRN.rests["night"].stop_at == {"morning": "night"}


def test_a_rest_ends_on_the_hour_it_began_plus_its_length(client_factory):
    assert rest_target(2, CAIRN.rests["moments"], CAIRN.clock) == 3
    assert rest_target(1, CAIRN.rests["night"], CAIRN.clock) == 2 * HPP


@pytest.mark.parametrize(
    ("lengths", "clock", "message"),
    [
        (
            {"night": {"hours": 8, "full": True, "stop_at": {"morning": "night"}}},
            CAIRN.clock,
            "'stop_at' needs 'until'",
        ),
        (
            {"night": {"until": "morning", "full": True, "stop_at": {"morning": "night"}}},
            None,
            "'stop_at' must be an object of parts of the day",
        ),
        (
            {"night": {"until": "morning", "full": True, "stop_at": {"morning": "midnight"}}},
            CAIRN.clock,
            "names no part of the day",
        ),
        (
            {"night": {"until": "morning", "full": True, "stop_at": ["night"]}},
            CAIRN.clock,
            "must be an object of parts of the day",
        ),
    ],
)
def test_a_bad_stop_at_is_rejected(lengths, clock, message):
    definition = Definition(type="rest", id="rest", data={"lengths": lengths})
    with pytest.raises(ContentError, match=message):
        _load_rests([definition], None, "testset", clock)


def test_advancing_a_day_lands_on_the_morning_and_a_part_moves_to_the_afternoon(client_factory):
    _, _, tools = _fight(client_factory)
    assert tools.advance_time(days=1) == {
        "day": 2,
        "part": "morning",
        "hour": 0,
        "notice": "1 day passed; update anything that should be updated.",
    }
    assert tools.advance_time(parts=1) == {"day": 2, "part": "afternoon", "hour": 0}


def test_a_clock_move_into_a_new_day_is_a_day_end(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    before = _last_event_id(app, campaign_id)
    tools.advance_time(days=1)
    assert _turn_has_day_end(app.state.engine, campaign_id, before)


def test_a_clock_move_within_the_same_day_is_not_a_day_end(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    before = _last_event_id(app, campaign_id)
    assert tools.advance_time(parts=1)["day"] == 1
    assert not _turn_has_day_end(app.state.engine, campaign_id, before)


def test_hours_carry_into_the_next_part_of_the_day(client_factory):
    _, _, tools = _fight(client_factory)
    assert tools.advance_time(hours=HPP + 2) == {"day": 1, "part": CAIRN.clock.parts[1], "hour": 2}


def test_advancing_by_hours_alone_is_allowed(client_factory):
    _, _, tools = _fight(client_factory)
    assert tools.advance_time(hours=1)["hour"] == 1


def test_advancing_by_nothing_is_refused(client_factory):
    _, _, tools = _fight(client_factory)
    with pytest.raises(InvalidInput):
        tools.advance_time()


def test_advancing_by_a_year_or_more_is_refused(client_factory):
    _, _, tools = _fight(client_factory)
    with pytest.raises(InvalidInput):
        tools.advance_time(days=366)


def test_a_night_rest_heals_a_wounded_character(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _wound(app, campaign_id)
    result = tools.rest("night", reason="camp")
    assert _character(app, campaign_id, PC).harm.get("hp", 0) == 0
    assert PC in result["recovered"]


def test_a_night_rest_notice_says_the_rest_is_over_not_a_day_passed(client_factory):
    _, _, tools = _fight(client_factory)
    result = tools.rest("night", reason="camp")
    assert result["notice"].startswith(
        "the rest is over and time has passed; update anything that should be updated."
    )


def test_a_deprived_character_recovers_nothing_on_a_rest(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _set_condition(tools, PC, True, cause="no food")
    wound = _wound(app, campaign_id)
    result = tools.rest("night", reason="camp")
    assert _character(app, campaign_id, PC).harm.get("hp", 0) == wound
    assert PC in result["held_back"]
    assert PC not in result["recovered"]


def test_an_interrupted_night_rest_becomes_a_moments_rest(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    before = _clock_position(app, campaign_id)
    result = tools.rest("night", interrupted=True, reason="ambush")
    assert result["length"] == "moments"
    assert result["interrupted"] is True
    assert _clock_position(app, campaign_id) - before == CAIRN.rests["moments"].hours


def test_deprivation_costs_one_fatigue_each_day_after_its_first(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _set_condition(tools, PC, True, cause="no food")
    tools.advance_time(days=1)
    assert not [i for i in _character(app, campaign_id, PC).items if i.kind == "fatigue"]
    tools.advance_time(days=1)
    assert [i for i in _character(app, campaign_id, PC).items if i.kind == "fatigue"]


def test_a_deprived_character_is_refused_manual_fatigue_unless_forced(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _set_condition(tools, PC, True, cause="no food")
    with pytest.raises(
        InvalidInput,
        match="is held back by no food, so set force only if something besides that causes this Fatigue",
    ):
        tools.add_penalty(PC, "a shock")
    assert not [i for i in _character(app, campaign_id, PC).items if i.kind == "fatigue"]
    tools.add_penalty(PC, "a shock", force=True)
    assert [i for i in _character(app, campaign_id, PC).items if i.kind == "fatigue"]


def test_a_night_rest_clears_fatigue_from_a_character_who_is_not_deprived(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.add_penalty(PC, "spell")
    assert [i for i in _character(app, campaign_id, PC).items if i.kind == "fatigue"]
    tools.rest("night", reason="camp")
    assert not [i for i in _character(app, campaign_id, PC).items if i.kind == "fatigue"]


def test_deprivation_needs_a_cause(client_factory):
    _, _, tools = _fight(client_factory)
    with pytest.raises(InvalidInput):
        _set_condition(tools, PC, True)


def test_setting_deprivation_again_keeps_the_first_day(client_factory):
    _, _, tools = _fight(client_factory)
    first = _set_condition(tools, PC, True, cause="no food")
    tools.advance_time(days=1)
    again = _set_condition(tools, PC, True, cause="thirst")
    assert first["since_day"] == 1
    assert again["since_day"] == 1


def test_rest_history_lists_the_newest_rest_first_with_its_reason(client_factory):
    _, _, tools = _fight(client_factory)
    tools.rest("night", reason="camp")
    tools.rest("moments", reason="watch")
    rests = tools.rest_history()["rests"]
    assert [r["reason"] for r in rests] == ["watch", "camp"]


def test_a_rest_line_ends_with_one_full_stop_even_if_the_reason_has_one(client_factory):
    line = RestTaken(
        length="night", full=True, interrupted=False, reason="camp.", day=1, part="morning"
    ).render()
    assert line == "The party rests (night): camp."


def test_a_discovered_place_nobody_visits_goes_stale_after_five_days(client_factory):
    _, _, tools = _fight(client_factory)
    tools.create_location("Old Mill", "A ruined mill.")
    tools.discover_location("Old Mill")
    result = tools.advance_time(days=5)
    assert "Old Mill" in result["stale_places"]


def test_a_day_advance_tells_the_warden_to_update_the_world(client_factory):
    _, _, tools = _fight(client_factory)
    tools.create_location("Old Mill", "A ruined mill.")
    tools.discover_location("Old Mill")
    notice = tools.advance_time(days=5)["notice"]
    assert notice.startswith("5 days passed; update anything that should be updated.")
    assert "Old Mill has not been visited in 5 days, update it." in notice


def test_a_part_of_a_day_gives_no_notice(client_factory):
    _, _, tools = _fight(client_factory)
    assert "notice" not in tools.advance_time(parts=1)


def test_a_place_a_character_stays_in_is_not_stale(client_factory):
    _, _, tools = _fight(client_factory)
    tools.discover_location(YARD)
    assert "stale_places" not in tools.advance_time(days=5)


def test_a_night_rest_does_not_feed_or_deprive_an_npc(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.create_npc("Blink Dog", YARD, "A beast.")
    tools.rest("night", reason="camp")
    npc = _character(app, campaign_id, "blink dog")
    assert "deprived" not in npc.marks
    assert not [i for i in npc.items if i.name.lower() == "rations"]
