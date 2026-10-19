from __future__ import annotations

import logging
from typing import Any

import httpx

from cygnusforge.llm.base import ChatReply, LLMError, ToolRequest

logger = logging.getLogger(__name__)


class OpenAICompatProvider:
    """Adapter for any server that speaks the OpenAI chat completions API, such as Ollama at /v1."""

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=5.0))

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatReply:
        body: dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            body["tools"] = tools
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = self._client.build_request(
            "POST", f"{self._base_url}/chat/completions", json=body, headers=headers
        )
        try:
            response = await self._client.send(request)
        except httpx.HTTPError as exc:
            logger.warning("provider %s unreachable: %r", self.name, exc)
            raise LLMError(f"provider {self.name} is unreachable") from exc
        try:
            if response.status_code >= 400:
                logger.warning("provider %s returned HTTP %d", self.name, response.status_code)
                raise LLMError(f"provider {self.name} returned HTTP {response.status_code}")
            data = response.json()
        except ValueError as exc:
            raise LLMError(f"provider {self.name} sent a malformed reply") from exc
        finally:
            await response.aclose()
        return _parse_reply(self.name, data)


def _parse_reply(name: str, data: Any) -> ChatReply:
    try:
        message = data["choices"][0]["message"]
        text = message.get("content") or ""
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
    return ChatReply(text=text, tool_requests=requests)
