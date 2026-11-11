from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusloom.db.models import Campaign, Event
from cygnusloom.llm.fake import FakeProvider
from cygnusloom.services.campaigns import _exclusive, campaign_log, list_messages
from cygnusloom.services.events import ChatGM, record
from tests.conftest import csrf_token, is_journal_messages, is_review_messages
from tests.factories import db
from tests.test_campaigns import _campaign_id, _send, _signed_in
from tests.test_multiplayer import _joined_campaign


def _edit(
    client: TestClient,
    campaign_id: int,
    event_id: int,
    content: str,
    token: str | None = None,
    page: str | None = None,
):
    token = token or csrf_token(client, page or f"/campaigns/{campaign_id}")
    return client.post(
        f"/campaigns/{campaign_id}/messages/{event_id}/edit",
        data={"csrf_token": token, "content": content},
        follow_redirects=False,
    )


def _reply_ids(app: FastAPI, campaign_id: int) -> list[int]:
    return [m.id for m in list_messages(app.state.engine, campaign_id) if m.role == "assistant"]


def _log(app: FastAPI, campaign_id: int) -> list[str]:
    with Session(app.state.engine) as db_session:
        owner_id = db_session.get(Campaign, campaign_id).owner_id
    return campaign_log(app.state.engine, owner_id, campaign_id)


def _message_ids(app: FastAPI, campaign_id: int, role: str) -> list[int]:
    return [m.id for m in list_messages(app.state.engine, campaign_id) if m.role == role]


def test_editing_the_newest_reply_replaces_it_in_the_transcript(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "Hello.")
    reply_id = _reply_ids(app, campaign_id)[-1]
    with Session(app.state.engine) as db_session:
        original = db_session.get(Event, reply_id)
        assert original is not None
        original_payload = original.payload

    response = _edit(client, campaign_id, reply_id, "A quieter arrival.")

    assert response.status_code == 303
    assert response.headers["location"] == f"/campaigns/{campaign_id}"
    messages = list_messages(app.state.engine, campaign_id)
    assert messages[-1].id == reply_id
    assert messages[-1].content == "A quieter arrival."
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "A quieter arrival." in page
    # The correction is a later event; the reply it corrects keeps what the model wrote.
    with Session(app.state.engine) as db_session:
        assert db_session.get(Event, reply_id).payload == original_payload
        kinds = [
            row.kind
            for row in db_session.exec(
                select(Event).where(Event.campaign_id == campaign_id).order_by(Event.seq)
            ).all()
        ]
    assert kinds[-1] == "chat.gm.edited"


def test_the_newest_correction_wins(client_factory: Callable[[], TestClient], app: FastAPI):
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "Hello.")
    reply_id = _reply_ids(app, campaign_id)[-1]

    assert _edit(client, campaign_id, reply_id, "First telling.").status_code == 303
    assert _edit(client, campaign_id, reply_id, "Second telling.").status_code == 303

    assert list_messages(app.state.engine, campaign_id)[-1].content == "Second telling."


def test_a_correction_is_not_a_chat_line_or_a_log_entry(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "Hello.")
    reply_id = _reply_ids(app, campaign_id)[-1]
    messages_before = list_messages(app.state.engine, campaign_id)
    log_before = _log(app, campaign_id)

    assert _edit(client, campaign_id, reply_id, "A cleaner telling.").status_code == 303

    assert len(list_messages(app.state.engine, campaign_id)) == len(messages_before)
    assert _log(app, campaign_id) == log_before
    page = client.get(f"/campaigns/{campaign_id}").text
    assert page.count('class="message ') == len(messages_before)


def test_the_edit_reaches_the_next_turn(
    client_factory: Callable[[], TestClient], app: FastAPI, fake: FakeProvider
):
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "First.")
    reply_id = _reply_ids(app, campaign_id)[-1]
    assert _edit(client, campaign_id, reply_id, "The corrected telling.").status_code == 303

    _send(client, campaign_id, "Second.")

    last = [c for c in fake.calls if not is_journal_messages(c) and not is_review_messages(c)][-1]
    replies = [m for m in last if m["role"] == "assistant"]
    assert replies[-1]["content"] == "The corrected telling."
    assert replies[-1]["reasoning_content"] == ""


def test_the_edit_drops_the_old_reasoning(client_factory: Callable[[], TestClient], app: FastAPI):
    client, campaign_id = _campaign_id(client_factory)
    with db(app) as session:
        reply_id = record(
            session,
            campaign_id,
            ChatGM(
                content="The original telling.",
                model="test",
                reasoning="I chose the road by the river.",
            ),
        )
        session.commit()

    assert _edit(client, campaign_id, reply_id, "The corrected telling.").status_code == 303

    message = list_messages(app.state.engine, campaign_id)[-1]
    assert message.content == "The corrected telling."
    assert message.reasoning == ""


def test_only_the_newest_reply_can_be_edited(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "One.")
    older = _reply_ids(app, campaign_id)[-1]
    _send(client, campaign_id, "Two.")
    newest = _reply_ids(app, campaign_id)[-1]
    before = list_messages(app.state.engine, campaign_id)

    response = _edit(client, campaign_id, older, "An old correction.")

    assert response.status_code == 400
    assert "only the most recent GM reply can be edited" in response.text
    assert list_messages(app.state.engine, campaign_id) == before
    assert _edit(client, campaign_id, newest, "The new correction.").status_code == 303


def test_only_a_gm_reply_can_be_edited(client_factory: Callable[[], TestClient], app: FastAPI):
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "Hello.")
    user_id = _message_ids(app, campaign_id, "user")[0]

    assert _edit(client, campaign_id, user_id, "Not mine to edit.").status_code == 404
    assert _edit(client, campaign_id, 999999, "No such reply.").status_code == 404


def test_editing_needs_the_csrf_token(client_factory: Callable[[], TestClient], app: FastAPI):
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "Hello.")
    reply_id = _reply_ids(app, campaign_id)[-1]
    before = list_messages(app.state.engine, campaign_id)

    response = _edit(client, campaign_id, reply_id, "Forged.", token="forged")

    assert response.status_code == 403
    assert list_messages(app.state.engine, campaign_id) == before


def test_an_empty_correction_is_refused(client_factory: Callable[[], TestClient], app: FastAPI):
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "Hello.")
    reply_id = _reply_ids(app, campaign_id)[-1]
    before = list_messages(app.state.engine, campaign_id)

    response = _edit(client, campaign_id, reply_id, "   ")

    assert response.status_code == 400
    assert "cannot be empty" in response.text
    assert list_messages(app.state.engine, campaign_id) == before


def test_editing_while_the_gm_is_working_is_refused(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "Hello.")
    reply_id = _reply_ids(app, campaign_id)[-1]

    with _exclusive(campaign_id):
        response = _edit(client, campaign_id, reply_id, "Too soon.")

    assert response.status_code == 409
    assert list_messages(app.state.engine, campaign_id)[-1].id == reply_id


def test_only_the_newest_reply_offers_an_edit_control(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "One.")
    _send(client, campaign_id, "Two.")
    replies = _reply_ids(app, campaign_id)

    page = client.get(f"/campaigns/{campaign_id}").text

    assert page.count("data-message-edit-url") == 1
    assert f"/campaigns/{campaign_id}/messages/{replies[-1]}/edit" in page
    older = f'data-message-edit-url="/campaigns/{campaign_id}/messages/{replies[0]}/edit"'
    assert older not in page
    assert 'id="dialog-message-edit"' in page


def test_a_joined_player_can_edit_the_reply(
    client_factory: Callable[[], TestClient],
):
    owner, guest, campaign_id, _ = _joined_campaign(client_factory)
    _send(owner, campaign_id, "Hello.")
    reply_id = _reply_ids(guest.app, campaign_id)[-1]

    response = _edit(guest, campaign_id, reply_id, "A guest's telling.")

    assert response.status_code == 303
    assert list_messages(guest.app.state.engine, campaign_id)[-1].content == "A guest's telling."


def test_another_account_cannot_edit_the_reply(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    owner, campaign_id = _campaign_id(client_factory)
    _send(owner, campaign_id, "Hello.")
    reply_id = _reply_ids(app, campaign_id)[-1]
    _send(owner, campaign_id, "Again.")
    intruder = _signed_in(client_factory, "intruder@example.com")

    response = _edit(intruder, campaign_id, reply_id, "Not my story.", page="/campaigns")

    assert response.status_code == 404
    assert list_messages(app.state.engine, campaign_id)[-1].content != "Not my story."
