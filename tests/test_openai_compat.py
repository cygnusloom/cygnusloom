import asyncio
import json

import httpx
import pytest

from cygnusforge.llm.base import ChatReply, LLMError, ToolRequest, complete
from cygnusforge.llm.openai_compat import OpenAICompatProvider
from cygnusforge.services.campaigns import _tool_call_message

MESSAGES = [{"role": "user", "content": "hi"}]
TEST_KEY = "sk-test"
TOOLS = [{"type": "function", "function": {"name": "social_roll", "parameters": {}}}]


def _provider(handler) -> OpenAICompatProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAICompatProvider("testprov", "https://example.test/v1", TEST_KEY, client=client)


def _json_reply(message: dict) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": message}]})


def test_sends_bearer_key_and_returns_text():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["Authorization"]
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return _json_reply({"role": "assistant", "content": "Hello there."})

    reply = asyncio.run(complete(_provider(handler), "deepseek-flash", MESSAGES))

    assert reply == "Hello there."
    assert seen["auth"] == f"Bearer {TEST_KEY}"
    assert seen["url"] == "https://example.test/v1/chat/completions"
    assert seen["body"] == {"model": "deepseek-flash", "messages": MESSAGES}


def test_tools_are_sent_and_tool_requests_are_parsed():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _json_reply(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "social_roll", "arguments": '{"relationship": 1}'},
                    }
                ],
            }
        )

    reply = asyncio.run(_provider(handler).chat("deepseek-flash", MESSAGES, TOOLS))

    assert seen["body"]["tools"] == TOOLS
    assert reply.text == ""
    assert len(reply.tool_requests) == 1
    request = reply.tool_requests[0]
    assert (request.id, request.name, request.arguments) == (
        "call_1",
        "social_roll",
        '{"relationship": 1}',
    )


def test_tools_key_is_omitted_when_no_tools_are_given():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _json_reply({"content": "ok"})

    asyncio.run(_provider(handler).chat("m", MESSAGES, []))

    assert "tools" not in seen["body"]


def test_http_error_becomes_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    with pytest.raises(LLMError, match="HTTP 401"):
        asyncio.run(complete(_provider(handler), "deepseek-flash", MESSAGES))


def test_unreachable_provider_becomes_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(LLMError, match="unreachable"):
        asyncio.run(complete(_provider(handler), "deepseek-flash", MESSAGES))


def test_malformed_json_becomes_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>oops</html>")

    with pytest.raises(LLMError, match="malformed"):
        asyncio.run(complete(_provider(handler), "deepseek-flash", MESSAGES))


def test_reply_without_message_becomes_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    with pytest.raises(LLMError, match="without a message"):
        asyncio.run(complete(_provider(handler), "deepseek-flash", MESSAGES))


def _events(*payloads: str, done: bool = True) -> httpx.Response:
    lines = [f"data: {payload}\n\n" for payload in payloads]
    if done:
        lines.append("data: [DONE]\n\n")
    return httpx.Response(
        200, content="".join(lines).encode(), headers={"Content-Type": "text/event-stream"}
    )


def _chunk(delta: dict) -> str:
    return json.dumps({"choices": [{"delta": delta}]})


async def _collect(provider: OpenAICompatProvider, tools=None) -> list:
    return [item async for item in provider.stream("deepseek-flash", MESSAGES, tools)]


def test_stream_yields_text_deltas_then_the_full_reply():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _events(_chunk({"content": "Hel"}), _chunk({"content": "lo."}))

    items = asyncio.run(_collect(_provider(handler)))

    assert seen["body"]["stream"] is True
    assert [item.text for item in items[:-1]] == ["Hel", "lo."]
    assert items[-1].text == "Hello."
    assert items[-1].tool_requests == ()


def test_stream_merges_tool_call_fragments_by_index():
    def handler(request: httpx.Request) -> httpx.Response:
        return _events(
            _chunk(
                {
                    "content": None,
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "function": {"name": "social_roll", "arguments": ""},
                        }
                    ],
                }
            ),
            _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"relation'}}]}),
            _chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'ship": 1}'}}]}),
        )

    items = asyncio.run(_collect(_provider(handler), TOOLS))

    assert items[-1].text == ""
    assert items[-1].tool_requests == (
        ToolRequest(id="call_1", name="social_roll", arguments='{"relationship": 1}'),
    )


def test_stream_http_error_raises_before_anything_is_yielded():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="busy")

    with pytest.raises(LLMError, match="HTTP 503"):
        asyncio.run(_collect(_provider(handler)))


def test_stream_that_ends_without_done_is_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return _events(_chunk({"content": "cut off"}), done=False)

    with pytest.raises(LLMError, match="ended its reply early"):
        asyncio.run(_collect(_provider(handler)))


def test_stream_with_a_malformed_event_is_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return _events("{not json", _chunk({"content": "x"}))

    with pytest.raises(LLMError, match="malformed stream event"):
        asyncio.run(_collect(_provider(handler)))


def test_stream_reports_an_error_event_from_the_provider():
    def handler(request: httpx.Request) -> httpx.Response:
        return _events(json.dumps({"error": {"message": "quota"}}))

    with pytest.raises(LLMError, match="reported an error"):
        asyncio.run(_collect(_provider(handler)))


def test_reasoning_content_is_returned_with_tool_requests():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_reply(
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "Check the roll first.",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "social_roll", "arguments": "{}"},
                    }
                ],
            }
        )

    reply = asyncio.run(_provider(handler).chat("deepseek-flash", MESSAGES, TOOLS))

    assert reply.reasoning == "Check the roll first."


def test_stream_collects_reasoning_content_before_the_tool_call():
    def handler(request: httpx.Request) -> httpx.Response:
        return _events(
            _chunk({"reasoning_content": "Check "}),
            _chunk({"reasoning_content": "the roll."}),
            _chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "function": {"name": "social_roll", "arguments": "{}"},
                        }
                    ]
                }
            ),
        )

    items = asyncio.run(_collect(_provider(handler), TOOLS))

    assert items[-1].reasoning == "Check the roll."
    assert items[-1].tool_requests == (
        ToolRequest(id="call_1", name="social_roll", arguments="{}"),
    )


def test_reasoning_is_passed_back_on_the_tool_call_message():
    seen = {}
    step = ChatReply(
        text="",
        tool_requests=(ToolRequest(id="call_1", name="social_roll", arguments="{}"),),
        reasoning="Check the roll first.",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _json_reply({"role": "assistant", "content": "Done."})

    messages = [*MESSAGES, _tool_call_message(step)]
    asyncio.run(_provider(handler).chat("deepseek-flash", messages, TOOLS))

    assert seen["body"]["messages"][-1]["reasoning_content"] == "Check the roll first."


def test_tool_call_message_without_reasoning_sends_an_empty_reasoning_key():
    step = ChatReply(
        text="", tool_requests=(ToolRequest(id="call_1", name="social_roll", arguments="{}"),)
    )

    assert _tool_call_message(step)["reasoning_content"] == ""
