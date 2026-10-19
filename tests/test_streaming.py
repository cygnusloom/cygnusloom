from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusforge.config import Settings
from cygnusforge.db.models import Campaign
from cygnusforge.llm.base import LLMError
from cygnusforge.services.campaigns import list_messages
from tests.conftest import csrf_token
from tests.test_campaigns import _answer, _campaign_id
from tests.test_context import RecordingProvider, _saved_message_count, _small_window_app, _stored
from tests.test_tools import ROLL, _recording_provider, _script


def _stream(client: TestClient, campaign_id: int, text: str) -> list[dict[str, Any]]:
    token = csrf_token(client, "/campaigns")
    response = client.post(
        f"/campaigns/{campaign_id}/messages/stream",
        data={"csrf_token": token, "content": text},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def _deltas(events: list[dict[str, Any]]) -> str:
    return "".join(e["text"] for e in events if e["type"] == "delta")


def _assistant_ids(app: FastAPI, campaign_id: int) -> list[int]:
    return [m.id for m in list_messages(app.state.engine, campaign_id) if m.role == "assistant"]


def test_stream_sends_status_then_deltas_then_saved(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)

    events = _stream(client, campaign_id, _script({"text": "Hello there, friend."}))

    assert events[0] == {"type": "status", "text": "Deciding..."}
    assert _deltas(events) == "Hello there, friend."
    assert events[-1] == {"type": "saved", "event_id": _assistant_ids(app, campaign_id)[-1]}


def test_each_phase_shows_a_status_and_only_the_narration_is_streamed(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)

    paused = _stream(client, campaign_id, _script({"rounds": [[ROLL]], "text": "Done."}))
    assert paused[-1]["type"] == "prompt"
    assert paused[-1]["details"] == "Modifiers: +1 relationship, +0 circumstance."
    assert [e["text"] for e in paused if e["type"] == "status"] == [
        "Deciding...",
        "Running social_roll...",
    ]

    events = _answer(client, campaign_id, "roll")

    statuses = [e["text"] for e in events if e["type"] == "status"]
    assert statuses == ["Deciding...", "Narrating..."]
    narrating_at = events.index({"type": "status", "text": "Narrating..."})
    assert _deltas(events[:narrating_at]) == ""
    assert _deltas(events) == "Done."
    assert events[-1]["type"] == "saved"


def test_decisions_offer_tools_and_the_narration_does_not(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    client, campaign_id = _campaign_id(client_factory)

    _stream(client, campaign_id, _script({"rounds": [[ROLL]], "text": "Done."}))
    _answer(client, campaign_id, "roll")

    assert [call["tools"] is not None for call in recorder.calls] == [True, True, False]


def test_compression_is_announced_as_a_status(settings: Settings):
    provider = RecordingProvider()
    app = _small_window_app(settings, provider)
    client, campaign_id = _campaign_id(lambda: TestClient(app))

    events: list[dict[str, Any]] = []
    for index in range(10):
        events = _stream(client, campaign_id, f"m{index} " + "x" * 600)
        if _stored(app, campaign_id).context_summary:
            break

    assert _stored(app, campaign_id).context_summary
    assert {"type": "status", "text": "Summarizing older messages..."} in events


def test_retired_campaign_model_is_reported_in_band_and_saves_nothing(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    with Session(app.state.engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        campaign.model = "retired/model"
        db.add(campaign)
        db.commit()

    events = _stream(client, campaign_id, "hello")

    assert events == [{"type": "error", "status": 400, "message": "that model is not available"}]
    assert _saved_message_count(app) == 0


def test_provider_failure_is_reported_and_saves_nothing(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    class Broken:
        name = "broken"

        async def chat(self, model, messages, tools=None):
            raise LLMError("provider broken is unreachable")

        async def stream(self, model, messages, tools=None):
            raise LLMError("provider broken is unreachable")
            yield  # pragma: no cover

    app.state.providers = lambda model_id: (Broken(), "model")
    client, campaign_id = _campaign_id(client_factory)

    events = _stream(client, campaign_id, "are you there?")

    assert events[0] == {"type": "status", "text": "Deciding..."}
    assert events[-1] == {
        "type": "error",
        "status": 502,
        "message": "provider broken is unreachable",
    }
    assert not any(e["type"] == "saved" for e in events)
    assert _saved_message_count(app) == 0


def test_a_recording_provider_sees_the_streamed_turn(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    client, campaign_id = _campaign_id(client_factory)

    events = _stream(client, campaign_id, _script({"text": "Hi."}))

    assert recorder.calls
    assert _deltas(events) == "Hi."
