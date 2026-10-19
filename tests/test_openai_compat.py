import asyncio
import json

import httpx
import pytest

from cygnusforge.llm.base import LLMError, complete
from cygnusforge.llm.openai_compat import OpenAICompatProvider

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
