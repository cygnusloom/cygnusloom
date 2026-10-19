from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cygnusforge.llm.base import LLMError, complete
from cygnusforge.llm.fake import FakeProvider


def _run(messages: list[dict[str, Any]]) -> str:
    return asyncio.run(complete(FakeProvider(), "scripted", messages))


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


def test_tool_calls_and_outputs_are_rendered():
    script = (
        '__FAKE__:{"turns": [{"text": "Rolling.", "tool_calls": '
        '[{"name": "roll", "arguments": {"sides": 20}, "output": "14"}]}]}'
    )
    reply = _run([_user(script)])
    assert reply == (
        'Rolling.\n<tool_call name="roll">{"sides": 20}</tool_call>\n<tool_output>14</tool_output>'
    )


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
