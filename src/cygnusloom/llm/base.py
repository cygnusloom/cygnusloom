from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
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
    # Thinking-mode models must get this back on the tool-call message that produced it.
    reasoning: str = ""


@dataclass(frozen=True)
class TextDelta:
    """A piece of reply text as it arrives. The joined deltas equal the final reply's text."""

    text: str


class Provider(Protocol):
    name: str

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatReply: ...

    def stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[TextDelta | ChatReply]:
        """Yields text deltas as they arrive, then the complete ChatReply as the last item."""
        ...


async def complete(provider: Provider, model: str, messages: list[dict[str, Any]]) -> str:
    """A plain text reply with no tools, such as a summary."""
    return (await provider.chat(model, messages)).text.strip()
