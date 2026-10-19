"""Scripted provider for tests and development. Use the model id `fake/scripted`.

A user message may carry a directive, `__FAKE__:` followed by a JSON object:

    __FAKE__:{"turns": [
        {"text": "Hello."},
        {"text": "Rolling.", "tool_calls": [{"name": "roll", "arguments": {"sides": 20}, "output": "14"}]},
        {"error": "upstream exploded"}
    ]}

Each assistant turn after the directive consumes the next entry. A turn may set `text`,
`tool_calls` (rendered as tagged text), or `error` (raises LLMError, so nothing is saved). A
newer directive replaces the older one. When the script runs out, the provider echoes the last
user message. The script lives in the transcript, so it needs no server-side state.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from cygnusforge.llm.base import LLMError

DIRECTIVE = "__FAKE__:"
_decoder = json.JSONDecoder()


class FakeProvider:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    async def stream_text(self, model: str, messages: list[dict[str, Any]]) -> AsyncIterator[str]:
        self.calls.append(messages)
        yield _respond(messages)


def _respond(messages: list[dict[str, Any]]) -> str:
    start = _latest_directive(messages)
    if start is None:
        return _echo(messages)
    turns = _parse_directive(messages[start]["content"])
    index = sum(1 for m in messages[start + 1 :] if m["role"] == "assistant")
    if index >= len(turns):
        return _echo(messages)
    return _render(turns[index])


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


def _render(turn: Any) -> str:
    if not isinstance(turn, dict):
        raise LLMError("each scripted turn must be a JSON object")
    if "error" in turn:
        raise LLMError(str(turn["error"]))
    parts = [str(turn.get("text", ""))]
    for call in turn.get("tool_calls", []):
        if not isinstance(call, dict) or not isinstance(call.get("name"), str):
            raise LLMError("each scripted tool call needs a name")
        arguments = json.dumps(call.get("arguments", {}), sort_keys=True)
        parts.append(f'<tool_call name="{call["name"]}">{arguments}</tool_call>')
        if "output" in call:
            parts.append(f"<tool_output>{call['output']}</tool_output>")
    return "\n".join(part for part in parts if part)
