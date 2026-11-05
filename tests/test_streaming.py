from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.config import Settings
from cygnusloom.db.models import Campaign
from cygnusloom.llm.base import ChatReply, LLMError, ToolRequest
from cygnusloom.llm.fake import FakeProvider
from cygnusloom.services.campaigns import list_messages
from tests.conftest import NARRATE_TEXT, csrf_token
from tests.test_campaigns import _answer, _campaign_id
from tests.test_context import (
    RecordingProvider,
    _saved_message_count,
    _small_window_app,
    _state,
)
from tests.test_journal import _day_campaign
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

    assert events[0] == {"type": "status", "text": "Managing state..."}
    assert _deltas(events) == "Hello there, friend."
    assert events[-1] == {"type": "saved", "event_id": _assistant_ids(app, campaign_id)[-1]}


# A day ending runs the journal step after the clock moves. Only Cairn has advance_time.
ADVANCE_DAY = {"name": "advance_time", "arguments": {"days": 1}}


def test_each_phase_shows_a_status_and_only_the_narration_is_streamed(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _day_campaign(client_factory)

    events = _stream(client, campaign_id, _script({"rounds": [[ADVANCE_DAY]], "text": "Done."}))

    statuses = [e["text"] for e in events if e["type"] == "status"]
    assert statuses == [
        "Managing state...",
        "Running advance_time...",
        "Managing state...",
        "Updating the journal...",
        "Narrating...",
        "Reviewing the turn...",
    ]
    narrating_at = events.index({"type": "status", "text": "Narrating..."})
    assert _deltas(events[:narrating_at]) == ""
    assert _deltas(events) == "Done."
    assert events[-1]["type"] == "saved"


def test_state_steps_offer_tools_and_the_narration_does_not(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _day_campaign(client_factory)
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)

    _stream(client, campaign_id, _script({"rounds": [[ADVANCE_DAY]], "text": "Done."}))

    assert [call["tools"] is not None for call in recorder.calls] == [
        True,
        True,
        True,
        False,
        True,
    ]


def test_compression_is_announced_as_a_status(settings: Settings):
    provider = RecordingProvider()
    app = _small_window_app(settings, provider)
    client, campaign_id = _campaign_id(lambda: TestClient(app))

    events: list[dict[str, Any]] = []
    for index in range(10):
        events = _stream(client, campaign_id, f"m{index} " + "x" * 600)
        if _state(app, campaign_id).summary:
            break

    assert _state(app, campaign_id).summary
    assert {"type": "status", "text": "Summarizing older messages..."} in events


def test_retired_campaign_model_is_reported_in_band_and_saves_nothing(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    saved_before = _saved_message_count(app)
    with Session(app.state.engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        campaign.model = "retired/model"
        db.add(campaign)
        db.commit()

    events = _stream(client, campaign_id, "hello")

    assert events == [{"type": "error", "status": 400, "message": "that model is not available"}]
    assert _saved_message_count(app) == saved_before


def test_provider_failure_is_reported_and_keeps_the_message(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    class Broken:
        name = "broken"

        async def chat(self, model, messages, tools=None):
            raise LLMError("provider broken is unreachable")

        async def stream(self, model, messages, tools=None):
            raise LLMError("provider broken is unreachable")
            yield  # pragma: no cover

    client, campaign_id = _campaign_id(client_factory)
    app.state.providers = lambda model_id: (Broken(), "model")

    events = _stream(client, campaign_id, "are you there?")

    assert events[0] == {"type": "status", "text": "Managing state..."}
    assert events[-1] == {
        "type": "error",
        "status": 502,
        "message": "provider broken is unreachable",
    }
    assert not any(e["type"] == "saved" for e in events)
    assert _saved_message_count(app) == 2
    turn = _state(app, campaign_id).turn
    assert turn is not None and turn.failed


def test_a_recording_provider_sees_the_streamed_turn(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    client, campaign_id = _campaign_id(client_factory)

    events = _stream(client, campaign_id, _script({"text": "Hi."}))

    assert recorder.calls
    assert _deltas(events) == "Hi."


OOC_REPLY = {"name": "ooc", "arguments": {"no_narration": True}}
OOC_THEN_STORY = {"name": "ooc", "arguments": {"no_narration": False}}


def test_out_of_character_reply_is_the_final_message_when_nothing_is_narrated(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)

    events = _stream(
        client,
        campaign_id,
        _script({"rounds": [[OOC_REPLY]], "ooc": "The rules say two rolls for a wound."}),
    )

    assert _deltas(events) == "The rules say two rolls for a wound."
    assert "next_message" not in [e["type"] for e in events]
    assert "Narrating..." not in [e.get("text") for e in events]
    assert events[-1] == {"type": "saved", "event_id": _assistant_ids(app, campaign_id)[-1]}
    assert _state(app, campaign_id).turn is None


def test_out_of_character_reply_can_be_followed_by_the_story(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)

    events = _stream(
        client,
        campaign_id,
        _script(
            {"rounds": [[OOC_THEN_STORY]], "ooc": "Sure, ask away.", "text": "The tide turns."}
        ),
    )

    split = events.index({"type": "next_message"})
    assert _deltas(events[:split]) == "Sure, ask away."
    assert _deltas(events[split:]) == "The tide turns."
    assert events[-1]["type"] == "saved"
    assert len(_assistant_ids(app, campaign_id)) == 3
    assert _state(app, campaign_id).turn is None


def test_out_of_character_step_is_not_offered_the_ooc_tool(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    client, campaign_id = _campaign_id(client_factory)

    events = _stream(
        client,
        campaign_id,
        _script(
            {"rounds": [[OOC_THEN_STORY]], "ooc": "Sure, ask away.", "text": "The tide turns."}
        ),
    )

    ooc_calls = [
        call
        for call in recorder.calls
        if call["tools"] is not None
        and call["messages"]
        and call["messages"][-1]["role"] == "system"
        and "Out-of-character step." in call["messages"][-1]["content"]
    ]
    assert ooc_calls
    offered = {spec["function"]["name"] for spec in ooc_calls[0]["tools"]}
    assert "ooc" not in offered
    assert events[-1]["type"] == "saved"


class _OocCallsItself(FakeProvider):
    """A model that ignores the tool list and calls ooc again from the OOC step."""

    def __init__(self) -> None:
        super().__init__()
        self.ooc_steps = 0

    async def stream(self, model, messages, tools=None):
        if (
            messages
            and messages[-1]["role"] == "system"
            and "Out-of-character step." in messages[-1]["content"]
        ):
            self.ooc_steps += 1
            self.calls.append(messages)
            yield ChatReply(
                text="Sure, ask away.",
                tool_requests=(
                    ToolRequest(
                        id="ooc-again",
                        name="ooc",
                        arguments='{"no_narration": false}',
                    ),
                ),
            )
            return
        async for item in super().stream(model, messages, tools):
            yield item


def test_an_ooc_restart_request_is_ignored(client_factory: Callable[[], TestClient], app: FastAPI):
    provider = _OocCallsItself()
    app.state.providers = lambda model_id: (provider, model_id)
    client, campaign_id = _campaign_id(client_factory)

    events = _stream(
        client,
        campaign_id,
        _script(
            {"rounds": [[OOC_THEN_STORY]], "ooc": "Sure, ask away.", "text": "The tide turns."}
        ),
    )

    assert provider.ooc_steps == 1
    assert _deltas(events) == "The tide turns."
    assert events[-1]["type"] == "saved"
    assert _state(app, campaign_id).turn is None
    saved = [
        message.content
        for message in list_messages(app.state.engine, campaign_id)
        if message.role == "assistant"
    ]
    assert saved[-2:] == ["Sure, ask away.", "The tide turns."]


def test_out_of_character_step_can_roll_and_resume_with_its_reply(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    opening_replies = _assistant_ids(app, campaign_id)

    paused = _stream(
        client,
        campaign_id,
        _script({"rounds": [[OOC_REPLY], [ROLL]], "ooc": "Roll to see if they listen."}),
    )
    assert paused[-1]["type"] == "prompt"
    assert _assistant_ids(app, campaign_id) == opening_replies

    events = _answer(client, campaign_id, "roll")

    assert _deltas(events) == "Roll to see if they listen."
    assert events[-1]["type"] == "saved"
    assert _state(app, campaign_id).turn is None


def test_empty_out_of_character_reply_fails_and_keeps_the_message(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    opening_replies = _assistant_ids(app, campaign_id)

    events = _stream(client, campaign_id, _script({"rounds": [[OOC_REPLY]]}))

    assert events[-1]["type"] == "error"
    assert "empty reply" in events[-1]["message"]
    assert _assistant_ids(app, campaign_id) == opening_replies
    assert _saved_message_count(app) == 2


def _empty_narrations(provider: Any, empty: int) -> Any:
    """Wrap a provider so its first `empty` narration calls come back with no text."""

    class EmptyFirst:
        name = provider.name

        def __init__(self) -> None:
            self.empty_left = empty

        async def chat(self, model, messages, tools=None):
            return await provider.chat(model, messages, tools)

        async def stream(self, model, messages, tools=None):
            narrating = any(m.get("content") == NARRATE_TEXT for m in messages)
            if narrating and self.empty_left:
                self.empty_left -= 1
                yield ChatReply(text="")
                return
            async for item in provider.stream(model, messages, tools):
                yield item

    return EmptyFirst()


def test_an_empty_narration_is_asked_for_again(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    recorder = _recording_provider()
    client, campaign_id = _campaign_id(client_factory)
    app.state.providers = lambda model_id: (_empty_narrations(recorder, 1), model_id)
    paused = _stream(client, campaign_id, _script({"rounds": [[ROLL]], "text": "Done."}))
    assert paused[-1]["type"] == "prompt"

    events = _answer(client, campaign_id, "roll")

    assert _deltas(events) == "Done."
    assert events[-1]["type"] == "saved"
    assert _state(app, campaign_id).turn is None


def test_two_empty_narrations_fail_the_turn(client_factory: Callable[[], TestClient], app: FastAPI):
    recorder = _recording_provider()
    client, campaign_id = _campaign_id(client_factory)
    app.state.providers = lambda model_id: (_empty_narrations(recorder, 2), model_id)
    paused = _stream(client, campaign_id, _script({"rounds": [[ROLL]], "text": "Done."}))
    assert paused[-1]["type"] == "prompt"

    events = _answer(client, campaign_id, "roll")

    assert events[-1]["type"] == "error"
    assert "empty reply" in events[-1]["message"]
    turn = _state(app, campaign_id).turn
    assert turn is not None and turn.failed
