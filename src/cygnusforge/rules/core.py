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


def equip_armor(name: str, item: str, ctx: ToolContext, equipped: bool = True) -> dict[str, Any]:
    return ctx.world.equip_armor(name, item, equipped)


def place_object(
    place: str,
    contents: str,
    hidden: bool,
    reason: str,
    ctx: ToolContext,
    currency: int | None = None,
    item: str | None = None,
    item_size: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    return ctx.world.place_object(
        place, contents, hidden, reason, currency, item, item_size or "", name or ""
    )


def take_object(object: str, character: str, reason: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.take_object(object, character, reason)


def lookup_move(move: str, ctx: ToolContext) -> dict[str, Any]:
    """Read a move's trigger, stat, and outcome bands before rolling it."""
    return ctx.world.lookup_move(move)


def roll_move(
    move: str, character: str, ctx: ToolContext, modifier: int = 0, stat: str | None = None
) -> dict[str, Any]:
    return ctx.world.roll_move(move, character, modifier, stat)


def adjust_counter(character: str, counter: str, change: int, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.adjust_counter(character, counter, change)


def roll_creative(ctx: ToolContext, tables: list[str] | None = None) -> dict[str, Any]:
    return ctx.world.roll_creative(tables)


def take_advancement(
    character: str,
    option: str,
    ctx: ToolContext,
    stat: str | None = None,
    feature: str | None = None,
) -> dict[str, Any]:
    return ctx.world.take_advancement(character, option, stat, feature)


def end_period(period: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.end_period(period)


def adjust_tally(
    tally: str,
    entry: str,
    change: int,
    ctx: ToolContext,
    reason: str = "",
    award: str = "",
    force: bool = False,
) -> dict[str, Any]:
    return ctx.world.adjust_tally(tally, entry, change, reason, award, force)


def set_relation(
    character: str, relation: str, other: str, added: bool, ctx: ToolContext
) -> dict[str, Any]:
    return ctx.world.set_relation(character, relation, other, added)


def start_project(
    name: str,
    kind: str,
    goal: str,
    target: int,
    ctx: ToolContext,
    force: bool = False,
) -> dict[str, Any]:
    """Begin a long-running effort and track its points toward a target."""
    return ctx.world.start_project(name, kind, goal, target, force)


def advance_project(name: str, amount: int, ctx: ToolContext, note: str = "") -> dict[str, Any]:
    """Add progress to a project, research effort, magic working, or faction turn."""
    return ctx.world.advance_project(name, amount, note)


def abandon_project(name: str, ctx: ToolContext, reason: str = "") -> dict[str, Any]:
    return ctx.world.abandon_project(name, reason)


def list_projects(ctx: ToolContext) -> dict[str, Any]:
    return {"projects": ctx.world.list_projects()}


def heal_character(
    name: str,
    amount: int,
    ctx: ToolContext,
    stat: str | None = None,
    cause: str = "",
) -> dict[str, Any]:
    """Restore lost points of a stat, such as hit points from a healing spell or art."""
    return ctx.world.heal_character(name, amount, stat, cause)


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
