from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from cygnusforge.llm.base import ChatReply, LLMError
from cygnusforge.llm.fake import FakeProvider


def _decide(messages: list[dict[str, Any]]) -> ChatReply:
    return asyncio.run(FakeProvider().chat("scripted", messages))


def _narrate(messages: list[dict[str, Any]]) -> str:
    return _stream(messages)[-1].text


def _stream(messages: list[dict[str, Any]]) -> list:
    async def collect() -> list:
        return [item async for item in FakeProvider().stream("scripted", messages)]

    return asyncio.run(collect())


def _user(content: str) -> dict[str, Any]:
    return {"role": "user", "content": content}


def _assistant(content: str) -> dict[str, Any]:
    return {"role": "assistant", "content": content}


ROLL = {"name": "social_roll", "arguments": {"relationship": 1, "circumstance": 0}}


def test_echoes_without_directive():
    assert _decide([_user("hi")]).text == ""
    assert _narrate([_user("hi")]) == "You said: hi"


def test_each_user_message_uses_the_next_turn():
    script = '__FAKE__:{"turns": [{"text": "a"}, {"text": "b"}]}'
    history = [_user(script)]
    assert _narrate(history) == "a"
    history += [_assistant("a"), _user("next")]
    assert _narrate(history) == "b"
    history += [_assistant("b"), _user("again")]
    assert _narrate(history) == "You said: again"


def test_rounds_are_returned_as_structured_requests():
    script = "__FAKE__:" + json.dumps({"turns": [{"rounds": [[ROLL]], "text": "Rolling."}]})
    reply = _decide([_user(script)])
    assert reply.text == ""
    assert len(reply.tool_requests) == 1
    request = reply.tool_requests[0]
    assert request.name == "social_roll"
    assert json.loads(request.arguments) == {"relationship": 1, "circumstance": 0}
    assert _narrate([_user(script)]) == "Rolling."


def test_tool_request_ids_are_unique_within_a_round():
    script = "__FAKE__:" + json.dumps({"turns": [{"rounds": [[ROLL, ROLL]]}]})
    ids = [request.id for request in _decide([_user(script)]).tool_requests]
    assert len(set(ids)) == 2


def test_each_decision_call_takes_the_next_round_then_stops():
    script = "__FAKE__:" + json.dumps({"turns": [{"rounds": [[ROLL], [ROLL]], "text": "Done."}]})
    history = [_user(script)]
    first = _decide(history)
    assert first.tool_requests
    history += [
        {"role": "assistant", "content": None, "tool_calls": [{"id": first.tool_requests[0].id}]}
    ]
    history += [{"role": "tool", "tool_call_id": first.tool_requests[0].id, "content": "{}"}]
    second = _decide(history)
    assert second.tool_requests
    history += [
        {"role": "assistant", "content": None, "tool_calls": [{"id": second.tool_requests[0].id}]}
    ]
    history += [{"role": "tool", "tool_call_id": second.tool_requests[0].id, "content": "{}"}]
    third = _decide(history)
    assert third.tool_requests == () and third.text == ""
    assert _narrate(history) == "Done."


def test_directive_may_be_followed_by_text():
    assert _narrate([_user('__FAKE__:{"turns": [{"text": "ok"}]} thanks')]) == "ok"


def test_latest_directive_replaces_older_one():
    history = [
        _user('__FAKE__:{"turns": [{"text": "old"}]}'),
        _assistant("old"),
        _user('__FAKE__:{"turns": [{"text": "new"}]}'),
    ]
    assert _narrate(history) == "new"


def test_error_turn_raises_llm_error():
    with pytest.raises(LLMError, match="upstream exploded"):
        _decide([_user('__FAKE__:{"turns": [{"error": "upstream exploded"}]}')])
    with pytest.raises(LLMError, match="upstream exploded"):
        _stream([_user('__FAKE__:{"turns": [{"error": "upstream exploded"}]}')])


@pytest.mark.parametrize(
    "content",
    [
        "__FAKE__:{turns: []}",
        '__FAKE__:{"turns": "nope"}',
        '__FAKE__:{"turns": [42]}',
        '__FAKE__:{"turns": [{"rounds": "nope"}]}',
        '__FAKE__:{"turns": [{"rounds": [[{"arguments": {}}]]}]}',
    ],
)
def test_malformed_directive_raises_llm_error(content: str):
    with pytest.raises(LLMError):
        _decide([_user(content)])


def test_stream_deltas_join_to_the_reply_text():
    script = '__FAKE__:{"turns": [{"text": "Hello  there, friend."}]}'
    items = _stream([_user(script)])

    deltas = "".join(item.text for item in items[:-1])
    assert deltas == "Hello  there, friend."
    assert items[-1].text == "Hello  there, friend."
