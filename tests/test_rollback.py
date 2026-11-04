from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusloom.db.models import Campaign, Event
from cygnusloom.services.campaigns import campaign_log, list_messages
from cygnusloom.services.events import (
    ChatPlayer,
    EncounterEnded,
    EncounterStarted,
    EncounterTurnBegun,
    EncounterTurnEnded,
    JournalStarted,
    LocationAdded,
    NarrationUndone,
    NoteAdded,
    StatChanged,
    SummaryUpdated,
    TurnFailed,
    last_event_id,
    load_state,
    record,
)
from tests.conftest import csrf_token
from tests.factories import answer, send_message
from tests.test_campaigns import _campaign_id, _pending, _send, _signed_in
from tests.test_encounters import _encounter_with, _npc_turn_stream
from tests.test_tools import ROLL, _recording_provider, _script

PLAYER_ATTACK = {
    "name": "player_attack",
    "arguments": {"target": "Pip", "weapon": "unarmed", "prompt": "Roll your attack."},
}


def _event_kinds(app: FastAPI, campaign_id: int, after: int = 0) -> list[str]:
    with Session(app.state.engine) as db:
        rows = db.exec(
            select(Event)
            .where(Event.campaign_id == campaign_id, Event.id > after)
            .order_by(Event.seq)
        ).all()
    return [row.kind for row in rows]


def _ids(app: FastAPI, campaign_id: int, role: str) -> list[int]:
    return [m.id for m in list_messages(app.state.engine, campaign_id) if m.role == role]


def _rollback(
    client: TestClient,
    campaign_id: int,
    event_id: int,
    token: str | None = None,
    page: str | None = None,
):
    token = token or csrf_token(client, page or f"/campaigns/{campaign_id}")
    return client.post(
        f"/campaigns/{campaign_id}/messages/{event_id}/rollback",
        data={"csrf_token": token},
        follow_redirects=False,
    )


def _location_exists(app: FastAPI, campaign_id: int, name: str) -> bool:
    with Session(app.state.engine) as db:
        return load_state(db, campaign_id).named(name) is not None


def test_rollback_restores_the_chat_and_the_world_to_just_after_the_reply(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "First question.")
    first_reply = _ids(app, campaign_id, "assistant")[-1]
    with Session(app.state.engine) as db:
        record(db, campaign_id, LocationAdded(name="Test Ledge", description="A ledge."))
        db.commit()
    _send(client, campaign_id, "Second question.")
    later_reply = _ids(app, campaign_id, "assistant")[-1]
    assert _location_exists(app, campaign_id, "Test Ledge")

    response = _rollback(client, campaign_id, first_reply)

    assert response.status_code == 303
    assert response.headers["location"] == f"/campaigns/{campaign_id}"
    messages = list_messages(app.state.engine, campaign_id)
    assert [m.role for m in messages] == ["assistant", "user", "assistant"]
    assert messages[1].content == "First question."
    assert messages[-1].id == first_reply
    assert not _location_exists(app, campaign_id, "Test Ledge")
    assert not any("Test Ledge" in line for line in _log(app, campaign_id))
    assert client.get(f"/campaigns/{campaign_id}/messages/{later_reply}/context").status_code == 404


def test_rollback_discards_a_paused_turn(client_factory: Callable[[], TestClient], app: FastAPI):
    app.state.providers = lambda model_id: (_recording_provider(), model_id)
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "First question.")
    first_reply = _ids(app, campaign_id, "assistant")[-1]
    _send(client, campaign_id, _script({"rounds": [[ROLL]], "text": "Well rolled."}))
    assert _pending(app, campaign_id) is not None

    _rollback(client, campaign_id, first_reply)

    assert _pending(app, campaign_id) is None
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "Roll to persuade." not in page
    assert len(list_messages(app.state.engine, campaign_id)) == 3


def test_rollback_of_an_npc_reply_ends_the_turn_before_the_reply(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _encounter_with(client_factory, columns=10, rows=10, cairn=True)
    script = _script({"text": "Pip waits."}, {"text": "Pip ducks behind the crate."})
    assert send_message(client, campaign_id, script).status_code == 303
    lines = _npc_turn_stream(client, campaign_id, "Pip")
    assert lines[-1]["type"] == "saved"
    reply_id = _ids(client.app, campaign_id, "assistant")[-1]
    with Session(client.app.state.engine) as db:
        ended_id = db.exec(
            select(Event.id).where(
                Event.campaign_id == campaign_id, Event.kind == "encounter.turn_ended"
            )
        ).one()
    assert ended_id < reply_id

    assert _rollback(client, campaign_id, reply_id).status_code == 303

    with Session(client.app.state.engine) as db:
        state = load_state(db, campaign_id)
    assert state.turn is None
    assert state.encounter is not None
    assert state.encounter.acting is None
    # The rollback kept the turn end, so the next actor can start.
    token = csrf_token(client, "/campaigns")
    response = client.post(
        f"/campaigns/{campaign_id}/encounter/turn/begin",
        data={"csrf_token": token, "name": "Ada"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_a_prompt_paused_npc_turn_ends_when_it_is_resumed(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _encounter_with(client_factory, columns=10, rows=10, cairn=True)
    script = _script(
        {"text": "Pip waits."},
        {"rounds": [[PLAYER_ATTACK]], "text": "Pip shrugs and does nothing."},
    )
    assert send_message(client, campaign_id, script).status_code == 303
    lines = _npc_turn_stream(client, campaign_id, "Pip")
    assert lines[-1]["type"] == "prompt"
    assert _pending(client.app, campaign_id) is not None

    events = answer(client, campaign_id, "text", "I brace myself.")

    assert events[-1]["type"] == "saved"
    with Session(client.app.state.engine) as db:
        state = load_state(db, campaign_id)
        ended = list(
            db.exec(
                select(Event).where(
                    Event.campaign_id == campaign_id,
                    Event.kind == "encounter.turn_ended",
                )
            ).all()
        )
    assert state.turn is None
    assert state.encounter is not None
    assert state.encounter.acting is None
    assert len(ended) == 1


def test_rollback_reverts_every_record_a_turn_leaves(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "First question.")
    reply_id = _ids(app, campaign_id, "assistant")[-1]
    with Session(app.state.engine) as db:
        snapshot = load_state(db, campaign_id)

    # A turn opens and records every kind of state the player can undo: a journal, a note, a
    # stat change, an encounter begin and end, a summary, a review undo, and a failure.
    with Session(app.state.engine) as db:
        opener = record(db, campaign_id, ChatPlayer(content="I look around.", speaker="Ada"))
        record(db, campaign_id, JournalStarted())
        record(db, campaign_id, NoteAdded(text="The gate is barred.", subject="Mossbell Hollow"))
        record(
            db,
            campaign_id,
            StatChanged(name="Ada", stat="hp", before=10, after=7, cause="a fall"),
        )
        record(db, campaign_id, EncounterStarted(map="Green", location="Mossbell Hollow"))
        record(db, campaign_id, EncounterTurnBegun(name="Ada"))
        record(db, campaign_id, EncounterTurnEnded(name="Ada"))
        record(db, campaign_id, EncounterEnded(reason="test"))
        record(db, campaign_id, SummaryUpdated(summary="So far, a fall.", through=opener))
        record(db, campaign_id, NarrationUndone(reason="the narration was wrong"))
        record(db, campaign_id, TurnFailed(error="the turn stopped"))
        db.commit()
    assert {"narration.undone", "journal.started", "summary.updated", "turn.failed"} <= set(
        _event_kinds(app, campaign_id, after=reply_id)
    )

    assert _rollback(client, campaign_id, reply_id).status_code == 303

    with Session(app.state.engine) as db:
        state = load_state(db, campaign_id)
        assert last_event_id(db, campaign_id) == reply_id
    assert state == snapshot
    assert _event_kinds(app, campaign_id, after=reply_id) == []


def test_rollback_is_refused_for_other_accounts(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    owner, campaign_id = _campaign_id(client_factory)
    _send(owner, campaign_id, "Hello.")
    reply = _ids(app, campaign_id, "assistant")[-1]
    intruder = _signed_in(client_factory, "intruder@example.com")

    response = _rollback(intruder, campaign_id, reply, page="/campaigns")

    assert response.status_code == 404
    assert len(list_messages(app.state.engine, campaign_id)) == 3


def test_only_gm_replies_can_be_rolled_back(client_factory: Callable[[], TestClient], app: FastAPI):
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "Hello.")
    user_line = _ids(app, campaign_id, "user")[0]

    assert _rollback(client, campaign_id, user_line).status_code == 404
    assert _rollback(client, campaign_id, 999999).status_code == 404
    assert len(list_messages(app.state.engine, campaign_id)) == 3


def test_rollback_needs_the_csrf_token(client_factory: Callable[[], TestClient], app: FastAPI):
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "Hello.")
    reply = _ids(app, campaign_id, "assistant")[-1]
    _send(client, campaign_id, "Again.")

    response = _rollback(client, campaign_id, reply, token="forged")

    assert response.status_code == 403
    assert len(list_messages(app.state.engine, campaign_id)) == 5


def test_each_gm_reply_has_a_rollback_button_and_the_player_lines_do_not(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "One.")
    _send(client, campaign_id, "Two.")
    replies = _ids(app, campaign_id, "assistant")
    users = _ids(app, campaign_id, "user")

    page = client.get(f"/campaigns/{campaign_id}").text

    for reply in replies:
        assert f"/campaigns/{campaign_id}/messages/{reply}/rollback" in page
    for user in users:
        assert f"/campaigns/{campaign_id}/messages/{user}/rollback" not in page


def _log(app: FastAPI, campaign_id: int) -> list[str]:
    with Session(app.state.engine) as db:
        owner_id = db.get(Campaign, campaign_id).owner_id
    return campaign_log(app.state.engine, owner_id, campaign_id)


def test_discard_removes_a_failed_first_reply_and_its_message(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    app.state.providers = lambda model_id: (_recording_provider(), model_id)
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, _script({"error": "upstream exploded"}))
    assert _pending(app, campaign_id) is not None

    token = csrf_token(client, f"/campaigns/{campaign_id}")
    response = client.post(
        f"/campaigns/{campaign_id}/pending/discard",
        data={"csrf_token": token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert _pending(app, campaign_id) is None
    assert [m.role for m in list_messages(app.state.engine, campaign_id)] == ["assistant"]


def test_discard_keeps_the_earlier_exchange(client_factory: Callable[[], TestClient], app: FastAPI):
    app.state.providers = lambda model_id: (_recording_provider(), model_id)
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "First question.")
    _send(client, campaign_id, _script({"error": "upstream exploded"}))
    before = [m.role for m in list_messages(app.state.engine, campaign_id)]

    token = csrf_token(client, f"/campaigns/{campaign_id}")
    client.post(f"/campaigns/{campaign_id}/pending/discard", data={"csrf_token": token})

    assert _pending(app, campaign_id) is None
    assert before == ["assistant", "user", "assistant", "user"]
    assert [m.role for m in list_messages(app.state.engine, campaign_id)] == [
        "assistant",
        "user",
        "assistant",
    ]
