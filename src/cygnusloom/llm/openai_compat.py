from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

from cygnusloom.llm.base import ChatReply, LLMError, TextDelta, TokenUsage, ToolRequest

logger = logging.getLogger(__name__)


class OpenAICompatProvider:
    """Adapter for any server that speaks the OpenAI chat completions API, such as Ollama at /v1."""

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        stream_usage: bool = True,
    ):
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=30.0))
        # Ask for usage in the final stream chunk. Some servers reject the OpenAI stream_options
        # field, so the admin can switch this off per model; a rejection is also retried once
        # without it.
        self._stream_usage = stream_usage

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatReply:
        request = self._request(_body(model, messages, tools), "application/json")
        try:
            response = await self._client.send(request)
        except httpx.HTTPError as exc:
            logger.warning("provider %s unreachable: %r", self.name, exc)
            raise LLMError(f"provider {self.name} is unreachable") from exc
        try:
            if response.status_code >= 400:
                logger.warning(
                    "provider %s returned HTTP %d: %s",
                    self.name,
                    response.status_code,
                    (await response.aread())[:500].decode(errors="replace"),
                )
                raise LLMError(f"provider {self.name} returned HTTP {response.status_code}")
            data = response.json()
        except ValueError as exc:
            raise LLMError(f"provider {self.name} sent a malformed reply") from exc
        finally:
            await response.aclose()
        return _parse_reply(self.name, data)

    async def stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[TextDelta | ChatReply]:
        """Server-sent events. Text is yielded as it arrives; the reply is yielded after [DONE].

        With stream usage enabled, the final chunk carries exact token counts when the server
        supports OpenAI's `stream_options`. A server that rejects that field is retried once
        without it, so streaming still works and usage falls back to estimation."""
        request = self._request(
            _body(model, messages, tools, stream=True, include_usage=self._stream_usage),
            "text/event-stream",
        )
        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            logger.warning("provider %s unreachable: %r", self.name, exc)
            raise LLMError(f"provider {self.name} is unreachable") from exc
        if self._stream_usage and response.status_code in (400, 422):
            detail = (await response.aread())[:500].decode(errors="replace")
            await response.aclose()
            if not _rejects_stream_usage(detail):
                logger.warning(
                    "provider %s returned HTTP %d: %s",
                    self.name,
                    response.status_code,
                    detail,
                )
                raise LLMError(f"provider {self.name} returned HTTP {response.status_code}")
            logger.info("provider %s rejected stream usage; retrying without it", self.name)
            request = self._request(_body(model, messages, tools, stream=True), "text/event-stream")
            try:
                response = await self._client.send(request, stream=True)
            except httpx.HTTPError as exc:
                logger.warning("provider %s unreachable: %r", self.name, exc)
                raise LLMError(f"provider {self.name} is unreachable") from exc
        try:
            if response.status_code >= 400:
                logger.warning(
                    "provider %s returned HTTP %d: %s",
                    self.name,
                    response.status_code,
                    (await response.aread())[:500].decode(errors="replace"),
                )
                raise LLMError(f"provider {self.name} returned HTTP {response.status_code}")
            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            calls: dict[int, dict[str, str]] = {}
            usage: TokenUsage | None = None
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if payload == "[DONE]":
                    yield ChatReply(
                        text="".join(text_parts),
                        tool_requests=_calls(self.name, calls),
                        reasoning="".join(reasoning_parts),
                        usage=usage,
                    )
                    return
                chunk = _stream_chunk(self.name, payload)
                usage = _parse_usage(chunk.get("usage")) or usage
                choices = chunk.get("choices") or []
                delta = (choices[0].get("delta") or {}) if choices else {}
                reasoning = delta.get("reasoning_content") or ""
                if reasoning:
                    reasoning_parts.append(str(reasoning))
                text = delta.get("content") or ""
                if text:
                    text_parts.append(text)
                    yield TextDelta(text)
                for fragment in delta.get("tool_calls") or []:
                    _merge_call(calls, fragment)
            raise LLMError(f"provider {self.name} ended its reply early")
        except httpx.HTTPError as exc:
            logger.warning("provider %s dropped the stream: %r", self.name, exc)
            raise LLMError(f"provider {self.name} dropped the connection") from exc
        finally:
            await response.aclose()

    def _request(self, body: dict[str, Any], accept: str) -> httpx.Request:
        headers = {"Accept": accept}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return self._client.build_request(
            "POST", f"{self._base_url}/chat/completions", json=body, headers=headers
        )


def _body(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    stream: bool = False,
    include_usage: bool = False,
) -> dict[str, Any]:
    body: dict[str, Any] = {"model": model, "messages": messages}
    if tools:
        body["tools"] = tools
    if stream:
        body["stream"] = True
        if include_usage:
            body["stream_options"] = {"include_usage": True}
    return body


def _parse_reply(name: str, data: Any) -> ChatReply:
    try:
        message = data["choices"][0]["message"]
        text = message.get("content") or ""
        reasoning = str(message.get("reasoning_content") or "")
        requests = tuple(
            ToolRequest(
                id=str(call["id"]),
                name=str(call["function"]["name"]),
                arguments=str(call["function"].get("arguments") or "{}"),
            )
            for call in message.get("tool_calls") or []
        )
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise LLMError(f"provider {name} sent a reply without a message") from exc
    return ChatReply(
        text=text,
        tool_requests=requests,
        reasoning=reasoning,
        usage=_parse_usage(data.get("usage")),
    )


def _parse_usage(raw: Any) -> TokenUsage | None:
    """Token counts from either OpenAI's `usage` object or DeepSeek's cache fields."""
    if not isinstance(raw, Mapping):
        return None
    prompt_details = raw.get("prompt_tokens_details")
    cached = 0
    if isinstance(prompt_details, Mapping):
        cached = _token_count(prompt_details.get("cached_tokens"))
    if not cached:
        cached = _token_count(raw.get("prompt_cache_hit_tokens"))
    completion_details = raw.get("completion_tokens_details")
    reasoning = 0
    if isinstance(completion_details, Mapping):
        reasoning = _token_count(completion_details.get("reasoning_tokens"))
    return TokenUsage(
        input_tokens=_token_count(raw.get("prompt_tokens")),
        output_tokens=_token_count(raw.get("completion_tokens")),
        cached_tokens=cached,
        reasoning_tokens=reasoning,
    )


def _token_count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float) and value.is_integer():
        return max(0, int(value))
    return 0


def _stream_chunk(name: str, payload: str) -> dict[str, Any]:
    try:
        chunk = json.loads(payload)
        if "error" in chunk:
            raise LLMError(f"provider {name} reported an error: {chunk['error']}")
        return chunk if isinstance(chunk, dict) else {}
    except (ValueError, TypeError, AttributeError) as exc:
        raise LLMError(f"provider {name} sent a malformed stream event") from exc


def _rejects_stream_usage(detail: str) -> bool:
    text = detail.casefold()
    return ("stream_options" in text or "include_usage" in text) and any(
        word in text
        for word in (
            "unknown",
            "unrecognized",
            "unexpected",
            "unsupported",
            "not supported",
            "not allowed",
            "invalid",
            "extra",
        )
    )


def _merge_call(calls: dict[int, dict[str, str]], fragment: dict[str, Any]) -> None:
    """Tool calls arrive in pieces: the id and name once, then the arguments a few characters at
    a time. Each piece is matched to its call by index."""
    entry = calls.setdefault(int(fragment.get("index", 0)), {"id": "", "name": "", "arguments": ""})
    if fragment.get("id"):
        entry["id"] = str(fragment["id"])
    function = fragment.get("function") or {}
    if function.get("name"):
        entry["name"] = str(function["name"])
    entry["arguments"] += str(function.get("arguments") or "")


def _calls(name: str, calls: dict[int, dict[str, str]]) -> tuple[ToolRequest, ...]:
    requests = []
    for index in sorted(calls):
        entry = calls[index]
        if not entry["name"]:
            raise LLMError(f"provider {name} sent a tool call without a name")
        requests.append(
            ToolRequest(
                id=entry["id"] or f"call_{index}",
                name=entry["name"],
                arguments=entry["arguments"] or "{}",
            )
        )
    return tuple(requests)
