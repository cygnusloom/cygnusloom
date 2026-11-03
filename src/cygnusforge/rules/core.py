"""Python side of the core rule set. Its JSON names these functions."""

from __future__ import annotations

from typing import Any

from cygnusforge.rules.context import ToolContext, brief


async def create_location(
    name: str,
    description: str,
    ctx: ToolContext,
    discovered: bool = False,
    x: float | None = None,
    y: float | None = None,
    force: bool = False,
    parent: str | None = None,
) -> dict[str, Any]:
    if ctx.check_new_location is not None:
        await ctx.check_new_location(name)
    return ctx.world.create_location(
        name, description, discovered=discovered, x=x, y=y, force=force, parent=parent
    )


def roll_creative(ctx: ToolContext, tables: list[str] | None = None) -> dict[str, Any]:
    return ctx.world.roll_creative(tables)


def list_projects(ctx: ToolContext) -> dict[str, Any]:
    return {"projects": ctx.world.list_projects()}


def lookup_rule(rule: str, ctx: ToolContext) -> dict[str, str]:
    text = ctx.rules.get(rule)
    if text is None:
        return {"error": f"no rule {rule!r}; the rules are {', '.join(ctx.rules)}"}
    return {"rule": rule, "text": text}


def list_lookups(ctx: ToolContext) -> dict[str, Any]:
    index = ctx.world.lookup_index()
    return {
        "tools": [{"tool": name, "brief": text} for name, text in ctx.lookup_tools.items()],
        "rules": [{"rule": name, "brief": brief(text)} for name, text in ctx.rules.items()],
        "locations": index["locations"],
        "npcs": index["npcs"],
    }


async def speak(
    npc: str, intent: str, ctx: ToolContext, replace: bool = False, force: bool = False
) -> dict[str, str]:
    profile = ctx.world.speaker_profile(npc)
    if not force and not ctx.world.speaker_at_player(npc):
        return {
            "speaker": npc,
            "status": "dialogue not queued",
            "warning": (
                f"{npc} is not at the player character's location, so nothing was written. "
                "If they are speaking from elsewhere, such as over a wall or by message, call "
                "speak again with force true; the narration will show where they are speaking from."
            ),
        }
    await ctx.speak(npc, profile, intent, replace)
    return {"speaker": npc, "status": "dialogue queued for the narration"}


def ooc(ctx: ToolContext, no_narration: bool = False) -> dict[str, str]:
    ctx.ooc(not no_narration)
    if no_narration:
        status = (
            "your next reply goes to the player directly, and the story is not narrated after it"
        )
    else:
        status = "your next reply goes to the player directly, and the story is narrated after it"
    return {"status": status}
