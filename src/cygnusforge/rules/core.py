"""Python side of the core rule set. Its JSON names these functions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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
    character: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return ctx.world.create_npc(
        name=name,
        location=location,
        description=description,
        personality=personality,
        accent=accent,
        quirks=quirks,
        character=character,
    )


def update_location(name: str, description: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.update_location(name, description)


def delete_location(name: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.delete_location(name)


def update_npc(
    name: str,
    ctx: ToolContext,
    description: str | None = None,
    personality: str | None = None,
    accent: str | None = None,
    quirks: Sequence[str] | None = None,
) -> dict[str, Any]:
    return ctx.world.update_npc(
        name=name,
        description=description,
        personality=personality,
        accent=accent,
        quirks=quirks,
    )


def delete_npc(name: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.delete_npc(name)


def move_actor(name: str, location: str, ctx: ToolContext) -> dict[str, str]:
    return ctx.world.move_actor(name, location)


def add_note(text: str, ctx: ToolContext, subject: str | None = None) -> dict[str, Any]:
    return ctx.world.add_note(text, subject)


def update_note(note_id: int, text: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.update_note(note_id, text)


def delete_note(note_id: int, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.delete_note(note_id)


def search_notes(query: str, ctx: ToolContext) -> dict[str, list[dict[str, Any]]]:
    return ctx.world.search_notes(query)


async def speak(npc: str, intent: str, ctx: ToolContext, replace: bool = False) -> dict[str, str]:
    profile = ctx.world.speaker_profile(npc)
    await ctx.speak(npc, profile, intent, replace)
    return {"speaker": npc, "status": "dialogue queued for the narration"}
