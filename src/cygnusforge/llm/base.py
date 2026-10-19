from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol


class LLMError(Exception):
    """A provider failure. The turn that hit it is not saved, so the user can retry."""


@dataclass(frozen=True)
class ToolRequest:
    """A tool the model asked to run. `arguments` is the raw JSON text the model wrote."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ChatReply:
    text: str
    tool_requests: Sequence[ToolRequest] = field(default_factory=tuple)


class Provider(Protocol):
    name: str

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatReply: ...


async def complete(provider: Provider, model: str, messages: list[dict[str, Any]]) -> str:
    """A plain text reply with no tools, such as a summary."""
    return (await provider.chat(model, messages)).text.strip()
