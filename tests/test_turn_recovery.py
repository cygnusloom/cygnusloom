"""Recovery and progress signals for a turn: retries, heartbeats, status, and the live map."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.llm.base import ChatReply
from cygnusloom.llm.fake import FakeProvider
from cygnusloom.services import turns as turns_service
from cygnusloom.services.campaigns import list_messages
from cygnusloom.services.events import events_since
from tests import factories


def _stream(client: TestClient, campaign_id: int, text: str) -> list[dict[str, Any]]:
    token = factories.csrf_token(client, "/campaigns")
    response = client.post(
        f"/campaigns/{campaign_id}/messages/stream",
        data={"csrf_token": token, "content": text},
    )
    assert response.status_code == 200
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def _kinds(app: FastAPI, campaign_id: int) -> list[str]:
    with Session(app.state.engine) as db:
        return [row.kind for row in events_since(db, campaign_id)]


def _saved(app: FastAPI, campaign_id: int) -> list[tuple[str, str]]:
    return [(m.role, m.content) for m in list_messages(app.state.engine, campaign_id)]


class _SilentSpeech(FakeProvider):
    """Answers every speech step with nothing, however many times it is asked."""

    def __init__(self) -> None:
        super().__init__()
        self.speech_calls: list[list[dict[str, Any]]] = []

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatReply:
        if "Speech step." in str(messages[0]["content"]):
            self.speech_calls.append(messages)
            return ChatReply(text="")
        return await super().chat(model, messages, tools)


class _SlowProvider(FakeProvider):
    """Takes long enough at every model call that the heartbeat has to speak."""

    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatReply:
        await asyncio.sleep(self.delay)
        return await super().chat(model, messages, tools)

    async def stream(self, model, messages, tools=None):
        # One silent delay models a hung stream. Sleeping per word would only make the test
        # longer, not slow down a phase any more than this does.
        await asyncio.sleep(self.delay)
        async for item in super().stream(model, messages, tools):
            yield item


def _speech_script() -> str:
    return "__FAKE__:" + json.dumps(
        {
            "turns": [
                {
                    "rounds": [
                        [
                            {
                                "name": "speak",
                                "arguments": {"npc": "Mara Thistledown", "intent": "Warn Ada."},
                            }
                        ]
                    ],
                    "text": "Mara looks up from her kettle.",
                }
            ]
        }
    )


def test_a_silent_speech_step_is_a_tool_error_not_a_dead_turn(
    client_factory: Callable[[], TestClient], app: FastAPI
) -> None:
    """Three empty attempts must not throw away a reply the player has waited for."""
    provider = _SilentSpeech()
    app.state.providers = lambda model_id: (provider, model_id)
    client, campaign_id = factories.campaign_ready(client_factory)
    factories.place_pc(client, campaign_id)

    response = factories.send_message(client, campaign_id, _speech_script())

    assert response.status_code == 303, response.text[:300]
    kinds = _kinds(app, campaign_id)
    assert "turn.failed" not in kinds
    assert "chat.gm" in kinds
    assert "speech.written" in kinds
    # The retry restates the one thing that went wrong.
    assert len(provider.speech_calls) == turns_service.SPEECH_ATTEMPTS
    retry = provider.speech_calls[1]
    assert any("Your last reply was empty" in str(message["content"]) for message in retry)


def test_a_slow_model_call_sends_a_heartbeat(
    client_factory: Callable[[], TestClient], app: FastAPI, monkeypatch
) -> None:
    """A silent stream looks hung, so a slow call reports that the GM is still working."""
    provider = _SlowProvider(delay=0.05)
    app.state.providers = lambda model_id: (provider, model_id)
    monkeypatch.setattr(turns_service, "HEARTBEAT_SECONDS", 0.01)
    client, campaign_id = factories.campaign_ready(client_factory)

    events = _stream(client, campaign_id, "I look around.")

    heartbeats = [
        event for event in events if event["type"] == "status" and "Still working" in event["text"]
    ]
    assert heartbeats
    # The turn still finished normally after the heartbeats.
    assert any(event["type"] in {"saved", "prompt"} for event in events)


def test_turn_status_reports_a_pending_opening_and_a_finished_turn(
    client_factory: Callable[[], TestClient],
) -> None:
    client = factories.signed_in_client(client_factory)
    campaign_id = factories.create_campaign(client)
    created = factories.create_character(client, campaign_id)
    assert created.status_code == 303, created.text[:300]

    waiting = client.get(f"/campaigns/{campaign_id}/turn-status")
    assert waiting.status_code == 200
    assert waiting.json()["status"] in {"busy", "prompt"}

    factories.answer(client, campaign_id, "continue")

    assert client.get(f"/campaigns/{campaign_id}/turn-status").json()["status"] == "idle"


def test_turn_status_is_not_readable_for_another_users_campaign(
    client_factory: Callable[[], TestClient],
) -> None:
    owner = factories.signed_in_client(client_factory, email="owner@example.com")
    campaign_id = factories.create_campaign(owner)
    stranger = factories.signed_in_client(client_factory, email="stranger@example.com")

    assert stranger.get(f"/campaigns/{campaign_id}/turn-status").status_code == 404


def test_the_map_fragment_renders_the_panel_on_its_own(
    client_factory: Callable[[], TestClient],
) -> None:
    """The turn stream redraws this fragment, so it must carry the map and nothing else."""
    client, campaign_id = factories.campaign_ready(client_factory)

    response = client.get(f"/campaigns/{campaign_id}/map")

    assert response.status_code == 200
    assert 'class="map-panel"' in response.text
    assert "<html" not in response.text
    page = client.get(f"/campaigns/{campaign_id}")
    assert f'data-map-url="/campaigns/{campaign_id}/map"' in page.text
    assert f'data-status-url="/campaigns/{campaign_id}/turn-status"' in page.text


def test_a_finished_turn_can_be_rendered_as_a_live_fragment(
    client_factory: Callable[[], TestClient],
) -> None:
    """The stream swaps this fragment in, so it carries every view that a turn can change."""
    client, campaign_id = factories.campaign_ready_fast(client_factory)

    response = client.get(f"/campaigns/{campaign_id}/live")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert 'data-view="play"' in response.text
    assert "data-transcript" in response.text
    assert 'data-view="journal"' in response.text
    assert 'data-view="characters"' in response.text
    assert "<html" not in response.text
    page = client.get(f"/campaigns/{campaign_id}")
    assert f'data-live-url="/campaigns/{campaign_id}/live"' in page.text


def test_the_live_fragment_is_not_readable_for_another_users_campaign(
    client_factory: Callable[[], TestClient],
) -> None:
    owner = factories.signed_in_client(client_factory, email="owner@example.com")
    campaign_id = factories.create_campaign(owner)
    stranger = factories.signed_in_client(client_factory, email="stranger@example.com")

    assert stranger.get(f"/campaigns/{campaign_id}/live").status_code == 404


def test_the_live_fragment_is_refused_while_the_campaign_is_still_being_created(
    client_factory: Callable[[], TestClient],
) -> None:
    """A creation page has no live region; the page falls back to a normal reload."""
    client = factories.signed_in_client(client_factory)
    campaign_id = factories.create_campaign(client)

    assert client.get(f"/campaigns/{campaign_id}/live").status_code == 409
