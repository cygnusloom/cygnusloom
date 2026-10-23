from __future__ import annotations

import json
import random

import pytest
from fastapi import FastAPI
from sqlmodel import Session, select

from cygnusforge.db.models import Event
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.death import begin_dying
from cygnusforge.services.injuries import EffectContext, run_effects
from tests.test_combat import (
    CAIRN,
    PC,
    _character,
    _fight,
    _foe,
    _knock_out_pc,
    _set_stat,
)
from tests.test_world import _db


def _events(app: FastAPI, campaign_id: int, kind: str) -> list[Event]:
    with Session(app.state.engine) as db:
        rows = db.exec(
            select(Event).where(Event.campaign_id == campaign_id).order_by(Event.id)
        ).all()
    return [row for row in rows if row.kind == kind]


def _add_condition_by_effect(app, campaign_id: int, condition: str) -> None:
    """Give a condition the way an injury does, so the death clock starts as it does in play."""
    with _db(app) as db:
        run_effects(
            db,
            campaign_id,
            PC,
            [{"op": "condition", "id": condition}],
            EffectContext(rng=random.Random(0), combat=CAIRN.combat, picked={}),
            "a test",
        )


def test_critical_damage_starts_the_one_hour_clock(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _knock_out_pc(app, campaign_id, tools)
    character = _character(app, campaign_id, PC)
    assert character.defeated is True
    assert character.dying_hours == CAIRN.combat.dying_hours == 1
    assert character.dead is False


def test_a_dying_character_dies_when_its_hour_runs_out(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _knock_out_pc(app, campaign_id, tools)
    tools.advance_time(hours=1)
    character = _character(app, campaign_id, PC)
    assert character.dead is True
    assert character.dying_hours is None


def test_a_stabilized_character_survives_the_hour(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _knock_out_pc(app, campaign_id, tools)
    result = tools.stabilize(PC, "A healer's bandages hold.")
    assert result["stabilized"] is True
    tools.advance_time(hours=5)
    character = _character(app, campaign_id, PC)
    assert character.dead is False
    assert character.dying_hours is None


def test_stabilize_refuses_a_character_who_is_not_dying(client_factory):
    _app, _campaign_id, tools = _fight(client_factory)
    with pytest.raises(InvalidInput, match="is not dying"):
        tools.stabilize(PC, "Lucky.")


def test_a_bandage_stabilizes_a_dying_character_and_is_spent(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.give_item(PC, "Bandages")
    _knock_out_pc(app, campaign_id, tools)
    result = tools.use_item(PC, "Bandages", PC)
    assert result["stabilized"]["stabilized"] is True
    assert _character(app, campaign_id, PC).dying_hours is None


def test_a_singular_bandage_name_gets_the_listed_uses_and_stabilizes(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    tools.give_item(PC, "Bandage")
    held = [i for i in _character(app, campaign_id, PC).items if i.name == "Bandages"]
    assert len(held) == 1 and held[0].max_uses == 3
    _knock_out_pc(app, campaign_id, tools)
    result = tools.use_item(PC, "Bandage", PC)
    assert result["stabilized"]["stabilized"] is True
    assert _character(app, campaign_id, PC).dying_hours is None


def test_a_bandage_on_someone_who_is_not_dying_is_refused_before_it_is_spent(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _foe(tools, "Bo")
    tools.give_item(PC, "Bandages")
    with pytest.raises(InvalidInput, match="is not dying"):
        tools.use_item(PC, "Bandages", "Bo")
    bandages = [i for i in _character(app, campaign_id, PC).items if i.name == "Bandages"]
    assert len(bandages) == 1 and bandages[0].uses == bandages[0].max_uses


def test_only_an_aid_item_may_be_used_on_someone_else(client_factory):
    _app, _campaign_id, tools = _fight(client_factory)
    tools.give_item(PC, "Rope")
    _foe(tools, "Bo")
    with pytest.raises(InvalidInput, match="can only be used on the character carrying it"):
        tools.use_item(PC, "Rope", "Bo")


def test_a_mortal_wound_starts_the_clock_and_aid_ends_it(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _add_condition_by_effect(app, campaign_id, "mortal_wound")
    character = _character(app, campaign_id, PC)
    assert "mortal_wound" in character.conditions
    assert character.dying_hours == 1
    tools.stabilize(PC, "A surgeon closes the wound.")
    character = _character(app, campaign_id, PC)
    assert "mortal_wound" not in character.conditions
    assert character.dying_hours is None


def test_a_mortal_wound_left_alone_kills_at_the_hour(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _add_condition_by_effect(app, campaign_id, "mortal_wound")
    tools.advance_time(hours=1)
    assert _character(app, campaign_id, PC).dead is True


def test_a_started_clock_is_kept_after_the_session_that_started_it_closes(client_factory):
    app, campaign_id, _tools = _fight(client_factory)
    with _db(app) as db:
        begin_dying(db, campaign_id, CAIRN.combat, PC)
    assert _character(app, campaign_id, PC).dying_hours == CAIRN.combat.dying_hours


def test_a_doomed_character_dies_at_once_on_a_failed_save_against_critical_damage(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _add_condition_by_effect(app, campaign_id, "doomed")
    _knock_out_pc(app, campaign_id, tools)
    character = _character(app, campaign_id, PC)
    assert character.dead is True
    assert character.dying_hours is None


def test_a_harmed_pc_whose_str_reaches_zero_outside_combat_dies(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _set_stat(app, campaign_id, PC, "str", 2)
    tools.harm_character(PC, "str", 2, "A fall down a well.")
    assert _character(app, campaign_id, PC).dead is True
    harmed = _events(app, campaign_id, "combat.harmed")[-1]
    assert json.loads(harmed.payload)["status"] == "dead"


def test_recovery_refuses_the_dead_and_the_dying(client_factory):
    app, campaign_id, tools = _fight(client_factory)
    _knock_out_pc(app, campaign_id, tools)
    with pytest.raises(InvalidInput, match="stabilize them first"):
        tools.recover_character(PC, "Lucky.", "Nothing.")
    tools.advance_time(hours=1)
    with pytest.raises(InvalidInput, match="dead and cannot be recovered"):
        tools.recover_character(PC, "Lucky.", "Nothing.")
