"""Turns run to completion when the page that started them goes away.

`TestClient` buffers ASGI responses, so a real socket disconnect cannot be simulated here. The
tests drive the job directly instead: a follower is closed exactly as Starlette closes a
cancelled response generator, then the gated provider is released and the turn must still
finish on its own.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from cygnusloom.db.models import Campaign
from cygnusloom.llm.base import ChatReply, TextDelta
from cygnusloom.llm.fake import FakeProvider
from cygnusloom.services import campaigns as service
from cygnusloom.services.campaigns import list_messages
from cygnusloom.web import campaigns as campaigns_web
from tests import factories

BUSY = {
    "type": "error",
    "status": 409,
    "message": "the GM is still working on this campaign; try again when it finishes",
}
NARRATION = "The door creaks open."


class _GatedProvider(FakeProvider):
    """The scripted fake, held inside the narration after its first words.

    The turn reaches the gate with a status and part of the reply already streamed, which is
    where a page that reloads should find a live turn. The gate is a thread event waited on in a
    worker thread, so the event loop (and the pages served on it) keeps running while it waits.
    """

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self.reached = threading.Event()
        self.narrations = 0

    async def stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[TextDelta | ChatReply]:
        self.narrations += 1
        if self.narrations > 1:
            async for item in super().stream(model, messages, tools):
                yield item
            return
        yield TextDelta("The door ")
        self.reached.set()
        await asyncio.to_thread(self.release.wait)
        yield TextDelta("creaks open.")
        yield ChatReply(text=NARRATION)


def _owner_id(app: FastAPI, campaign_id: int) -> int:
    with Session(app.state.engine) as db:
        campaign = db.get(Campaign, campaign_id)
        assert campaign is not None
        return campaign.owner_id


def _events(
    app: FastAPI, campaign_id: int, text: str = "I look around."
) -> AsyncIterator[service.TurnEvent]:
    settings = app.state.settings
    return service.stream_message(
        app.state.engine,
        app.state.providers,
        settings.llm_models,
        app.state.content,
        settings.llm_context_tokens,
        _owner_id(app, campaign_id),
        campaign_id,
        text,
    )


def _start_job(app: FastAPI, campaign_id: int, text: str = "I look around."):
    return app.state.turn_jobs.start(
        campaign_id, _events(app, campaign_id, text), campaigns_web._turn_lines
    )


def _saved(role: str, app: FastAPI, campaign_id: int) -> list[str]:
    return [m.content for m in list_messages(app.state.engine, campaign_id) if m.role == role]


def _scenario(client_factory: Callable[[], TestClient], app: FastAPI):
    client, campaign_id = factories.campaign_ready_fast(client_factory)
    provider = _GatedProvider()
    app.state.providers = lambda model_id: (provider, model_id)
    return client, campaign_id, provider


def test_closing_the_follower_does_not_stop_the_turn(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    """The exact unload case: the page closes its stream while the GM is still writing."""
    client, campaign_id, provider = _scenario(client_factory, app)

    async def start_and_close() -> tuple[Any, str]:
        job = _start_job(app, campaign_id)
        follower = job.follow()
        first = await anext(follower)
        await follower.aclose()
        return job, first

    job, first = client.portal.call(start_and_close)
    assert json.loads(first)["type"] == "status"
    assert app.state.turn_jobs.active(campaign_id) is True

    provider.release.set()
    client.portal.call(job.wait)

    assert app.state.turn_jobs.active(campaign_id) is False
    assert service.turn_busy(campaign_id) is False
    assert service.turn_progress(campaign_id) == ("", "")
    assert factories.pending_turn(app, campaign_id) is None
    assert _saved("assistant", app, campaign_id)[-1] == NARRATION


def test_a_reload_while_the_job_runs_shows_the_live_turn_and_then_the_reply(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    """A page that comes back mid-turn sees the same busy state and partial reply."""
    client, campaign_id, provider = _scenario(client_factory, app)

    async def start_until_delta() -> Any:
        job = _start_job(app, campaign_id)
        follower = job.follow()
        try:
            for _ in range(50):
                line = await anext(follower)
                if json.loads(line)["type"] == "delta":
                    return job
            raise AssertionError("the turn never streamed a delta")
        finally:
            await follower.aclose()

    job = client.portal.call(start_until_delta)
    assert provider.reached.is_set()

    snapshot = client.get(f"/campaigns/{campaign_id}/turn-status").json()
    assert snapshot["status"] == "busy"
    assert snapshot["running"] is True
    assert snapshot["activity"] == "Narrating..."
    assert snapshot["reply"] == "The door "

    page = client.get(f"/campaigns/{campaign_id}/live")
    assert page.status_code == 200
    assert 'data-gm-state="busy"' in page.text
    assert "data-gm-working" in page.text
    assert "Narrating..." in page.text
    assert "The door " in page.text
    composer = page.text.split('id="composer"', 1)[1]
    assert "<textarea" in composer
    assert "disabled" in composer.split("</form>", 1)[0]

    provider.release.set()
    client.portal.call(job.wait)

    # A full reload after the turn reads the saved reply from the log.
    finished = client.get(f"/campaigns/{campaign_id}")
    assert NARRATION in finished.text
    assert "data-gm-working" not in finished.text
    assert 'data-gm-state="waiting"' in finished.text

    async def replay() -> list[str]:
        return [line async for line in job.follow()]

    lines = client.portal.call(replay)
    assert json.loads(lines[0])["type"] == "status"
    assert json.loads(lines[-1])["type"] == "saved"


def test_a_second_turn_is_refused_while_the_job_is_active(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id, provider = _scenario(client_factory, app)

    async def start() -> Any:
        return _start_job(app, campaign_id)

    job = client.portal.call(start)
    assert provider.reached.wait(timeout=30), "the turn never reached the provider"

    token = factories.csrf_token(client, "/campaigns")
    streamed = client.post(
        f"/campaigns/{campaign_id}/messages/stream",
        data={"csrf_token": token, "content": "Again."},
    )
    assert streamed.status_code == 200
    assert [json.loads(line) for line in streamed.text.splitlines() if line.strip()] == [BUSY]

    plain = client.post(
        f"/campaigns/{campaign_id}/messages",
        data={"csrf_token": token, "content": "Again."},
        follow_redirects=False,
    )
    assert plain.status_code == 409

    provider.release.set()
    client.portal.call(job.wait)


def test_a_finished_job_is_forgotten_and_the_next_turn_starts(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id, provider = _scenario(client_factory, app)
    provider.release.set()

    async def start() -> Any:
        return _start_job(app, campaign_id)

    job = client.portal.call(start)
    client.portal.call(job.wait)
    assert app.state.turn_jobs.active(campaign_id) is False

    token = factories.csrf_token(client, "/campaigns")
    second = client.post(
        f"/campaigns/{campaign_id}/messages/stream",
        data={"csrf_token": token, "content": "Again."},
    )
    assert second.status_code == 200
    events = [json.loads(line) for line in second.text.splitlines() if line.strip()]
    assert events[-1]["type"] == "saved"
    assert app.state.turn_jobs.active(campaign_id) is False
