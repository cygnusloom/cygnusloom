from __future__ import annotations

import pytest

from cygnusforge.services.clock import absolute, rest_target
from cygnusforge.services.common import InvalidInput
from tests.test_combat import (
    CAIRN,
    PC,
    YARD,
    _character,
    _fight,
    _set_stat,
    _state,
)

PARTS = len(CAIRN.clock.parts)


def _wound(app, campaign_id, name=PC):
    _set_stat(app, campaign_id, name, "hp", 1)
    return _character(app, campaign_id, name).harm.get("hp", 0)


def _clock_position(app, campaign_id):
    return absolute(_state(app, campaign_id).clock, PARTS)


@pytest.mark.parametrize(
    ("rest_name", "start", "expected"),
    [("moments", 0, 1), ("night", 0, 4), ("night", 3, 4), ("night", 2, 4), ("week", 0, 28)],
)
def test_a_rest_ends_at_its_target_part(rest_name, start, expected):
    assert rest_target(start, CAIRN.rests[rest_name], PARTS) == expected


def test_advancing_a_day_lands_on_the_morning_and_a_part_moves_to_the_afternoon(client_factory):
    _, _, tools = _fight(client_factory)
    assert tools.advance_time(days=1) == {
        "day": 2,
        "part": "morning",
        "notice": "1 day passed; update anything that should be updated.",
    }
    assert tools.advance_time(parts=1) == {"day": 2, "part": "afternoon"}


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


def test_a_deprived_character_recovers_nothing_on_a_rest(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.set_deprivation(PC, True, cause="no food")
    wound = _wound(app, campaign_id)
    result = tools.rest("night", reason="camp")
    assert _character(app, campaign_id, PC).harm.get("hp", 0) == wound
    assert PC in result["deprived"]
    assert PC not in result["recovered"]


def test_an_interrupted_night_rest_becomes_a_moments_rest(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    before = _clock_position(app, campaign_id)
    result = tools.rest("night", interrupted=True, reason="ambush")
    assert result["length"] == "moments"
    assert result["interrupted"] is True
    assert _clock_position(app, campaign_id) - before == 1


def test_deprivation_costs_one_fatigue_each_day_after_its_first(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.set_deprivation(PC, True, cause="no food")
    tools.advance_time(days=1)
    assert not [i for i in _character(app, campaign_id, PC).items if i.kind == "fatigue"]
    tools.advance_time(days=1)
    assert [i for i in _character(app, campaign_id, PC).items if i.kind == "fatigue"]


def test_a_deprived_character_is_refused_manual_fatigue_unless_forced(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.set_deprivation(PC, True, cause="no food")
    with pytest.raises(InvalidInput, match="the engine adds their Fatigue each day"):
        tools.add_fatigue(PC, "a shock")
    assert not [i for i in _character(app, campaign_id, PC).items if i.kind == "fatigue"]
    tools.add_fatigue(PC, "a shock", force=True)
    assert [i for i in _character(app, campaign_id, PC).items if i.kind == "fatigue"]


def test_a_night_rest_clears_fatigue_from_a_character_who_is_not_deprived(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.add_fatigue(PC, "spell")
    assert [i for i in _character(app, campaign_id, PC).items if i.kind == "fatigue"]
    tools.rest("night", reason="camp")
    assert not [i for i in _character(app, campaign_id, PC).items if i.kind == "fatigue"]


def test_deprivation_needs_a_cause(client_factory):
    _, _, tools = _fight(client_factory)
    with pytest.raises(InvalidInput):
        tools.set_deprivation(PC, True)


def test_setting_deprivation_again_keeps_the_first_day(client_factory):
    _, _, tools = _fight(client_factory)
    first = tools.set_deprivation(PC, True, cause="no food")
    tools.advance_time(days=1)
    again = tools.set_deprivation(PC, True, cause="thirst")
    assert first["since_day"] == 1
    assert again["since_day"] == 1


def test_rest_history_lists_the_newest_rest_first_with_its_reason(client_factory):
    _, _, tools = _fight(client_factory)
    tools.rest("night", reason="camp")
    tools.rest("moments", reason="watch")
    rests = tools.rest_history()["rests"]
    assert [r["reason"] for r in rests] == ["watch", "camp"]


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
