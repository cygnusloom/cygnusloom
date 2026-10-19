"""Python side of the core rule set. Its JSON names these functions."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from cygnusforge.rules.context import ToolContext


def create_location(name: str, description: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.create_location(name, description)


def create_npc(
    name: str,
    location: str,
    description: str,
    ctx: ToolContext,
    personality: str | None = None,
    accent: str | None = None,
    quirks: Sequence[str] | None = None,
) -> dict[str, Any]:
    return ctx.world.create_npc(
        name=name,
        location=location,
        description=description,
        personality=personality,
        accent=accent,
        quirks=quirks,
    )


def move_actor(name: str, location: str, ctx: ToolContext) -> dict[str, str]:
    return ctx.world.move_actor(name, location)


def add_note(text: str, ctx: ToolContext, subject: str | None = None) -> dict[str, Any]:
    return ctx.world.add_note(text, subject)


def search_notes(query: str, ctx: ToolContext) -> dict[str, list[dict[str, str]]]:
    return ctx.world.search_notes(query)


async def speak(npc: str, intent: str, ctx: ToolContext) -> dict[str, str]:
    profile = ctx.world.speaker_profile(npc)
    await ctx.speak(npc, profile, intent)
    return {"speaker": npc, "status": "dialogue queued for the narration"}
