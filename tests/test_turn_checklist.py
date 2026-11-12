"""The state step ends with a checked-off turn checklist. A reply that leaves items out is asked
for again, but the player's story is never blocked on it."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusloom.llm.base import ChatReply
from cygnusloom.llm.fake import FakeProvider
from cygnusloom.services.campaigns import list_messages
from cygnusloom.services.turns import CHECKLIST_RETRIES
from tests.factories import campaign_ready_fast, send_message


def _is_state_step(messages: list[dict[str, Any]]) -> bool:
    return (
        bool(messages)
        and messages[-1].get("role") == "system"
        and str(messages[-1].get("content", "")).startswith("State management step.")
    )


class _IncompleteStateReplies(FakeProvider):
    """A provider whose state step leaves the checklist out `incomplete` times."""

    def __init__(self, incomplete: int = 1) -> None:
        super().__init__()
        self.incomplete = incomplete
        self.state_replies: list[list[dict[str, Any]]] = []

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatReply:
        reply = await super().chat(model, messages, tools)
        if _is_state_step(messages) and not reply.tool_requests:
            self.state_replies.append(list(messages))
            if self.incomplete:
                self.incomplete -= 1
                return ChatReply(text="[x] characters: no change")
        return reply


def _script(text: str = "The room is quiet.") -> str:
    return "__FAKE__:" + json.dumps({"turns": [{"text": text}]})


def _saved_assistant(app: FastAPI, campaign_id: int) -> list[str]:
    return [
        m.content for m in list_messages(app.state.engine, campaign_id) if m.role == "assistant"
    ]


def test_an_incomplete_checklist_is_asked_for_again_before_narrating(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = campaign_ready_fast(client_factory)
    provider = _IncompleteStateReplies(incomplete=1)
    app.state.providers = lambda model_id: (provider, model_id)

    assert send_message(client, campaign_id, _script()).status_code == 303

    assert len(provider.state_replies) == 2
    retry = provider.state_replies[1]
    assert any(
        message["role"] == "system"
        and "the turn checklist is missing" in str(message["content"])
        and "summaries" in str(message["content"])
        for message in retry
    )
    assert _saved_assistant(app, campaign_id)[-1] == "The room is quiet."


def test_an_incomplete_checklist_never_blocks_the_turn(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = campaign_ready_fast(client_factory)
    provider = _IncompleteStateReplies(incomplete=CHECKLIST_RETRIES + 5)
    app.state.providers = lambda model_id: (provider, model_id)

    assert send_message(client, campaign_id, _script("The story goes on.")).status_code == 303

    assert len(provider.state_replies) == CHECKLIST_RETRIES + 1
    assert _saved_assistant(app, campaign_id)[-1] == "The story goes on."


def test_a_complete_checklist_finishes_the_state_step_without_another_round(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = campaign_ready_fast(client_factory)
    provider = _IncompleteStateReplies(incomplete=0)
    app.state.providers = lambda model_id: (provider, model_id)

    assert send_message(client, campaign_id, _script()).status_code == 303

    assert len(provider.state_replies) == 1
    assert not any(
        message["role"] == "system" and "the turn checklist is missing" in str(message["content"])
        for message in provider.state_replies[0]
    )
    assert _saved_assistant(app, campaign_id)[-1] == "The room is quiet."
