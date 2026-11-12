"""The state step ends with a finish_state_step tool call. A reply that leaves the checklist out
is asked for again, but the player's story is never blocked on it."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusloom.llm.base import ChatReply, ToolRequest
from cygnusloom.llm.fake import FakeProvider
from cygnusloom.services.campaigns import list_messages
from cygnusloom.services.turns import CHECKLIST_RETRIES
from tests.conftest import NARRATE_TEXT, is_review_messages
from tests.factories import campaign_ready_fast, send_message

FINISH_TOOL = "finish_state_step"


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
        if _is_state_step(messages):
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


def _narration_calls(provider: FakeProvider) -> list[list[dict[str, Any]]]:
    return [
        call
        for call in provider.calls
        if call and call[-1].get("role") == "system" and call[-1].get("content") == NARRATE_TEXT
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
        and FINISH_TOOL in str(message["content"])
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


class _IncompleteFinishCalls(FakeProvider):
    """A provider whose finish_state_step call leaves an area out `incomplete` times."""

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
        if not _is_state_step(messages):
            return reply
        self.state_replies.append(list(messages))
        finishes = [request for request in reply.tool_requests if request.name == FINISH_TOOL]
        if self.incomplete and finishes:
            self.incomplete -= 1
            arguments = json.loads(finishes[0].arguments)
            arguments.pop("movement", None)
            return ChatReply(
                text="",
                tool_requests=(
                    ToolRequest(
                        id=finishes[0].id,
                        name=FINISH_TOOL,
                        arguments=json.dumps(arguments),
                    ),
                ),
                reasoning=reply.reasoning,
            )
        return reply


def test_an_incomplete_finish_call_is_asked_for_again(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = campaign_ready_fast(client_factory)
    provider = _IncompleteFinishCalls(incomplete=1)
    app.state.providers = lambda model_id: (provider, model_id)

    assert send_message(client, campaign_id, _script()).status_code == 303

    assert len(provider.state_replies) == 2
    second = provider.state_replies[1]
    assert any(
        message.get("role") == "tool" and "movement" in str(message.get("content"))
        for message in second
    )
    assert any(
        message.get("role") == "system"
        and "the turn checklist is missing movement" in str(message.get("content"))
        for message in second
    )
    assert _saved_assistant(app, campaign_id)[-1] == "The room is quiet."


def test_an_incomplete_finish_call_never_blocks_the_turn(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = campaign_ready_fast(client_factory)
    provider = _IncompleteFinishCalls(incomplete=CHECKLIST_RETRIES + 5)
    app.state.providers = lambda model_id: (provider, model_id)

    assert send_message(client, campaign_id, _script("The story goes on.")).status_code == 303

    assert len(provider.state_replies) == CHECKLIST_RETRIES + 1
    assert _saved_assistant(app, campaign_id)[-1] == "The story goes on."


def test_the_narration_never_reads_the_checklist_or_its_retry(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = campaign_ready_fast(client_factory)
    provider = _IncompleteStateReplies(incomplete=1)
    app.state.providers = lambda model_id: (provider, model_id)

    assert send_message(client, campaign_id, _script()).status_code == 303

    narration = _narration_calls(provider)
    assert narration
    assert not any(
        message.get("role") == "system"
        and "the turn checklist is missing" in str(message.get("content", ""))
        for message in narration[-1]
    )
    assert not any(
        FINISH_TOOL in str(message.get("content", ""))
        or FINISH_TOOL in json.dumps(message.get("tool_calls", []))
        for message in narration[-1]
    )


class _MarkedChecklistReplies(FakeProvider):
    """Puts a distinctive note in the finish call so a leak is easy to see."""

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatReply:
        reply = await super().chat(model, messages, tools)
        requests = []
        for request in reply.tool_requests:
            if request.name == FINISH_TOOL:
                arguments = json.loads(request.arguments)
                arguments["characters"] = "secret state note"
                request = ToolRequest(
                    id=request.id,
                    name=request.name,
                    arguments=json.dumps(arguments),
                )
            requests.append(request)
        return ChatReply(
            text=reply.text,
            tool_requests=tuple(requests),
            reasoning=reply.reasoning,
        )


def test_the_checklist_notes_never_reach_the_narration_or_review(
    client_factory: Callable[[], TestClient], app: FastAPI
):
    client, campaign_id = campaign_ready_fast(client_factory)
    provider = _MarkedChecklistReplies()
    app.state.providers = lambda model_id: (provider, model_id)

    assert send_message(client, campaign_id, _script()).status_code == 303

    narration = _narration_calls(provider)
    assert narration
    assert "secret state note" not in json.dumps(narration[-1])
    reviews = [call for call in provider.calls if is_review_messages(call)]
    assert reviews
    assert "secret state note" not in json.dumps(reviews[-1])
