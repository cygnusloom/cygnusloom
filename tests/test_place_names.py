from __future__ import annotations

import asyncio
from typing import Any

from cygnusloom.llm.base import ChatReply
from cygnusloom.services.campaigns import _repeated_place

NAMES = ["Farming Path", "The Mossbell Hollow"]


class ScriptedReply:
    """Answers the name check with a fixed reply and records what it was asked."""

    name = "scripted"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[list[dict[str, Any]]] = []

    async def chat(self, model: str, messages: list[dict[str, Any]], tools=None) -> ChatReply:
        self.calls.append(messages)
        return ChatReply(text=self.reply)


def _check(provider: ScriptedReply, name: str, names: list[str] = NAMES) -> str | None:
    return asyncio.run(_repeated_place(provider, "model", names, name))


def test_a_repeat_reply_returns_the_existing_place_it_names():
    provider = ScriptedReply("REPEAT: Farming Path")
    assert _check(provider, "Farm Road") == "Farming Path"


def test_the_repeat_match_is_case_insensitive_and_returns_the_stored_name():
    provider = ScriptedReply("repeat: farming path")
    assert _check(provider, "Farm Road") == "Farming Path"


def test_a_new_reply_allows_the_name():
    assert _check(ScriptedReply("NEW"), "Bone Sump") is None


def test_an_unparsable_reply_allows_the_name():
    assert _check(ScriptedReply("I think they are similar."), "Bone Sump") is None


def test_a_repeat_naming_a_place_not_in_the_list_allows_the_name():
    assert _check(ScriptedReply("REPEAT: Nowhere"), "Bone Sump") is None


def test_no_existing_places_skips_the_model_call():
    provider = ScriptedReply("REPEAT: Farming Path")
    assert _check(provider, "Farm Road", names=[]) is None
    assert provider.calls == []


def test_the_model_sees_the_existing_names_and_the_new_one():
    provider = ScriptedReply("NEW")
    _check(provider, "Bone Sump")
    listing = provider.calls[0][1]["content"]
    assert "- Farming Path" in listing
    assert "- The Mossbell Hollow" in listing
    assert "New place: Bone Sump" in listing
