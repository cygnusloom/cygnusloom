import asyncio
import json

import httpx
import pytest

from cygnusforge.llm.base import LLMError, complete
from cygnusforge.llm.openai_compat import OpenAICompatProvider

MESSAGES = [{"role": "user", "content": "hi"}]


def _provider(handler) -> OpenAICompatProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAICompatProvider("testprov", "https://example.test/v1", "sk-test", client=client)


def _sse(*chunks: dict) -> bytes:
    lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    return ("".join(lines) + "data: [DONE]\n\n").encode()


def test_sends_bearer_key_and_joins_streamed_content():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["Authorization"]
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                {"choices": [{"delta": {"reasoning_content": "thinking"}}]},
                {"choices": [{"delta": {"content": "Hello "}}]},
                {"choices": [{"delta": {"content": "there."}}]},
            ),
        )

    reply = asyncio.run(complete(_provider(handler), "deepseek-flash", MESSAGES))

    assert reply == "Hello there."
    assert seen["auth"] == "Bearer sk-test"
    assert seen["url"] == "https://example.test/v1/chat/completions"
    assert seen["body"]["model"] == "deepseek-flash"
    assert seen["body"]["stream"] is True


def test_http_error_becomes_llm_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    with pytest.raises(LLMError, match="HTTP 401"):
        asyncio.run(complete(_provider(handler), "deepseek-flash", MESSAGES))
