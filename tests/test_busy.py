from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.config import Settings
from cygnusloom.db.models import Campaign
from cygnusloom.services import campaigns as service
from tests.conftest import csrf_token
from tests.test_campaigns import _answer, _campaign_id, _pending, _send, _set_model
from tests.test_context import _saved_message_count
from tests.test_streaming import _stream

BUSY = {
    "type": "error",
    "status": 409,
    "message": "the GM is still working on this campaign; try again when it finishes",
}


def _campaign_model(app: FastAPI, campaign_id: int) -> str:
    with Session(app.state.engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        return campaign.model


def _rollback(client: TestClient, campaign_id: int, event_id: int):
    token = csrf_token(client, "/campaigns")
    return client.post(
        f"/campaigns/{campaign_id}/messages/{event_id}/rollback",
        data={"csrf_token": token},
        follow_redirects=False,
    )


def test_every_turn_action_is_refused_while_a_turn_is_running(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = _campaign_id(client_factory)
    saved_before = _saved_message_count(app)
    with service._exclusive(campaign_id):
        assert _send(client, campaign_id, "hi").status_code == 409
        assert _set_model(client, campaign_id, "ollama/test-model").status_code == 409
        assert _rollback(client, campaign_id, 1).status_code == 409
        assert _stream(client, campaign_id, "hi") == [BUSY]
        assert _answer(client, campaign_id, "text", "hello") == [BUSY]

        assert _campaign_model(app, campaign_id) == "fake/scripted"
        assert _saved_message_count(app) == saved_before

    assert _send(client, campaign_id, "hi").status_code == 303


def test_closing_an_unfinished_turn_releases_the_campaign(
    client_factory: Callable[[], TestClient], app: FastAPI, settings: Settings
):
    client, campaign_id = _campaign_id(client_factory)
    with Session(app.state.engine) as db:
        owner_id = db.get(Campaign, campaign_id).owner_id

    async def start_then_abandon() -> None:
        events = service.stream_message(
            app.state.engine,
            app.state.providers,
            settings.llm_models,
            app.state.content,
            settings.llm_context_tokens,
            owner_id,
            campaign_id,
            "hi",
        )
        first = await events.__anext__()
        assert first is not None
        assert campaign_id in service._busy_campaigns
        await events.aclose()

    asyncio.run(start_then_abandon())

    assert campaign_id not in service._busy_campaigns
    assert _pending(app, campaign_id) is not None
    refused = _send(client, campaign_id, "hi")
    assert refused.status_code == 400
    assert "reply" in refused.text
