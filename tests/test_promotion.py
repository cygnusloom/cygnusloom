from __future__ import annotations

import random
from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusloom.content.character import NO_CAMPAIGN
from cygnusloom.db.models import Event
from cygnusloom.services import world
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import PC_KIND, load_state
from cygnusloom.services.injuries import EffectContext, run_effects
from tests.test_campaigns import _create_character
from tests.test_character_creation import _complete_form
from tests.test_combat import CAIRN, PC, _character, _fight, _fight_client, _knock_out_pc
from tests.test_world import _db

HIRELING = "Wen"
WAGE = 3
NEW_PC = "Ada"


def _condition(app: FastAPI, campaign_id: int, name: str, condition: str) -> None:
    with _db(app) as db:
        run_effects(
            db,
            campaign_id,
            name,
            [{"op": "condition", "id": condition}],
            EffectContext(rng=random.Random(0), combat=CAIRN.combat, picked={}),
            "a test",
            plus=CAIRN.plus,
        )


def _kill_pc(app: FastAPI, campaign_id: int, tools) -> None:
    _knock_out_pc(app, campaign_id, tools)
    tools.advance_time(hours=1)
    assert _character(app, campaign_id, PC).dead is True


def _hired_pc_dead(client_factory: Callable[[], TestClient]):
    app, campaign_id, tools = _fight(client_factory)
    tools.hire_hireling(HIRELING, PC, WAGE, "A hired hand, good with a rope.")
    _kill_pc(app, campaign_id, tools)
    return app, campaign_id, tools


def _events(app: FastAPI, campaign_id: int, kind: str) -> list[Event]:
    with Session(app.state.engine) as db:
        rows = db.exec(
            select(Event).where(Event.campaign_id == campaign_id).order_by(Event.id)
        ).all()
    return [row for row in rows if row.kind == kind]


def test_a_hireling_cannot_be_promoted_while_the_player_character_is_alive(client_factory):
    _app, _campaign_id, tools = _fight(client_factory)
    tools.hire_hireling(HIRELING, PC, WAGE, "A hired hand, good with a rope.")
    with pytest.raises(InvalidInput, match="still alive"):
        tools.promote_hireling(HIRELING)


def test_only_a_hireling_can_be_promoted(client_factory):
    _app, _campaign_id, tools = _hired_pc_dead(client_factory)
    with pytest.raises(InvalidInput, match="is not a hireling"):
        tools.promote_hireling(PC)
    with pytest.raises(InvalidInput, match="is not a hireling"):
        tools.promote_hireling("Nobody")


def test_a_dying_or_dead_hireling_cannot_be_promoted(client_factory):
    app, campaign_id, tools = _hired_pc_dead(client_factory)
    _condition(app, campaign_id, HIRELING, "mortal_wound")
    assert _character(app, campaign_id, HIRELING).dying_hours is not None
    with pytest.raises(InvalidInput):
        tools.promote_hireling(HIRELING)
    tools.advance_time(hours=1)
    assert _character(app, campaign_id, HIRELING).dead is True
    with pytest.raises(InvalidInput):
        tools.promote_hireling(HIRELING)


def test_a_promoted_hireling_keeps_its_pronouns(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.hire_hireling(HIRELING, PC, WAGE, "A hired hand, good with a rope.", pronouns="she/her")
    _kill_pc(app, campaign_id, tools)
    tools.promote_hireling(HIRELING)
    assert _character(app, campaign_id, HIRELING).pronouns == "she/her"


def test_a_living_hireling_takes_over_after_the_player_character_dies(client_factory):
    app, campaign_id, tools = _hired_pc_dead(client_factory)
    result = tools.promote_hireling(HIRELING)
    assert result == {"promoted": HIRELING, "employer": PC}
    hireling = _character(app, campaign_id, HIRELING)
    assert hireling.hireling is False
    assert hireling.employer == ""
    assert hireling.wage == 0
    with Session(app.state.engine) as db:
        state = load_state(db, campaign_id)
    assert state.pc().name == HIRELING


def test_a_promoted_hireling_is_no_longer_paid_by_the_former_employer(client_factory):
    app, campaign_id, tools = _hired_pc_dead(client_factory)
    tools.promote_hireling(HIRELING)
    before = len([e for e in _events(app, campaign_id, "hireling.unpaid") if HIRELING in e.payload])
    tools.advance_time(days=1)
    unpaid = [e for e in _events(app, campaign_id, "hireling.unpaid") if HIRELING in e.payload]
    assert len(unpaid) == before
    paid = [
        e for e in _events(app, campaign_id, "character.currency_changed") if HIRELING in e.payload
    ]
    assert not any(f"wages for {HIRELING}" in e.payload for e in paid)


def test_a_new_player_character_is_refused_while_one_lives_and_allowed_after(
    client_factory,
):
    client, campaign_id, tools = _fight_client(client_factory)
    tools.hire_hireling(HIRELING, PC, WAGE, "A hired hand, good with a rope.")
    with _db(client.app) as db, pytest.raises(InvalidInput, match="already exists"):
        world.create_player_character(
            db,
            campaign_id,
            CAIRN.campaign,
            CAIRN.character,
            NEW_PC,
            {},
            {},
            random.Random(0),
            plus=CAIRN.plus,
            pronouns="she/her",
        )
    _kill_pc(client.app, campaign_id, tools)
    created = _create_character(
        client, campaign_id, name=NEW_PC, fields=_complete_form(client, campaign_id)
    )
    assert created.status_code == 303
    with _db(client.app) as db:
        state = load_state(db, campaign_id)
    assert state.pc().name == NEW_PC
    assert state.pc().kind == PC_KIND


def test_the_gm_is_told_the_dead_player_character_body_location(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _kill_pc(app, campaign_id, tools)
    with _db(app) as db:
        state = load_state(db, campaign_id)
    text = world.world_state_text(state, NO_CAMPAIGN)
    assert "Player character " + PC + " is dead" in text
    assert "the body lies at" in text
