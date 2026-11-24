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


def test_the_repeat_match_is_case_insensitive_and_returns_the_stored_name():
    provider = ScriptedReply("repeat: farming path")
    assert _check(provider, "Farm Road") == "Farming Path"


def test_a_new_reply_allows_the_name():
    assert _check(ScriptedReply("NEW"), "Farm Dell") is None


def test_an_unparsable_reply_allows_the_name():
    assert _check(ScriptedReply("I think they are similar."), "Farm Dell") is None


def test_a_repeat_naming_a_place_not_in_the_list_allows_the_name():
    assert _check(ScriptedReply("REPEAT: Nowhere"), "Farm Dell") is None


def test_no_existing_places_skips_the_model_call():
    provider = ScriptedReply("REPEAT: Farming Path")
    assert _check(provider, "Farm Road", names=[]) is None
    assert provider.calls == []


def test_the_model_sees_the_existing_names_and_the_new_one():
    provider = ScriptedReply("NEW")
    _check(provider, "Farm Track")
    listing = provider.calls[0][1]["content"]
    assert "- Farming Path" in listing
    assert "New place: Farm Track" in listing


def test_an_obviously_new_name_skips_the_model_call():
    provider = ScriptedReply("REPEAT: Farming Path")
    assert _check(provider, "Bone Sump") is None
    assert provider.calls == []


def test_a_name_that_differs_only_by_an_article_needs_no_model_call():
    provider = ScriptedReply("REPEAT: Farming Path")
    assert _check(provider, "The Farming Path") == "Farming Path"
    assert provider.calls == []


def test_only_close_names_are_shown_to_the_model():
    provider = ScriptedReply("NEW")
    _check(provider, "Farm Track")
    listing = provider.calls[0][1]["content"]
    assert "The Mossbell Hollow" not in listing
