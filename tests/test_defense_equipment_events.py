from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.db.models import Event
from cygnusloom.services.events import (
    PC_KIND,
    CampaignState,
    Channel,
    Character,
    DefenseEquipmentSet,
    HirelingAdded,
    decode,
    events_since,
    last_event_id,
    load_state,
    name_key,
    record,
    truncate,
)
from tests.factories import campaign_ready

NO_EQUIPMENT = ("", 0, "", 0, 0)


def _equipment(character: Character) -> tuple[str, int, str, int, int]:
    return (
        character.equipped_armor,
        character.equipped_armor_value,
        character.equipped_shield,
        character.equipped_shield_value,
        character.defense_adjustment,
    )


def _held(state: CampaignState, name: str) -> Character:
    return state.characters[name_key(name)]


def _ada_at(db: Session, campaign_id: int, before: int | None = None) -> Character:
    return _held(load_state(db, campaign_id, before=before), "Ada")


def _hireling(**values: Any) -> HirelingAdded:
    base: dict[str, Any] = {
        "name": "Wen",
        "employer": "Ada",
        "wage": 2,
        "location": "The Inn",
        "description": "A hired hand.",
        "personality": "",
        "accent": "",
        "quirks": [],
    }
    return HirelingAdded(**(base | values))


def test_the_event_is_stored_internally_with_its_payload_and_kept_out_of_the_log(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = campaign_ready(client_factory)
    event = DefenseEquipmentSet(
        name="Ada",
        equipped_armor="Scale",
        equipped_armor_value=3,
        equipped_shield="Buckler",
        equipped_shield_value=1,
        defense_adjustment=-1,
    )
    with Session(app.state.engine) as db:
        event_id = record(db, campaign_id, event)
        db.commit()

    with Session(app.state.engine) as db:
        row = db.get(Event, event_id)
        assert row.kind == "combat.defense_equipment_set"
        assert row.channel == Channel.INTERNAL
        assert json.loads(row.payload) == asdict(event)
        assert decode(row) == event
        internal = {r.id for r in events_since(db, campaign_id, channel=Channel.INTERNAL)}
        logged = {r.id for r in events_since(db, campaign_id, channel=Channel.LOG)}
    assert event_id in internal
    assert event_id not in logged


def test_replay_gives_the_equipment_and_tracking_in_force_at_each_point(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = campaign_ready(client_factory)
    with Session(app.state.engine) as db:
        equipped = record(
            db,
            campaign_id,
            DefenseEquipmentSet(name="Ada", equipped_armor="Leather", equipped_armor_value=2),
        )
        swapped = record(
            db,
            campaign_id,
            DefenseEquipmentSet(
                name="Ada",
                equipped_armor="Scale",
                equipped_armor_value=3,
                equipped_shield="Buckler",
                equipped_shield_value=1,
                defense_adjustment=-1,
            ),
        )
        cleared = record(db, campaign_id, DefenseEquipmentSet(name="Ada"))
        db.commit()

    with Session(app.state.engine) as db:
        before_equipping = _ada_at(db, campaign_id, before=equipped)
        assert _equipment(before_equipping) == NO_EQUIPMENT
        assert before_equipping.defense_tracked is False

        leather = _ada_at(db, campaign_id, before=swapped)
        assert _equipment(leather) == ("Leather", 2, "", 0, 0)
        assert leather.defense_tracked is True

        scale = _ada_at(db, campaign_id, before=cleared)
        assert _equipment(scale) == ("Scale", 3, "Buckler", 1, -1)
        assert scale.defense_tracked is True

        final = _ada_at(db, campaign_id)
        assert _equipment(final) == NO_EQUIPMENT
        assert final.defense_tracked is True


def test_truncating_the_log_rolls_equipment_and_tracking_back(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = campaign_ready(client_factory)
    with Session(app.state.engine) as db:
        mark = last_event_id(db, campaign_id)
        equipped = record(
            db,
            campaign_id,
            DefenseEquipmentSet(name="Ada", equipped_armor="Leather", equipped_armor_value=2),
        )
        record(
            db,
            campaign_id,
            DefenseEquipmentSet(name="Ada", equipped_armor="Scale", equipped_armor_value=3),
        )
        db.commit()

    with Session(app.state.engine) as db:
        truncate(db, campaign_id, equipped)
        db.commit()
    with Session(app.state.engine) as db:
        ada = _ada_at(db, campaign_id)
        assert _equipment(ada) == ("Leather", 2, "", 0, 0)
        assert ada.defense_tracked is True

    with Session(app.state.engine) as db:
        truncate(db, campaign_id, mark)
        db.commit()
    with Session(app.state.engine) as db:
        ada = _ada_at(db, campaign_id)
        assert _equipment(ada) == NO_EQUIPMENT
        assert ada.defense_tracked is False


def test_a_hireling_replays_with_the_counters_and_armor_it_was_generated_with(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    _, campaign_id = campaign_ready(client_factory)
    hired = _hireling(counters={"stamina": 3}, armor=4)
    with Session(app.state.engine) as db:
        hire_id = record(db, campaign_id, hired)
        db.commit()

    with Session(app.state.engine) as db:
        row = db.get(Event, hire_id)
        assert json.loads(row.payload) == asdict(hired)
        assert decode(row) == hired
        before_hire = load_state(db, campaign_id, before=hire_id)
        wen = _held(load_state(db, campaign_id), "Wen")
    assert name_key("Wen") not in before_hire.characters
    assert wen.kind == PC_KIND
    assert wen.hireling is True
    assert wen.counters == {"stamina": 3}
    assert wen.armor == 4
    assert wen.defense_tracked is False
