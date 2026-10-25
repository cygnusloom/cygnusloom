from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusforge.db.models import Event
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import (
    NPC_KIND,
    PC_KIND,
    GoldChanged,
    load_state,
    record,
)
from cygnusforge.services.world import scene_view, world_state_text
from tests.test_campaigns import _create_character
from tests.test_character_creation import _cairn_campaign, _complete_form
from tests.test_combat import CAIRN, PC, _character, _tools

MARKET = CAIRN.marketplace
PLACE = "The Road"
HIRELING = "Wen"
SECOND = "Mags"
NPC = "Rook"
WAGE = 3


def _pcs(client_factory: Callable[[], TestClient]):
    client, campaign_id = _cairn_campaign(client_factory)
    created = _create_character(
        client, campaign_id, name=PC, fields=_complete_form(client, campaign_id)
    )
    assert created.status_code == 303
    tools = _tools(client.app, campaign_id)
    tools.create_location(PLACE, "A road between towns.")
    tools.move_actor(PC, PLACE)
    return client.app, campaign_id, tools


def _gold(app: FastAPI, campaign_id: int, name: str = PC) -> int:
    return _character(app, campaign_id, name).features.get(MARKET.currency, 0)


def _set_gold(app: FastAPI, campaign_id: int, amount: int, name: str = PC) -> None:
    """Make the character's gold exactly `amount`."""
    with Session(app.state.engine) as db:
        record(
            db,
            campaign_id,
            GoldChanged(name=name, feature=MARKET.currency, amount=-10_000, cause="a test"),
        )
        record(
            db,
            campaign_id,
            GoldChanged(name=name, feature=MARKET.currency, amount=amount, cause="a test"),
        )
        db.commit()


def _hire(tools, name: str = HIRELING, wage: int = WAGE, employer: str = PC):
    return tools.hire_hireling(name, employer, wage, "A hired hand, good with a rope.")


def _events(app: FastAPI, campaign_id: int, kind: str) -> list[Event]:
    with Session(app.state.engine) as db:
        rows = db.exec(
            select(Event).where(Event.campaign_id == campaign_id).order_by(Event.id)
        ).all()
    return [row for row in rows if row.kind == kind]


def test_a_hireling_joins_the_company_under_player_control(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    result = _hire(tools)
    assert result["hired"] == HIRELING
    hireling = _character(app, campaign_id, HIRELING)
    assert hireling.kind == PC_KIND
    assert hireling.hireling is True
    assert hireling.employer == PC
    assert hireling.wage == WAGE
    assert hireling.location == PLACE
    assert hireling.stats


def test_the_player_character_is_still_the_employer_not_the_hireling(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _hire(tools)
    with Session(app.state.engine) as db:
        state = load_state(db, campaign_id)
    assert state.pc().name == PC
    assert not state.pc().hireling


def test_only_a_player_character_can_hire(client_factory):
    _app, _campaign_id, tools = _pcs(client_factory)
    tools.create_npc(NPC, PLACE, "A stranger on the road.")
    with pytest.raises(InvalidInput):
        _hire(tools, employer=NPC)
    with pytest.raises(InvalidInput):
        _hire(tools, employer="Nobody")
    _hire(tools)
    with pytest.raises(InvalidInput):
        _hire(tools, name=SECOND, employer=HIRELING)


def test_a_negative_or_fractional_wage_is_refused(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    with pytest.raises(InvalidInput):
        _hire(tools, wage=-1)
    with pytest.raises(InvalidInput):
        _hire(tools, wage=True)
    assert _events(app, campaign_id, "hireling.added") == []


def test_a_day_passing_takes_the_wage_from_the_employer(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _set_gold(app, campaign_id, 20)
    _hire(tools)
    tools.advance_time(days=1)
    assert _gold(app, campaign_id) == 20 - WAGE
    paid = _events(app, campaign_id, "character.gold_changed")
    assert any(f"wages for {HIRELING}" in row.payload for row in paid)


def test_a_part_of_a_day_does_not_pay_the_wage(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _set_gold(app, campaign_id, 20)
    _hire(tools)
    tools.advance_time(parts=1)
    assert _gold(app, campaign_id) == 20


def test_each_new_day_pays_once_and_a_zero_wage_pays_nothing(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _set_gold(app, campaign_id, 20)
    _hire(tools)
    _hire(tools, name=SECOND, wage=0)
    tools.advance_time(days=2)
    assert _gold(app, campaign_id) == 20 - 2 * WAGE


def test_an_employer_who_cannot_pay_loses_nothing_and_the_hireling_stays(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _set_gold(app, campaign_id, WAGE - 1)
    _hire(tools)
    tools.advance_time(days=1)
    assert _gold(app, campaign_id) == WAGE - 1
    unpaid = _events(app, campaign_id, "hireling.unpaid")
    assert len(unpaid) == 1
    assert _character(app, campaign_id, HIRELING).hireling is True


def test_two_hirelings_share_the_employers_gold_and_the_second_goes_unpaid(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _set_gold(app, campaign_id, WAGE)
    _hire(tools)
    _hire(tools, name=SECOND)
    tools.advance_time(days=1)
    assert _gold(app, campaign_id) == 0
    unpaid = _events(app, campaign_id, "hireling.unpaid")
    assert len(unpaid) == 1
    assert SECOND in unpaid[0].payload


def test_dismissing_a_hireling_makes_it_an_npc_that_stays_put(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _set_gold(app, campaign_id, 0)
    _hire(tools)
    tools.advance_time(days=1)
    result = tools.dismiss_hireling(HIRELING, "its wage went unpaid")
    assert result == {"dismissed": HIRELING, "employer": PC}
    left = _character(app, campaign_id, HIRELING)
    assert left.kind == NPC_KIND
    assert left.hireling is False
    assert left.employer == ""
    assert left.wage == 0
    assert left.location == PLACE
    tools.advance_time(days=1)
    assert _gold(app, campaign_id) == 0
    assert len(_events(app, campaign_id, "hireling.unpaid")) == 1


def test_only_a_hireling_can_be_dismissed(client_factory):
    _app, _campaign_id, tools = _pcs(client_factory)
    tools.create_npc(NPC, PLACE, "A stranger on the road.")
    with pytest.raises(InvalidInput):
        tools.dismiss_hireling(NPC, "no reason")
    with pytest.raises(InvalidInput):
        tools.dismiss_hireling(PC, "no reason")


def test_the_gm_is_told_about_the_hireling_and_its_wage(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _hire(tools)
    with Session(app.state.engine) as db:
        state = load_state(db, campaign_id)
        text = world_state_text(state, CAIRN.campaign, clock_rules=CAIRN.clock)
    assert f"{HIRELING}, at {PLACE}, hired by {PC} at {WAGE} a day" in text


def test_a_hireling_keeps_the_pronouns_the_gm_gives_and_the_gm_is_told(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    tools.hire_hireling(HIRELING, PC, WAGE, "A hired hand, good with a rope.", pronouns="they/them")
    assert _character(app, campaign_id, HIRELING).pronouns == "they/them"
    with Session(app.state.engine) as db:
        state = load_state(db, campaign_id)
        text = world_state_text(state, CAIRN.campaign, clock_rules=CAIRN.clock)
    assert f"Pronouns: {HIRELING}, they/them." in text


def test_a_hireling_hired_without_pronouns_has_none(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _hire(tools)
    assert _character(app, campaign_id, HIRELING).pronouns is None


def test_a_hireling_travels_with_its_employer(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _hire(tools)
    tools.create_location("The Inn", "A warm room with a fire.")
    tools.move_actor(PC, "The Inn")
    assert _character(app, campaign_id, HIRELING).location == "The Inn"
    scene = _scene(app, campaign_id)
    assert [h.name for h in scene.hirelings] == [HIRELING]


def test_a_hireling_is_listed_on_the_players_scene_only_where_it_is(client_factory):
    app, campaign_id, tools = _pcs(client_factory)
    _hire(tools)
    assert [h.name for h in _scene(app, campaign_id).hirelings] == [HIRELING]
    tools.create_location("The Inn", "A warm room with a fire.")
    tools.move_actor(HIRELING, "The Inn")
    assert _scene(app, campaign_id).hirelings == ()


def _scene(app: FastAPI, campaign_id: int):
    with Session(app.state.engine) as db:
        state = load_state(db, campaign_id)
    return scene_view(state, CAIRN.character)


def test_the_gm_can_voice_a_hireling_at_the_players_location(client_factory):
    _, _, tools = _pcs(client_factory)
    _hire(tools)
    profile = tools.speaker_profile(HIRELING)
    assert profile.splitlines()[0] == f"Speaker: {HIRELING}"
    assert f"Location: {PLACE}" in profile


def test_a_hireling_away_from_the_player_is_not_present_but_can_be_spoken_for_with_force(
    client_factory,
):
    _, _, tools = _pcs(client_factory)
    _hire(tools)
    tools.create_location("The Inn", "A warm room with a fire.")
    tools.move_actor(HIRELING, "The Inn")
    assert tools.speaker_at_player(HIRELING) is False
    assert tools.speaker_profile(HIRELING).splitlines()[0] == f"Speaker: {HIRELING}"


def test_the_player_character_still_cannot_be_spoken_for(client_factory):
    _, _, tools = _pcs(client_factory)
    _hire(tools)
    with pytest.raises(InvalidInput, match="does not need speaking for"):
        tools.speaker_profile(PC)
