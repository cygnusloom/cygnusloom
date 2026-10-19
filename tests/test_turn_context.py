from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusforge.config import Settings
from cygnusforge.db.models import Message
from cygnusforge.services.campaigns import get_turn_context
from tests.test_campaigns import _campaign_id, _send, _signed_in
from tests.test_context import RecordingProvider, _send_until_summarized, _small_window_app, _stored
from tests.test_tools import ROLL, _recording_provider, _script

SOURCE_ID_RE = re.compile(r"^Message #(\d+) ")


def _message_ids(app: FastAPI, campaign_id: int, role: str) -> list[int]:
    with Session(app.state.engine) as db:
        rows = db.exec(
            select(Message)
            .where(Message.campaign_id == campaign_id, Message.role == role)
            .order_by(Message.id)
        ).all()
        return [m.id for m in rows if m.id is not None]


def _context(app: FastAPI, campaign_id: int, message_id: int) -> list[Any]:
    owner_id = _stored(app, campaign_id).owner_id
    parts = get_turn_context(app.state.engine, owner_id, campaign_id, message_id)
    assert parts is not None
    return parts


def test_reply_context_lists_the_system_prompt_tools_and_the_message(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "hello")
    reply_id = _message_ids(app, campaign_id, "assistant")[0]

    parts = _context(app, campaign_id, reply_id)

    sources = [part.source for part in parts]
    assert sources[0] == "Base prompt"
    assert any(source.startswith("Rules from rule set 'freeform'") for source in sources)
    assert "Tool 'social_roll' from rule set 'freeform'" in sources
    assert sources[-1] == "This message (player)"
    assert parts[-1].content == "hello"
    page = client.get(f"/campaigns/{campaign_id}/messages/{reply_id}/context")
    assert page.status_code == 200
    assert "Base prompt" in page.text and "This message (player)" in page.text
    assert "social_roll" in page.text


def test_recorded_system_text_is_exactly_what_the_model_received(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, _script({"text": "Hello back."}))
    script = _script({"text": "Checking.", "tool_calls": [ROLL]}, {"text": "Well rolled."})
    _send(client, campaign_id, script)
    reply_id = _message_ids(app, campaign_id, "assistant")[-1]

    parts = _context(app, campaign_id, reply_id)

    final_call = recorder.calls[-1]["messages"]
    system_text = "\n\n".join(part.content for part in parts if part.role == "system")
    assert final_call[0] == {"role": "system", "content": system_text}


def test_context_shows_history_and_this_turns_tool_rounds(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, _script({"text": "Hello back."}))
    _send(client, campaign_id, "second")
    first_user, second_user = _message_ids(app, campaign_id, "user")
    first_reply = _message_ids(app, campaign_id, "assistant")[0]
    script = _script({"text": "Checking.", "tool_calls": [ROLL]}, {"text": "Well rolled."})
    _send(client, campaign_id, script)
    reply_id = _message_ids(app, campaign_id, "assistant")[-1]

    parts = _context(app, campaign_id, reply_id)

    sources = [part.source for part in parts]
    assert f"Message #{first_user} (player)" in sources
    assert f"Message #{first_reply} (GM, fake/scripted)" in sources
    assert f"Message #{second_user} (player)" in sources
    assert sources[-4:] == [
        "This message (player)",
        "GM text (round 1)",
        "GM tool call (round 1)",
        "Result of 'social_roll' (round 1)",
    ]
    assert parts[-2].content.startswith("social_roll(")
    result = json.loads(parts[-1].content)
    assert result["total"] == result["die"] + result["relationship"] + result["circumstance"]


def test_summary_part_appears_and_folded_messages_do_not(
    settings: Settings,
):
    provider = RecordingProvider()
    app = _small_window_app(settings, provider)
    client, campaign_id = _campaign_id(lambda: TestClient(app))
    _send_until_summarized(client, app, campaign_id, sends=10)
    assert _stored(app, campaign_id).context_summary
    _send(client, campaign_id, "final")
    reply_id = _message_ids(app, campaign_id, "assistant")[-1]
    first_user = _message_ids(app, campaign_id, "user")[0]

    parts = _context(app, campaign_id, reply_id)

    summaries = [p for p in parts if p.source.startswith("Summary of messages up to #")]
    assert len(summaries) == 1
    assert "SUMMARY" in summaries[0].content
    watermark = int(summaries[0].source.rsplit("#", 1)[1])
    assert first_user <= watermark
    kept = [int(m.group(1)) for p in parts if (m := SOURCE_ID_RE.match(p.source))]
    assert kept and all(message_id > watermark for message_id in kept)
    assert first_user not in kept


def test_legacy_reply_without_a_recorded_context_says_so(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    with Session(app.state.engine) as db:
        legacy = Message(campaign_id=campaign_id, role="assistant", content="old", model="m")
        db.add(legacy)
        db.commit()
        db.refresh(legacy)
        legacy_id = legacy.id

    response = client.get(f"/campaigns/{campaign_id}/messages/{legacy_id}/context")

    assert response.status_code == 200
    assert "No context was recorded" in response.text


def test_context_is_only_for_the_owner_and_only_for_replies(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    owner, campaign_id = _campaign_id(client_factory)
    _send(owner, campaign_id, "hi")
    reply_id = _message_ids(app, campaign_id, "assistant")[0]
    user_id = _message_ids(app, campaign_id, "user")[0]
    intruder = _signed_in(client_factory, "intruder@example.com")

    assert intruder.get(f"/campaigns/{campaign_id}/messages/{reply_id}/context").status_code == 404
    assert owner.get(f"/campaigns/{campaign_id}/messages/{user_id}/context").status_code == 404
    assert owner.get(f"/campaigns/{campaign_id}/messages/999999/context").status_code == 404
    assert owner.get(f"/campaigns/{campaign_id}/messages/{reply_id}/context").status_code == 200


def test_context_fragment_escapes_html(client_factory: Callable[[], TestClient], app: FastAPI):
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "<img src=x onerror=alert(1)>")
    reply_id = _message_ids(app, campaign_id, "assistant")[0]

    text = client.get(f"/campaigns/{campaign_id}/messages/{reply_id}/context").text

    assert "<img src=x" not in text
    assert "&lt;img src=x onerror=alert(1)&gt;" in text


def test_each_reply_has_a_gear_but_user_messages_do_not(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, "hello")
    reply_id = _message_ids(app, campaign_id, "assistant")[0]
    user_id = _message_ids(app, campaign_id, "user")[0]

    page = client.get(f"/campaigns/{campaign_id}").text

    assert f'data-turn-context-url="/campaigns/{campaign_id}/messages/{reply_id}/context"' in page
    assert f"/messages/{user_id}/context" not in page
    assert 'id="dialog-turn-context"' in page
