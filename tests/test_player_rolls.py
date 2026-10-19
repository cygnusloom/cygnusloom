from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusforge.db.models import Campaign, Message
from cygnusforge.llm.base import LLMError
from cygnusforge.llm.fake import FakeProvider
from cygnusforge.rules.dice import is_formula, roll_total
from cygnusforge.services.campaigns import Activity, resume_turn
from tests.test_campaigns import _answer, _campaign_id, _pending, _send
from tests.test_tools import ROLL, _recording_provider, _script


class FlakyNarration:
    """Fails the first narration stream, then behaves like the fake provider."""

    name = "fake"

    def __init__(self) -> None:
        self.inner = FakeProvider()
        self.failed = False
        self.streamed: list[list[dict[str, Any]]] = []

    async def chat(self, model: str, messages: list[dict[str, Any]], tools: Any = None):
        return await self.inner.chat(model, messages, tools)

    async def stream(
        self, model: str, messages: list[dict[str, Any]], tools: Any = None
    ) -> AsyncIterator[Any]:
        self.streamed.append(messages)
        if not self.failed:
            self.failed = True
            raise LLMError("narration broke")
        async for item in self.inner.stream(model, messages, tools):
            yield item


def _saved_messages(app: FastAPI, campaign_id: int) -> list[Message]:
    with Session(app.state.engine) as db:
        return list(
            db.exec(
                select(Message).where(Message.campaign_id == campaign_id).order_by(Message.id)
            ).all()
        )


def test_a_pause_saves_nothing_and_shows_the_prompt(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    app.state.providers = lambda model_id: (_recording_provider(), model_id)
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, _script({"rounds": [[ROLL]], "text": "Well rolled."}))

    assert _saved_messages(app, campaign_id) == []
    assert _pending(app, campaign_id) is not None
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "Roll to persuade." in page
    assert "Roll 1d20" in page
    assert "Modifiers: +1 relationship, +0 circumstance." in page
    assert 'name="content" rows="3" required maxlength="8000"' in page
    assert 'placeholder="Answer the GM above first" disabled>' in page


def test_a_new_message_is_refused_while_a_prompt_is_pending(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    app.state.providers = lambda model_id: (_recording_provider(), model_id)
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, _script({"rounds": [[ROLL]], "text": "Well rolled."}))

    response = _send(client, campaign_id, "Actually, never mind.")

    assert response.status_code == 400
    assert "answer" in response.text
    assert _saved_messages(app, campaign_id) == []
    assert _pending(app, campaign_id) is not None


def test_a_roll_saves_the_reply_with_its_activity(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    app.state.providers = lambda model_id: (_recording_provider(), model_id)
    client, campaign_id = _campaign_id(client_factory)
    script = _script({"rounds": [[ROLL]], "text": "Well rolled."})
    _send(client, campaign_id, script)

    events = _answer(client, campaign_id, "roll")

    kinds = [event["type"] for event in events]
    assert kinds[0] == "activity" and kinds[-1] == "saved"
    activity = next(event["text"] for event in events if event["type"] == "activity")
    assert activity.startswith("You rolled ") and " on the d20" in activity
    saved = _saved_messages(app, campaign_id)
    assert [(m.role, m.content) for m in saved] == [("user", script), ("assistant", "Well rolled.")]
    assert json.loads(saved[-1].activity) == [activity]
    assert _pending(app, campaign_id) is None
    page = client.get(f"/campaigns/{campaign_id}").text
    assert activity in page
    assert "Roll 1d20" not in page


def test_a_text_answer_reaches_the_gm_instead_of_a_roll(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    recorder = _recording_provider()
    app.state.providers = lambda model_id: (recorder, model_id)
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, _script({"rounds": [[ROLL]], "text": "He reads it."}))

    events = _answer(client, campaign_id, "text", "I show him the letter")

    assert events[-1]["type"] == "saved"
    tool_message = next(m for m in recorder.calls[-1]["messages"] if m["role"] == "tool")
    assert json.loads(tool_message["content"]) == {"player_text": "I show him the letter"}
    assert not any(event["type"] == "activity" for event in events)
    assert _pending(app, campaign_id) is None


def test_continue_after_a_failed_narration_does_not_roll_again(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    provider = FlakyNarration()
    app.state.providers = lambda model_id: (provider, model_id)
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, _script({"rounds": [[ROLL]], "text": "Well rolled."}))

    failed = _answer(client, campaign_id, "roll")
    assert failed[-1]["type"] == "error" and "narration broke" in failed[-1]["message"]
    assert _pending(app, campaign_id) is not None
    assert _saved_messages(app, campaign_id) == []

    events = _answer(client, campaign_id, "continue")

    assert events[-1]["type"] == "saved"
    final = provider.streamed[-1]
    assert [m["role"] for m in final].count("tool") == 1
    assert len(json.loads(_saved_messages(app, campaign_id)[-1].activity)) == 1
    assert _pending(app, campaign_id) is None


def test_invalid_answers_are_reported_as_error_events(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    app.state.providers = lambda model_id: (_recording_provider(), model_id)
    client, campaign_id = _campaign_id(client_factory)

    no_turn = _answer(client, campaign_id, "roll")
    assert no_turn[-1]["type"] == "error"
    assert "no reply waiting" in no_turn[-1]["message"]

    _send(client, campaign_id, _script({"rounds": [[ROLL]], "text": "Well rolled."}))

    empty = _answer(client, campaign_id, "text", "   ")
    assert "cannot be empty" in empty[-1]["message"]
    unknown = _answer(client, campaign_id, "dance")
    assert "not available" in unknown[-1]["message"]
    assert _pending(app, campaign_id) is not None


def test_the_player_roll_uses_the_given_generator(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    app.state.providers = lambda model_id: (_recording_provider(), model_id)
    client, campaign_id = _campaign_id(client_factory)
    _send(client, campaign_id, _script({"rounds": [[ROLL]], "text": "Well rolled."}))
    with Session(app.state.engine) as db:
        owner_id = db.get(Campaign, campaign_id).owner_id

    async def roll_with_seed() -> list[Any]:
        events = resume_turn(
            app.state.engine,
            app.state.providers,
            app.state.settings.llm_models,
            app.state.content,
            owner_id,
            campaign_id,
            "roll",
            rng=random.Random(0),
        )
        return [event async for event in events]

    events = asyncio.run(roll_with_seed())

    expected = roll_total("1d20", random.Random(0))
    activity = next(event.text for event in events if isinstance(event, Activity))
    assert activity.startswith(f"You rolled {expected} ")


@pytest.mark.parametrize("formula", ["1d20", "2d6", "10d100"])
def test_valid_formulas(formula: str):
    assert is_formula(formula)


@pytest.mark.parametrize("formula", ["d20", "1d", "0d6", "1d0", "2d6+1", "", None, 20])
def test_invalid_formulas(formula: object):
    assert not is_formula(formula)


def test_roll_totals_stay_within_the_dice():
    rng = random.Random(7)
    totals = [roll_total("2d6", rng) for _ in range(200)]
    assert all(2 <= total <= 12 for total in totals)
    assert len(set(totals)) > 1


def test_roll_total_rejects_non_formulas():
    with pytest.raises(ValueError, match="not a dice formula"):
        roll_total("twenty")
