"""The debug panel can force a wrong action or narration into the next GM turn.

The injection is one-shot: the review sees the wrong step as the GM's own, and a debug campaign
can check that the review catches it without changing normal play.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.config import Settings
from cygnusloom.llm.fake import FakeProvider
from cygnusloom.services.events import CampaignState, load_state
from tests.conftest import csrf_token, is_review_messages
from tests.test_campaigns import _campaign_id, _debug_client_factory
from tests.test_tools import _saved, _script

UNDO = {
    "name": "undo_narration",
    "arguments": {"reason": "The injected narration is wrong."},
}
WRONG_ROOM = {
    "name": "create_location",
    "arguments": {"name": "Wrong Room", "description": "A room that should not exist."},
}
DELETE_WRONG_ROOM = {
    "name": "delete_location",
    "arguments": {"name": "Wrong Room"},
}


def _inject(
    client: TestClient,
    campaign_id: int,
    mode: str,
    **fields: str,
):
    token = csrf_token(client, f"/campaigns/{campaign_id}")
    return client.post(
        f"/campaigns/{campaign_id}/debug/injection",
        data={"csrf_token": token, "mode": mode, **fields},
        follow_redirects=False,
    )


def _clear(client: TestClient, campaign_id: int):
    token = csrf_token(client, f"/campaigns/{campaign_id}")
    return client.post(
        f"/campaigns/{campaign_id}/debug/injection/clear",
        data={"csrf_token": token},
        follow_redirects=False,
    )


def _state(app: FastAPI, campaign_id: int) -> CampaignState:
    with Session(app.state.engine) as db:
        return load_state(db, campaign_id)


def _reviews(provider: FakeProvider) -> list[list[dict]]:
    return [messages for messages in provider.calls if is_review_messages(messages)]


def _reviewed_narration(messages: list[dict]) -> str:
    """The narration the review was given, which is the assistant message just before its
    instruction."""
    start = next(
        i
        for i, message in enumerate(messages)
        if message["role"] == "system" and "Turn review step." in (message.get("content") or "")
    )
    return next(
        message["content"]
        for message in reversed(messages[:start])
        if message["role"] == "assistant" and "tool_calls" not in message
    )


def test_debug_injection_forms_are_hidden_by_default(client_factory: Callable[[], TestClient]):
    client, campaign_id = _campaign_id(client_factory)

    page = client.get(f"/campaigns/{campaign_id}").text
    assert "Inject tool call" not in page
    assert _inject(client, campaign_id, "narration", text="Wrong.").status_code == 404


def test_the_debug_panel_sets_and_clears_an_injection(settings: Settings, fake: FakeProvider):
    client, campaign_id = _campaign_id(_debug_client_factory(settings, fake))

    assert (
        _inject(
            client, campaign_id, "narration", text="Ada still has a point to spend."
        ).status_code
        == 303
    )
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "Pending injection" in page
    assert "Ada still has a point to spend." in page

    assert _clear(client, campaign_id).status_code == 303
    assert "Pending injection" not in client.get(f"/campaigns/{campaign_id}").text


def test_injected_narration_is_what_the_review_sees(settings: Settings, fake: FakeProvider):
    client, campaign_id = _campaign_id(_debug_client_factory(settings, fake))
    injected = "Ada still has one advancement point to spend."
    script = _script(
        {
            "rounds": [[]],
            "text": "The narrator's own text.",
            "rewrite": "Ada has no advancement point left.",
            "review": [[[UNDO]]],
        }
    )

    assert _inject(client, campaign_id, "narration", text=injected).status_code == 303
    assert (
        client.post(
            f"/campaigns/{campaign_id}/messages",
            data={"csrf_token": csrf_token(client, "/campaigns"), "content": script},
            follow_redirects=False,
        ).status_code
        == 303
    )

    reviews = [review for review in _reviews(fake) if injected in json.dumps(review)]
    assert reviews
    assert _reviewed_narration(reviews[0]) == injected
    assert _saved(app=client.app, campaign_id=campaign_id)[-1] == (
        "assistant",
        "Ada has no advancement point left.",
    )
    assert "Pending injection" not in client.get(f"/campaigns/{campaign_id}").text


def test_injected_tool_call_reaches_the_review_and_the_record(
    settings: Settings, fake: FakeProvider
):
    client, campaign_id = _campaign_id(_debug_client_factory(settings, fake))
    script = _script(
        {
            "rounds": [[]],
            "text": "Ada waits.",
            "rewrite": "Ada waits in the hall.",
            "review": [[[UNDO, DELETE_WRONG_ROOM]]],
        }
    )

    assert (
        _inject(
            client,
            campaign_id,
            "tool",
            tool=WRONG_ROOM["name"],
            arguments=json.dumps(WRONG_ROOM["arguments"]),
        ).status_code
        == 303
    )
    assert (
        client.post(
            f"/campaigns/{campaign_id}/messages",
            data={"csrf_token": csrf_token(client, "/campaigns"), "content": script},
            follow_redirects=False,
        ).status_code
        == 303
    )

    assert "wrong room" not in _state(client.app, campaign_id).places
    reviews = [review for review in _reviews(fake) if "Wrong Room" in json.dumps(review)]
    assert reviews
    assert _saved(app=client.app, campaign_id=campaign_id)[-1] == (
        "assistant",
        "Ada waits in the hall.",
    )
    assert "Pending injection" not in client.get(f"/campaigns/{campaign_id}").text


def test_bad_injections_are_refused(settings: Settings, fake: FakeProvider):
    client, campaign_id = _campaign_id(_debug_client_factory(settings, fake))

    assert (
        _inject(client, campaign_id, "tool", tool="social_roll", arguments="{}").status_code == 400
    )
    assert (
        _inject(client, campaign_id, "tool", tool="create_location", arguments="{bad").status_code
        == 400
    )
    assert (
        _inject(client, campaign_id, "tool", tool="create_location", arguments="{}").status_code
        == 400
    )
    assert _inject(client, campaign_id, "narration", text="   ").status_code == 400
    assert _inject(client, campaign_id, "dance").status_code == 400
