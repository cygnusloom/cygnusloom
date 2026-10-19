from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from cygnusforge.llm.base import ChatReply, LLMError
from cygnusforge.llm.fake import FakeProvider


def _chat(messages: list[dict[str, Any]]) -> ChatReply:
    return asyncio.run(FakeProvider().chat("scripted", messages))


def _run(messages: list[dict[str, Any]]) -> str:
    return _chat(messages).text


def _user(content: str) -> dict[str, Any]:
    return {"role": "user", "content": content}


def _assistant(content: str) -> dict[str, Any]:
    return {"role": "assistant", "content": content}


def test_echoes_without_directive():
    assert _run([_user("hi")]) == "You said: hi"


def test_directive_turns_are_consumed_in_order():
    script = '__FAKE__:{"turns": [{"text": "a"}, {"text": "b"}]}'
    history = [_user(script)]
    assert _run(history) == "a"
    history += [_assistant("a"), _user("next")]
    assert _run(history) == "b"
    history += [_assistant("b"), _user("again")]
    assert _run(history) == "You said: again"


def test_tool_calls_are_returned_as_structured_requests():
    script = (
        '__FAKE__:{"turns": [{"text": "Rolling.", "tool_calls": '
        '[{"name": "social_roll", "arguments": {"relationship": 1, "circumstance": 0}}]}]}'
    )
    reply = _chat([_user(script)])
    assert reply.text == "Rolling."
    assert len(reply.tool_requests) == 1
    request = reply.tool_requests[0]
    assert request.name == "social_roll"
    assert json.loads(request.arguments) == {"relationship": 1, "circumstance": 0}


def test_tool_request_ids_are_unique_within_a_turn():
    script = (
        '__FAKE__:{"turns": [{"tool_calls": ['
        '{"name": "social_roll", "arguments": {}}, {"name": "social_roll", "arguments": {}}]}]}'
    )
    ids = [request.id for request in _chat([_user(script)]).tool_requests]
    assert len(set(ids)) == 2


def test_each_model_call_consumes_one_turn_including_tool_rounds():
    script = (
        '__FAKE__:{"turns": [{"tool_calls": [{"name": "social_roll", "arguments": {}}]},'
        ' {"text": "You rolled well."}]}'
    )
    history = [_user(script)]
    first = _chat(history)
    assert first.tool_requests and first.text == ""
    history += [
        {"role": "assistant", "content": ""},
        {"role": "tool", "tool_call_id": first.tool_requests[0].id, "content": "{}"},
    ]
    assert _run(history) == "You rolled well."


def test_directive_may_be_followed_by_text():
    assert _run([_user('__FAKE__:{"turns": [{"text": "ok"}]} thanks')]) == "ok"


def test_latest_directive_replaces_older_one():
    history = [
        _user('__FAKE__:{"turns": [{"text": "old"}]}'),
        _assistant("old"),
        _user('__FAKE__:{"turns": [{"text": "new"}]}'),
    ]
    assert _run(history) == "new"


def test_error_turn_raises_llm_error():
    with pytest.raises(LLMError, match="upstream exploded"):
        _run([_user('__FAKE__:{"turns": [{"error": "upstream exploded"}]}')])


@pytest.mark.parametrize(
    "content",
    [
        "__FAKE__:{turns: []}",
        '__FAKE__:{"turns": "nope"}',
        '__FAKE__:{"turns": [42]}',
        '__FAKE__:{"turns": [{"tool_calls": [{"arguments": {}}]}]}',
    ],
)
def test_malformed_directive_raises_llm_error(content: str):
    with pytest.raises(LLMError):
        _run([_user(content)])


def test_stream_deltas_join_to_the_reply_text():
    script = '__FAKE__:{"turns": [{"text": "Hello  there, friend."}]}'

    async def collect() -> list:
        return [item async for item in FakeProvider().stream("scripted", [_user(script)])]

    items = asyncio.run(collect())

    deltas = "".join(item.text for item in items[:-1])
    assert deltas == "Hello  there, friend."
    assert items[-1].text == "Hello  there, friend."
