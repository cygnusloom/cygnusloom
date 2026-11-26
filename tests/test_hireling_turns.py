"""The campaign option that hands the hirelings' encounter turns to the GM."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusloom.db.models import Campaign, Event
from cygnusloom.services import encounters
from cygnusloom.services import turns as turns_service
from cygnusloom.services.campaigns import campaign_transcript, update_campaign_settings
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import ChatGM, EncounterTurnBegun, decode, load_state, record
from tests.factories import campaign_ready_fast, close_opening, send_message
from tests.test_campaign_settings import _tool_results, _tool_runs, _tool_script
from tests.test_combat import _fight_client, _foe
from tests.test_encounters import _npc_turn_stream
from tests.test_export_import import (
    _document,
    _export,
    _import,
    _imported_id,
    _target_app,
    _target_client,
)
from tests.test_survival_option import _post_settings

HIRELING = "Wen"
PC = "Mara"
NPC = "Pip"


def _flag(app: FastAPI, campaign_id: int) -> bool:
    with Session(app.state.engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        return campaign.gm_plays_hirelings


def _set_flag(app: FastAPI, campaign_id: int, enabled: bool) -> None:
    """Save the setting the way the Settings page does, including the follow-up it triggers."""
    with Session(app.state.engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        model, label = campaign.model, campaign.name
        inventory_space = campaign.use_inventory_space
        survival_needs = campaign.use_survival_needs
        owner_id = campaign.owner_id
    update_campaign_settings(
        app.state.engine,
        [model],
        owner_id,
        campaign_id,
        model=model,
        inventory_space=inventory_space,
        survival_needs=survival_needs,
        label=label,
        gm_plays_hirelings=enabled,
    )


def _state(app: FastAPI, campaign_id: int):
    with Session(app.state.engine) as db:
        return load_state(db, campaign_id)


def _begin(app: FastAPI, campaign_id: int, name: str) -> None:
    with Session(app.state.engine) as db:
        encounters.begin_turn(db, campaign_id, name)


def _end(app: FastAPI, campaign_id: int, name: str, by_player: bool) -> None:
    with Session(app.state.engine) as db:
        encounters.end_turn(db, campaign_id, name, by_player=by_player)


def _close_reply(app: FastAPI, campaign_id: int) -> None:
    """Close the GM reply an NPC turn opened, so the turn can be ended."""
    with Session(app.state.engine) as db:
        record(db, campaign_id, ChatGM(content="The turn plays out.", model="test"))
        db.commit()


def _fight_with_hireling(client_factory: Callable[[], TestClient], *, gm_plays: bool):
    """A Cairn fight with Mara, the hireling Wen, and the NPC Pip. Wen has rolled initiative, and
    the setting is on or off before anyone acts."""
    client, campaign_id, tools = _fight_client(client_factory)
    close_opening(client, campaign_id)
    tools.hire_hireling(HIRELING, PC, 0, "A hired hand, good with a rope.")
    tools.add_participant(HIRELING)
    tools.player_initiative(1)
    _foe(tools, NPC)
    if gm_plays:
        _set_flag(client.app, campaign_id, True)
    return client, campaign_id


def _turn_events(app: FastAPI, campaign_id: int, name: str) -> list[EncounterTurnBegun]:
    with Session(app.state.engine) as db:
        rows = db.exec(select(Event).where(Event.campaign_id == campaign_id).order_by(Event.id)).all()
    return [
        event
        for row in rows
        if (event := decode(row)).kind == "encounter.turn_begun" and event.name == name
    ]


def test_the_owner_turns_the_hireling_turn_setting_on_and_off(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = campaign_ready_fast(client_factory)
    with Session(client.app.state.engine) as db:
        label = db.get(Campaign, campaign_id).name

    response = _post_settings(
        client,
        campaign_id,
        model="fake/scripted",
        label=label,
        use_inventory_space="1",
        use_survival_needs="1",
        gm_plays_hirelings="1",
    )

    assert response.status_code == 303
    assert _flag(client.app, campaign_id) is True
    page = client.get(f"/campaigns/{campaign_id}").text
    assert 'name="gm_plays_hirelings" value="1" checked' in page

    response = _post_settings(
        client,
        campaign_id,
        model="fake/scripted",
        label=label,
        use_inventory_space="1",
        use_survival_needs="1",
    )

    assert response.status_code == 303
    assert _flag(client.app, campaign_id) is False


def test_the_gm_can_hand_itself_the_hirelings_turns(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = campaign_ready_fast(client_factory)

    response = send_message(
        client,
        campaign_id,
        _tool_script(
            "set_campaign_setting", {"setting": "hireling_turns", "value": True}
        ),
    )

    assert response.status_code == 303
    assert _flag(app, campaign_id) is True
    results = _tool_results(_tool_runs(app, campaign_id))
    assert any('"hireling_turns"' in result for result in results)


def test_a_gm_played_hireling_turn_waits_for_start_and_the_gm_ends_it(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _fight_with_hireling(client_factory, gm_plays=True)
    app = client.app

    _begin(app, campaign_id, PC)
    _end(app, campaign_id, PC, by_player=True)

    state = _state(app, campaign_id)
    assert state.encounter is not None
    assert state.encounter.acting is None
    assert state.encounter.order[state.encounter.up] == HIRELING
    page = client.get(f"/campaigns/{campaign_id}").text
    assert f"Start {HIRELING}'s turn" in page
    assert f"End {HIRELING}&#39;s turn" not in page

    lines = _npc_turn_stream(client, campaign_id, HIRELING)

    assert lines[-1]["type"] == "saved"
    state = _state(app, campaign_id)
    assert state.turn is None
    assert state.encounter is not None
    assert state.encounter.acting is None
    assert state.encounter.order[state.encounter.up] == NPC
    events = _turn_events(app, campaign_id, HIRELING)
    assert len(events) == 1
    assert events[0].gm_played is True
    page = client.get(f"/campaigns/{campaign_id}").text
    assert f"Start {NPC}'s turn" in page


def test_export_and_import_carry_the_hireling_turn_setting(
    client_factory: Callable[[], TestClient], tmp_path, settings
):
    client, campaign_id = campaign_ready_fast(client_factory)
    _set_flag(client.app, campaign_id, True)

    response = _export(client, campaign_id)

    document = _document(response)
    assert document["campaign"]["gm_plays_hirelings"] is True
    payload = response.content
    target = _target_client(_target_app(tmp_path, settings))
    imported_id = _imported_id(_import(target, payload))
    assert _flag(target.app, imported_id) is True


def test_an_export_without_the_setting_keeps_the_player_in_control(
    client_factory: Callable[[], TestClient], tmp_path, settings
):
    client, campaign_id = campaign_ready_fast(client_factory)
    document = _document(_export(client, campaign_id))
    del document["campaign"]["gm_plays_hirelings"]

    target = _target_client(_target_app(tmp_path, settings))
    imported_id = _imported_id(_import(target, json.dumps(document).encode()))

    assert _flag(target.app, imported_id) is False


def test_a_player_cannot_speak_for_a_hireling_the_gm_is_playing(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _fight_with_hireling(client_factory, gm_plays=True)
    app = client.app
    with Session(app.state.engine) as db:
        encounters.begin_turn(db, campaign_id, PC)
        encounters.end_turn(db, campaign_id, PC, by_player=True)
        encounters.begin_turn(db, campaign_id, HIRELING, gm_only=True)
        state = load_state(db, campaign_id)
        owner_id = db.get(Campaign, campaign_id).owner_id

    assert state.encounter is not None and state.encounter.gm_played is True
    with pytest.raises(InvalidInput, match="the GM plays it"):
        turns_service._check_encounter_turn(state, owner_id)


def test_turning_the_setting_on_hands_a_waiting_player_turn_to_the_gm(
    client_factory: Callable[[], TestClient],
):
    """The campaign in play can change its mind: a hireling turn already begun for the player
    gets a Start button, which hands it to the GM without repeating the turn in the Log."""
    client, campaign_id = _fight_with_hireling(client_factory, gm_plays=False)
    app = client.app

    _begin(app, campaign_id, PC)
    _end(app, campaign_id, PC, by_player=True)

    state = _state(app, campaign_id)
    assert state.encounter is not None and state.encounter.acting == HIRELING
    assert state.encounter.gm_played is False
    page = client.get(f"/campaigns/{campaign_id}").text
    assert f"End {HIRELING}&#39;s turn" in page
    assert f"Start {HIRELING}'s turn" not in page

    _set_flag(app, campaign_id, True)

    page = client.get(f"/campaigns/{campaign_id}").text
    assert f"Start {HIRELING}'s turn" in page
    assert f"End {HIRELING}&#39;s turn" not in page

    lines = _npc_turn_stream(client, campaign_id, HIRELING)

    assert lines[-1]["type"] == "saved"
    state = _state(app, campaign_id)
    assert state.turn is None
    assert state.encounter is not None and state.encounter.acting is None
    assert state.encounter.order[state.encounter.up] == NPC
    events = _turn_events(app, campaign_id, HIRELING)
    assert [event.gm_played for event in events] == [False, True]
    assert events[0].render() == f"{HIRELING}'s turn begins."
    assert events[1].render() is None
    with Session(app.state.engine) as db:
        owner_id = db.get(Campaign, campaign_id).owner_id
    log = [
        item.content
        for item in campaign_transcript(app.state.engine, owner_id, campaign_id)
        if item.role == "log"
    ]
    assert log.count(f"{HIRELING}'s turn begins.") == 1


def test_turning_the_setting_off_returns_a_waiting_hireling_turn_to_the_player(
    client_factory: Callable[[], TestClient],
):
    client, campaign_id = _fight_with_hireling(client_factory, gm_plays=True)
    app = client.app

    # Mara, then the NPC whose turn the GM starts and ends, then round two begins with Mara.
    _begin(app, campaign_id, PC)
    _end(app, campaign_id, PC, by_player=True)
    assert _npc_turn_stream(client, campaign_id, HIRELING)[-1]["type"] == "saved"
    _begin(app, campaign_id, NPC)
    _close_reply(app, campaign_id)
    _end(app, campaign_id, NPC, by_player=False)
    # Round two begins with the party, so Mara's turn opens on its own.
    assert _state(app, campaign_id).encounter.acting == PC
    _end(app, campaign_id, PC, by_player=True)

    state = _state(app, campaign_id)
    assert state.encounter is not None
    assert state.encounter.acting is None
    assert state.encounter.order[state.encounter.up] == HIRELING
    page = client.get(f"/campaigns/{campaign_id}").text
    assert f"Start {HIRELING}'s turn" in page

    _set_flag(app, campaign_id, False)

    page = client.get(f"/campaigns/{campaign_id}").text
    assert f"End {HIRELING}&#39;s turn" in page
    assert f"Start {HIRELING}'s turn" not in page
    state = _state(app, campaign_id)
    assert state.encounter is not None
    assert state.encounter.acting == HIRELING
    assert state.encounter.gm_played is False
