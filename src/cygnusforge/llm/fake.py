"""Scripted provider for tests and development. Use the model id `fake/scripted`.

A user message may carry a directive, `__FAKE__:` followed by a JSON object:

    __FAKE__:{"turns": [
        {"text": "Hello."},
        {"rounds": [[{"name": "social_roll",
                      "arguments": {"prompt": "Roll.", "relationship": 1, "circumstance": 0}}]],
         "text": "You rolled well."},
        {"error": "upstream exploded"}
    ]}

Each user message after the directive uses the next turn. A turn's `rounds` are the tool calls
the model makes, one list per round. The engine runs them for real, so each name must be a tool
in the rule set. A `player_input` tool pauses the turn until the test answers it, so the test
posts the answer next. Its `text` is the narration. Its `error` raises LLMError; the player's message stays and the reply stays open.
Within a turn, `chat` calls (the GM's state steps) get the rounds and return no text, and `stream`
calls (the narration) get `text`. A speech step (its system message starts "Speech step.") gets
the dialogue from the turn's `speech` object, keyed by the speaker's name. A speaker not listed
there gets the intent back as its dialogue. A newer directive replaces the older one. When the
script runs out, the provider echoes the last user message. The script lives in the transcript,
so it needs no server-side state.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any

from cygnusforge.llm.base import ChatReply, LLMError, TextDelta, ToolRequest

DIRECTIVE = "__FAKE__:"
SPEECH_MARKER = "Speech step."
_decoder = json.JSONDecoder()
_WORDS = re.compile(r"\s*\S+|\s+")


class FakeProvider:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatReply:
        self.calls.append(messages)
        if _is_speech(messages):
            return _speech(messages)
        return _respond(messages, decision=True)

    async def stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[TextDelta | ChatReply]:
        self.calls.append(messages)
        reply = _respond(messages, decision=False)  # errors surface before anything is yielded
        for piece in _WORDS.findall(reply.text):
            yield TextDelta(piece)
        yield reply


def _is_speech(messages: list[dict[str, Any]]) -> bool:
    return (
        bool(messages)
        and messages[0]["role"] == "system"
        and SPEECH_MARKER in messages[0]["content"]
    )


def _speech(messages: list[dict[str, Any]]) -> ChatReply:
    system = messages[0]["content"]
    current = _current_turn(messages)
    turn = current[1] if current is not None else {}
    if "error" in turn:
        raise LLMError(str(turn["error"]))
    speech = turn.get("speech", {})
    if not isinstance(speech, dict):
        raise LLMError('"speech" must be an object mapping speaker names to dialogue')
    speaker = _line_value(system, "Speaker: ")
    if speaker in speech:
        return ChatReply(text=str(speech[speaker]))
    intent = system.split("\n\nIntent: ", 1)[-1]
    return ChatReply(text=intent)


def _line_value(text: str, prefix: str) -> str:
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return ""


def _current_turn(messages: list[dict[str, Any]]) -> tuple[int, dict[str, Any]] | None:
    """The scripted turn for the latest user message, with its index, or None when the script
    has run out or there is none."""
    start = _latest_directive(messages)
    if start is None:
        return None
    turns = _parse_directive(messages[start]["content"])
    turn_index = sum(1 for m in messages[start:] if m["role"] == "user") - 1
    if turn_index >= len(turns):
        return None
    return turn_index, _turn(turns[turn_index])


def _respond(messages: list[dict[str, Any]], decision: bool) -> ChatReply:
    current = _current_turn(messages)
    if current is None:
        # Decisions stay silent so the narration is the only text the model produces.
        return ChatReply(text="" if decision else _echo(messages))
    turn_index, turn = current
    if "error" in turn:
        raise LLMError(str(turn["error"]))
    if not decision:
        return ChatReply(text=str(turn.get("text", "")))
    rounds = _rounds(turn)
    # Tool rounds already taken this turn are the assistant messages with tool calls since
    # the latest user message.
    last_user = max(i for i, m in enumerate(messages) if m["role"] == "user")
    done = sum(1 for m in messages[last_user + 1 :] if "tool_calls" in m)
    if done >= len(rounds):
        return ChatReply(text="")
    prefix = f"fake_{turn_index}_{done}"
    return ChatReply(text="", tool_requests=_tool_requests(rounds[done], prefix))


def _echo(messages: list[dict[str, Any]]) -> str:
    last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    return f"You said: {last_user}"


def _latest_directive(messages: list[dict[str, Any]]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message["role"] == "user" and DIRECTIVE in message["content"]:
            return index
    return None


def _parse_directive(content: str) -> list[Any]:
    body = content[content.index(DIRECTIVE) + len(DIRECTIVE) :].lstrip()
    try:
        obj, _ = _decoder.raw_decode(body)
    except ValueError as exc:
        raise LLMError(f"invalid {DIRECTIVE} JSON: {exc}") from exc
    if not isinstance(obj, dict) or not isinstance(obj.get("turns", []), list):
        raise LLMError(f"{DIRECTIVE} must be a JSON object with a turns list")
    return obj.get("turns", [])


def _turn(turn: Any) -> dict[str, Any]:
    if not isinstance(turn, dict):
        raise LLMError("each scripted turn must be a JSON object")
    return turn


def _rounds(turn: dict[str, Any]) -> list[Any]:
    rounds = turn.get("rounds", [])
    if not isinstance(rounds, list):
        raise LLMError('"rounds" must be a list of tool-call lists')
    return rounds


def _tool_requests(calls: Any, prefix: str) -> tuple[ToolRequest, ...]:
    if not isinstance(calls, list):
        raise LLMError("each scripted round must be a list of tool calls")
    requests = []
    for position, call in enumerate(calls):
        if not isinstance(call, dict) or not isinstance(call.get("name"), str):
            raise LLMError("each scripted tool call needs a name")
        arguments = json.dumps(call.get("arguments", {}), sort_keys=True)
        requests.append(
            ToolRequest(id=f"{prefix}_{position}", name=call["name"], arguments=arguments)
        )
    return tuple(requests)
