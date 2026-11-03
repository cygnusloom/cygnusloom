from __future__ import annotations

from dataclasses import replace

import pytest

from cygnusforge.content.library import SaveRules
from cygnusforge.services import encounters
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import FOES_SIDE, MoraleChecked
from tests.test_combat import (
    PC,
    YARD,
    Rolls,
    _character,
    _fight,
    _finish_gm_reply,
    _foe,
    _hit,
    _set_stat,
    save_succeeds,
)
from tests.test_world import _db, _state

LEADER_WIL = 15


def _monster(tools, name: str, wil: int, claw: str = "2d6") -> None:
    tools.create_monster(
        name,
        YARD,
        description="A foe.",
        stats={"hp": 6, "str": 10, "dex": 10, "wil": wil},
        armor=0,
        attacks=[{"name": "Claw", "dice": claw}],
    )
    tools.add_participant(name)


def _morale(app, campaign_id: int) -> dict[str, list[str]]:
    return _state(app, campaign_id).encounter.morale


def test_a_lone_foe_checks_morale_at_zero_hp_not_before(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Bo", hp=6)
    result = replace(tools, rng=Rolls(3)).player_attack("Bo", "Sword")
    assert "morale" not in result
    assert _morale(app, campaign_id) == {}

    result = replace(tools, rng=Rolls(3, 1)).player_attack("Bo", "Sword")
    assert result["morale"] == [
        {
            "side": FOES_SIDE,
            "tag": "first",
            "leader": "Bo",
            "stat": "wil",
            "roll": 1,
            "score": 10,
            "passed": True,
            "fled": [],
        }
    ]
    assert _character(app, campaign_id, "Bo").defeated is False
    assert _morale(app, campaign_id) == {FOES_SIDE: ["first"]}


def test_a_lone_foe_that_breaks_flees_and_the_fight_ends(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Bo", hp=6)
    result = replace(tools, rng=Rolls(6, 20)).player_attack("Bo", "Sword")
    assert result["morale"][0]["passed"] is False
    assert result["morale"][0]["fled"] == ["Bo"]
    assert _state(app, campaign_id).encounter is None
    assert _character(app, campaign_id, "Bo").fled is False


@pytest.mark.parametrize(("save_roll", "passed"), [(1, False), (20, True)])
def test_a_morale_check_follows_a_declarative_roll_over_rule(client_factory, save_roll, passed):
    _, _, tools = _fight(client_factory)
    assert tools.combat is not None
    tools = replace(tools, combat=replace(tools.combat, save=SaveRules(mode="roll_over")))
    _foe(tools, "Bo", hp=6)

    result = replace(tools, rng=Rolls(6, save_roll)).player_attack("Bo", "Sword")

    assert result["morale"][0]["passed"] is passed


def test_a_group_checks_at_its_first_casualty_with_the_leaders_wil(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _monster(tools, "Grave Thing", wil=LEADER_WIL)
    _foe(tools, "Bo")
    _monster(tools, "Cy", wil=3)
    encounter = _state(app, campaign_id).encounter
    expected_leader = next(n for n in encounter.order if n in ("Grave Thing", "Cy"))
    wil = {"Grave Thing": LEADER_WIL, "Cy": 3}[expected_leader]
    roll = 12 if wil == LEADER_WIL else 4
    result = _hit(tools, "Grave Thing", "Bo", 6, 5, 20, roll)
    check = result["morale"][0]
    assert check["leader"] == expected_leader
    assert check["score"] == wil
    assert check["stat"] == "wil"
    assert check["passed"] is save_succeeds(roll, wil)


def test_a_group_of_two_makes_a_separate_check_for_first_and_half(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _monster(tools, "Grave Thing", wil=10)
    _foe(tools, "Bo")
    result = _hit(tools, "Grave Thing", "Bo", 6, 5, 20, 1, 1)
    assert [check["tag"] for check in result["morale"]] == ["first", "half"]
    assert _morale(app, campaign_id) == {FOES_SIDE: ["first", "half"]}


def test_a_failed_first_check_makes_the_side_flee_so_no_half_check_follows(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _monster(tools, "Grave Thing", wil=10)
    _foe(tools, "Bo")
    result = _hit(tools, "Grave Thing", "Bo", 6, 5, 20, 20)
    assert [check["tag"] for check in result["morale"]] == ["first"]
    assert result["morale"][0]["passed"] is False
    assert _state(app, campaign_id).encounter is None


def test_a_group_checks_at_first_casualty_and_at_half_once_each(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _monster(tools, "Grave Thing", wil=10)
    _foe(tools, "Bo")
    _foe(tools, "Cy")
    _foe(tools, "Dee")
    first = _hit(tools, "Grave Thing", "Bo", 6, 5, 20, 1)
    assert [check["tag"] for check in first["morale"]] == ["first"]
    second = _hit(tools, "Grave Thing", "Cy", 6, 5, 20, 1)
    assert [check["tag"] for check in second["morale"]] == ["half"]
    assert _morale(app, campaign_id) == {FOES_SIDE: ["first", "half"]}
    # Three of four are down and both tags are spent, so no morale die is rolled. Rolls(6, 5, 20)
    # would fail if one were.
    third = _hit(tools, "Grave Thing", "Dee", 6, 5, 20)
    assert "morale" not in third
    assert _morale(app, campaign_id) == {FOES_SIDE: ["first", "half"]}


def test_a_failed_group_check_flees_every_living_foe_and_ends_the_fight(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _monster(tools, "Grave Thing", wil=10)
    _foe(tools, "Bo")
    _foe(tools, "Cy")
    result = _hit(tools, "Grave Thing", "Bo", 6, 5, 20, 20)
    check = result["morale"][0]
    assert check["passed"] is False
    assert sorted(check["fled"]) == ["Cy", "Grave Thing"]
    assert _character(app, campaign_id, "Cy").fled is False
    assert _state(app, campaign_id).encounter is None


def test_a_player_character_is_never_checked_for_morale(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Bo", claw="4d6")
    _set_stat(app, campaign_id, PC, "hp", 0)
    result = _hit(tools, "Bo", PC, 6, 6, 6, 6)
    assert "morale" not in result
    assert _morale(app, campaign_id) == {}
    assert _character(app, campaign_id, PC).fled is False


def test_a_fled_character_cannot_attack(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing")
    _foe(tools, "Bo")
    _foe(tools, "Cy")
    replace(tools, rng=Rolls(10)).retreat("Bo", True)
    assert _character(app, campaign_id, "Bo").fled is True
    with pytest.raises(InvalidInput, match="Bo has fled the fight"):
        _hit(tools, "Bo", PC, 1)


def test_a_fled_foe_is_skipped_when_its_turn_comes_round(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing")
    _foe(tools, "Bo")
    _foe(tools, "Cy")
    replace(tools, rng=Rolls(10)).retreat("Bo", True)
    _finish_gm_reply(app, campaign_id)
    with _db(app) as db:
        encounters.begin_turn(db, campaign_id, PC)
        encounters.end_turn(db, campaign_id, PC, by_player=True)
        encounters.begin_turn(db, campaign_id, "Grave Thing")
        encounters.end_turn(db, campaign_id, "Grave Thing", by_player=False)
    state = _state(app, campaign_id).encounter
    assert state.order[state.up] == "Cy"


def test_the_fled_status_is_shown_for_a_fled_character(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing")
    _foe(tools, "Bo")
    _foe(tools, "Cy")
    replace(tools, rng=Rolls(10)).retreat("Bo", True)
    bo = _character(app, campaign_id, "Bo")
    assert encounters._status(bo, None) == "fled"


def test_a_surrendered_foe_leaves_the_fight(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Bo")
    _foe(tools, "Cy")
    assert tools.surrender("Bo") == {"character": "Bo", "surrendered": True}
    assert _character(app, campaign_id, "Bo").surrendered is True
    assert _state(app, campaign_id).encounter is not None


def test_the_encounter_ends_once_every_foe_has_surrendered(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Bo")
    tools.surrender("Bo")
    assert _state(app, campaign_id).encounter is None


def test_a_surrendered_character_cannot_attack_and_shows_as_surrendered(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Bo")
    _foe(tools, "Cy")
    tools.surrender("Bo")
    with pytest.raises(InvalidInput, match="Bo has surrendered"):
        _hit(tools, "Bo", PC, 1)
    assert encounters._status(_character(app, campaign_id, "Bo"), None) == "surrendered"


def test_a_defeated_character_shows_defeat_rather_than_fled(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Grave Thing", claw="2d6")
    _foe(tools, "Bo")
    _hit(tools, "Grave Thing", "Bo", 6, 5, 20, 1, 1)
    bo = _character(app, campaign_id, "Bo")
    assert bo.defeated is True
    assert encounters._status(bo, None) != "fled"


def test_an_npc_that_retreats_and_passes_flees_and_the_fight_ends(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Bo")
    result = replace(tools, rng=Rolls(10)).retreat("Bo", True)
    assert result == {"character": "Bo", "stat": "dex", "roll": 10, "score": 10, "passed": True}
    assert _character(app, campaign_id, "Bo").fled is False
    assert _state(app, campaign_id).encounter is None


def test_an_npc_that_retreats_and_fails_stays_in_the_fight(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Bo")
    _foe(tools, "Cy")
    result = replace(tools, rng=Rolls(11)).retreat("Bo", True)
    assert result["passed"] is False
    assert result["score"] == 10
    bo = _character(app, campaign_id, "Bo")
    assert bo.fled is False
    assert bo.defeated is False
    assert _state(app, campaign_id).encounter is not None


@pytest.mark.parametrize(("save_roll", "passed"), [(1, False), (20, True)])
def test_a_retreat_follows_a_declarative_roll_over_rule(client_factory, save_roll, passed):
    _, _, tools = _fight(client_factory)
    assert tools.combat is not None
    tools = replace(tools, combat=replace(tools.combat, save=SaveRules(mode="roll_over")))
    _foe(tools, "Bo")
    _foe(tools, "Cy")

    result = replace(tools, rng=Rolls(save_roll)).retreat("Bo", True)

    assert result["passed"] is passed


def test_a_retreat_without_a_safe_way_is_refused_before_any_roll(client_factory):
    _, _, tools = _fight(client_factory)
    _foe(tools, "Bo")
    with pytest.raises(InvalidInput, match="no safe way out"):
        replace(tools, rng=Rolls()).retreat("Bo", False)


def test_a_player_retreat_uses_the_players_roll_and_dex(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Bo")
    _foe(tools, "Cy")
    result = replace(tools, rng=Rolls()).player_retreat(True, 1)
    assert result["passed"] is True
    assert result["roll"] == 1
    assert result["stat"] == "dex"
    assert _state(app, campaign_id).encounter is None
    assert _character(app, campaign_id, PC).fled is False


def test_a_player_retreat_outcome_follows_the_save(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Bo")
    _foe(tools, "Cy")
    result = replace(tools, rng=Rolls()).player_retreat(True, 20)
    assert result["passed"] is save_succeeds(20, result["score"])
    assert result["passed"] is False
    assert _character(app, campaign_id, PC).fled is False
    assert _state(app, campaign_id).encounter is not None


def test_a_player_retreat_without_a_safe_way_is_refused(client_factory):
    _, _, tools = _fight(client_factory)
    _foe(tools, "Bo")
    with pytest.raises(InvalidInput, match="no safe way out"):
        replace(tools, rng=Rolls()).player_retreat(False, 1)


def test_an_npc_cannot_be_asked_to_use_the_player_retreat(client_factory):
    _, _, tools = _fight(client_factory)
    _foe(tools, "Bo")
    with pytest.raises(InvalidInput, match="use player_retreat"):
        replace(tools, rng=Rolls(1)).retreat(PC, True)


def test_morale_checked_renders_its_tag():
    event = MoraleChecked(
        side=FOES_SIDE, tag="half", leader="Grave Thing", stat="wil", roll=12, score=15, passed=True
    )
    assert event.render() == (
        "The foes side checks morale at the half casualties: Grave Thing rolls 12 "
        "on a WIL save of 15 and the side holds."
    )
