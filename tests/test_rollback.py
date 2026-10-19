from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusforge.db.models import Campaign
from cygnusforge.services.campaigns import campaign_log, list_messages
from cygnusforge.services.events import LocationAdded, load_state, record
from tests.conftest import csrf_token
from tests.test_campaigns import _campaign_id, _pending, _send, _signed_in
from tests.test_tools import ROLL, _recording_provider, _script


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
    first_reply = _ids(app, campaign_id, "assistant")[0]
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
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[0].content == "First question."
    assert messages[-1].id == first_reply
    assert not _location_exists(app, campaign_id, "Test Ledge")
    assert not any("Test Ledge" in line for line in _log(app, campaign_id))
    assert client.get(f"/campaigns/{campaign_id}/messages/{later_reply}/context").status_code == 404


def test_rollback_discards_a_paused_turn(client_factory: Callable[[], TestClient], app: FastAPI):
    app.state.providers = lambda model_id: (_recording_provider(), model_id)
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "First question.")
    first_reply = _ids(app, campaign_id, "assistant")[0]
    _send(client, campaign_id, _script({"rounds": [[ROLL]], "text": "Well rolled."}))
    assert _pending(app, campaign_id) is not None

    _rollback(client, campaign_id, first_reply)

    assert _pending(app, campaign_id) is None
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "Roll to persuade." not in page
    assert len(list_messages(app.state.engine, campaign_id)) == 2


def test_rollback_is_refused_for_other_accounts(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    owner, campaign_id = _campaign_id(client_factory)
    _send(owner, campaign_id, "Hello.")
    reply = _ids(app, campaign_id, "assistant")[0]
    intruder = _signed_in(client_factory, "intruder@example.com")

    response = _rollback(intruder, campaign_id, reply, page="/campaigns")

    assert response.status_code == 404
    assert len(list_messages(app.state.engine, campaign_id)) == 2


def test_only_gm_replies_can_be_rolled_back(client_factory: Callable[[], TestClient], app: FastAPI):
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "Hello.")
    user_line = _ids(app, campaign_id, "user")[0]

    assert _rollback(client, campaign_id, user_line).status_code == 404
    assert _rollback(client, campaign_id, 999999).status_code == 404
    assert len(list_messages(app.state.engine, campaign_id)) == 2


def test_rollback_needs_the_csrf_token(client_factory: Callable[[], TestClient], app: FastAPI):
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "Hello.")
    reply = _ids(app, campaign_id, "assistant")[0]
    _send(client, campaign_id, "Again.")

    response = _rollback(client, campaign_id, reply, token="forged")

    assert response.status_code == 403
    assert len(list_messages(app.state.engine, campaign_id)) == 4


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
    assert list_messages(app.state.engine, campaign_id) == []
    assert (
        "Say something to start the conversation." in client.get(f"/campaigns/{campaign_id}").text
    )


def test_discard_keeps_the_earlier_exchange(client_factory: Callable[[], TestClient], app: FastAPI):
    app.state.providers = lambda model_id: (_recording_provider(), model_id)
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "First question.")
    _send(client, campaign_id, _script({"error": "upstream exploded"}))
    before = [m.role for m in list_messages(app.state.engine, campaign_id)]

    token = csrf_token(client, f"/campaigns/{campaign_id}")
    client.post(f"/campaigns/{campaign_id}/pending/discard", data={"csrf_token": token})

    assert _pending(app, campaign_id) is None
    assert before == ["user", "assistant", "user"]
    assert [m.role for m in list_messages(app.state.engine, campaign_id)] == ["user", "assistant"]
