"""The turn review checks the actions and narration before the player keeps them.

A sound narration costs one model call and changes nothing. A review that finds a fault undoes
the narration and may fix the record; the narrator then writes the beat again from the corrected
state and the review's reason.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusforge.db.models import Event
from cygnusforge.services.events import CampaignState, load_state
from tests.conftest import csrf_token, is_review_messages
from tests.test_campaigns import _campaign_id, _send
from tests.test_tools import _recording_provider, _saved, _script

UNDO = {
    "name": "undo_narration",
    "arguments": {"reason": "Ada has no advancement point left; the tool spent it."},
}
CREATE_GATE = {
    "name": "create_location",
    "arguments": {"name": "Gate Room", "description": "A room behind the gate."},
}


def _state(app: FastAPI, campaign_id: int) -> CampaignState:
    with Session(app.state.engine) as db:
        return load_state(db, campaign_id)


def _narration_calls(recorder: Any, script: str) -> list[dict[str, Any]]:
    """The narration calls for the test's own message, not an opening the recorder also saw."""
    return [
        call
        for call in recorder.calls
        if call["tools"] is None and any(m.get("content") == script for m in call["messages"])
    ]


def _review_calls(recorder: Any) -> list[dict[str, Any]]:
    return [call for call in recorder.calls if is_review_messages(call["messages"])]


def _stream(client: TestClient, campaign_id: int, text: str) -> list[dict[str, Any]]:
    token = csrf_token(client, "/campaigns")
    response = client.post(
        f"/campaigns/{campaign_id}/messages/stream",
        data={"csrf_token": token, "content": text},
    )
    assert response.status_code == 200
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def _deltas(events: list[dict[str, Any]]) -> str:
    return "".join(e["text"] for e in events if e["type"] == "delta")


def _undone_events(app: FastAPI, campaign_id: int) -> list[Event]:
    with Session(app.state.engine) as db:
        return list(
            db.exec(
                select(Event).where(
                    Event.campaign_id == campaign_id, Event.kind == "narration.undone"
                )
            ).all()
        )


def test_a_sound_narration_passes_review_and_is_saved_once(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)

    script = _script({"rounds": [[]], "text": "Ada waits."})
    assert _send(client, campaign_id, script).status_code == 303

    narration_calls = _narration_calls(recorder, script)
    assert len(narration_calls) == 1
    reviews = _review_calls(recorder)
    assert len(reviews) == 1
    review = reviews[0]
    assert "Ada waits." in json.dumps(review["messages"])
    offered = {spec["function"]["name"] for spec in review["tools"]}
    assert "undo_narration" in offered
    assert "append_journal" not in offered
    assert _saved(app, campaign_id)[-1] == ("assistant", "Ada waits.")
    assert _undone_events(app, campaign_id) == []


def test_an_undone_narration_is_retracted_and_written_again(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    script = _script(
        {
            "rounds": [[]],
            "text": "Ada still has one advancement point to spend.",
            "rewrite": "Ada has no advancement point left.",
            "review": [[[UNDO]]],
        }
    )

    events = _stream(client, campaign_id, script)

    assert [e["type"] for e in events].count("retract") == 1
    retract_at = next(i for i, e in enumerate(events) if e["type"] == "retract")
    assert _deltas(events[:retract_at]) == "Ada still has one advancement point to spend."
    assert _deltas(events[retract_at:]) == "Ada has no advancement point left."
    assert events[-1]["type"] == "saved"
    assert _saved(app, campaign_id)[-1] == ("assistant", "Ada has no advancement point left.")

    narration_calls = _narration_calls(recorder, script)
    assert len(narration_calls) == 2
    assert "The last narration was set aside because:" in json.dumps(narration_calls[1])
    undone = _undone_events(app, campaign_id)
    assert len(undone) == 1
    assert json.loads(undone[0].payload)["reason"] == UNDO["arguments"]["reason"]


def test_a_review_that_changes_the_record_without_undoing_still_rewrites(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    script = _script(
        {
            "rounds": [[]],
            "text": "The gate is open.",
            "rewrite": "The gate opens onto a small room.",
            "review": [[[CREATE_GATE]]],
        }
    )

    assert _send(client, campaign_id, script).status_code == 303

    assert "gate room" in _state(app, campaign_id).places
    assert len(_narration_calls(recorder, script)) == 2
    assert _saved(app, campaign_id)[-1] == (
        "assistant",
        "The gate opens onto a small room.",
    )
    undone = _undone_events(app, campaign_id)
    assert len(undone) == 1
    assert "changed the record" in json.loads(undone[0].payload)["reason"]


def test_a_review_can_undo_and_fix_the_record_before_the_rewrite(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    script = _script(
        {
            "rounds": [[]],
            "text": "Ada stands in an empty hall.",
            "rewrite": "Ada finds a gate room behind the hall.",
            "review": [[[UNDO, CREATE_GATE]]],
        }
    )

    assert _send(client, campaign_id, script).status_code == 303

    assert "gate room" in _state(app, campaign_id).places
    assert _saved(app, campaign_id)[-1] == (
        "assistant",
        "Ada finds a gate room behind the hall.",
    )
    assert len(_undone_events(app, campaign_id)) == 1


def test_review_offers_the_usual_tools_and_no_journal_tools(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)

    script = _script({"rounds": [[]], "text": "Ada waits."})
    assert _send(client, campaign_id, script).status_code == 303

    assert len(_narration_calls(recorder, script)) == 1
    review = _review_calls(recorder)[0]
    offered = {spec["function"]["name"] for spec in review["tools"]}
    assert {"create_location", "get_statblock", "add_note"} <= offered
    assert {"append_journal", "replace_in_journal"} & offered == set()
