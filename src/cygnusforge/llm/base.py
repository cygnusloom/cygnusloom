from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol


class LLMError(Exception):
    """A provider failure. The turn that hit it is not saved, so the user can retry."""


class Provider(Protocol):
    name: str

    def stream_text(self, model: str, messages: list[dict[str, Any]]) -> AsyncIterator[str]: ...


async def complete(provider: Provider, model: str, messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    async for text in provider.stream_text(model, messages):
        parts.append(text)
    return "".join(parts).strip()
