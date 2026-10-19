"""Python side of the core rule set. Its JSON names these functions."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from cygnusforge.rules.context import ToolContext
from cygnusforge.services import world
from cygnusforge.services.common import session_scope


def create_location(name: str, description: str, ctx: ToolContext) -> dict[str, Any]:
    with session_scope(ctx.engine) as db:
        return world.create_location(db, ctx.campaign_id, name, description)


def create_npc(
    name: str,
    location: str,
    description: str,
    ctx: ToolContext,
    personality: str | None = None,
    accent: str | None = None,
    quirks: Sequence[str] | None = None,
) -> dict[str, Any]:
    with session_scope(ctx.engine) as db:
        return world.create_npc(
            db,
            ctx.campaign_id,
            ctx.tables,
            ctx.rng,
            name=name,
            location=location,
            description=description,
            personality=personality,
            accent=accent,
            quirks=quirks,
        )


def move_actor(name: str, location: str, ctx: ToolContext) -> dict[str, str]:
    with session_scope(ctx.engine) as db:
        return world.move_actor(db, ctx.campaign_id, name, location)


def add_note(text: str, ctx: ToolContext, subject: str | None = None) -> dict[str, str]:
    with session_scope(ctx.engine) as db:
        return world.add_note(db, ctx.campaign_id, text, subject)


def search_notes(query: str, ctx: ToolContext) -> dict[str, list[dict[str, str]]]:
    with session_scope(ctx.engine) as db:
        return world.search_notes(db, ctx.campaign_id, query)


async def speak(npc: str, intent: str, ctx: ToolContext) -> dict[str, str]:
    with session_scope(ctx.engine) as db:
        profile = world.speaker_profile(db, ctx.campaign_id, npc)
    dialogue = await ctx.speak(profile, intent)
    return {"speaker": npc, "dialogue": dialogue}
