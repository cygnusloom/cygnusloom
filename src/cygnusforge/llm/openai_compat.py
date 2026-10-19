from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from cygnusforge.llm.base import LLMError

logger = logging.getLogger(__name__)


class OpenAICompatProvider:
    """Adapter for any server that speaks the OpenAI chat completions streaming API, such as Ollama at /v1."""

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

    async def stream_text(self, model: str, messages: list[dict[str, Any]]) -> AsyncIterator[str]:
        body = {"model": model, "messages": messages, "stream": True}
        headers = {"Accept": "text/event-stream"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = self._client.build_request(
            "POST", f"{self._base_url}/chat/completions", json=body, headers=headers
        )
        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            logger.warning("provider %s unreachable: %r", self.name, exc)
            raise LLMError(f"provider {self.name} is unreachable") from exc
        try:
            if response.status_code >= 400:
                logger.warning("provider %s returned HTTP %d", self.name, response.status_code)
                raise LLMError(f"provider {self.name} returned HTTP {response.status_code}")
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError as exc:
                    raise LLMError("malformed stream chunk") from exc
                for choice in chunk.get("choices", []):
                    content = (choice.get("delta") or {}).get("content")
                    if content:
                        yield content
        except httpx.HTTPError as exc:
            raise LLMError(f"provider {self.name} stream failed") from exc
        finally:
            await response.aclose()
