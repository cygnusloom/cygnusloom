"""Python side of the core rule set. Its JSON names these functions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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


def create_region(
    name: str,
    terrain: str,
    points: Sequence[Sequence[float]],
    ctx: ToolContext,
    force: bool = False,
    discovered: bool = False,
) -> dict[str, Any]:
    return ctx.world.create_region(name, terrain, points, force=force, discovered=discovered)


def create_npc(
    name: str,
    location: str,
    description: str,
    ctx: ToolContext,
    personality: str | None = None,
    accent: str | None = None,
    quirks: Sequence[str] | None = None,
    character: Mapping[str, Any] | None = None,
    force: bool = False,
    hp_tier: str | None = None,
    pronouns: str | None = None,
    important: bool = False,
) -> dict[str, Any]:
    return ctx.world.create_npc(
        name=name,
        location=location,
        description=description,
        personality=personality,
        accent=accent,
        quirks=quirks,
        character=character,
        force=force,
        hp_tier=hp_tier,
        pronouns=pronouns,
        important=important,
    )


def update_location(
    name: str,
    description: str,
    ctx: ToolContext,
    x: float | None = None,
    y: float | None = None,
    off_map: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    return ctx.world.update_location(name, description, x=x, y=y, off_map=off_map, force=force)


def discover_location(name: str, ctx: ToolContext, summary: str | None = None) -> dict[str, Any]:
    return ctx.world.discover_location(name, summary)


def delete_location(name: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.delete_location(name)


def change_currency(name: str, amount: int, cause: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.change_currency(name, amount, cause)


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


def update_npc(
    name: str,
    ctx: ToolContext,
    description: str | None = None,
    personality: str | None = None,
    accent: str | None = None,
    quirks: Sequence[str] | None = None,
    character: Mapping[str, Any] | None = None,
    armor: int | None = None,
    attacks: Sequence[Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    return ctx.world.update_npc(
        name=name,
        description=description,
        personality=personality,
        accent=accent,
        quirks=quirks,
        character=character,
        armor=armor,
        attacks=attacks,
    )


def update_npc_knowledge(
    name: str,
    ctx: ToolContext,
    description: str | None = None,
    personality: str | None = None,
    accent: str | None = None,
    quirks: Sequence[str] | None = None,
    pronouns: str | None = None,
    relationship: str | None = None,
    location: str | None = None,
) -> dict[str, Any]:
    return ctx.world.update_npc_knowledge(
        name=name,
        description=description,
        personality=personality,
        accent=accent,
        quirks=quirks,
        pronouns=pronouns,
        relationship=relationship,
        location=location,
    )


def delete_npc(name: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.delete_npc(name)


def move_actor(
    name: str,
    mode: str,
    ctx: ToolContext,
    location: str | None = None,
    travel: str = "",
    route: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return ctx.world.move_actor(name, location, travel or None, mode, route)


def add_note(text: str, ctx: ToolContext, subject: str | None = None) -> dict[str, Any]:
    return ctx.world.add_note(text, subject)


def update_note(note_id: int, text: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.update_note(note_id, text)


def delete_note(note_id: int, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.delete_note(note_id)


def search_notes(query: str, ctx: ToolContext) -> dict[str, list[dict[str, Any]]]:
    return ctx.world.search_notes(query)


def append_journal(text: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.append_journal(text)


def replace_in_journal(old_text: str, new_text: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.replace_in_journal(old_text, new_text)


def search_journal(
    query: str, ctx: ToolContext, context: int = 1
) -> dict[str, list[dict[str, Any]]]:
    return ctx.world.search_journal(query, context)


def view_journal(
    ctx: ToolContext, start_line: int = 1, end_line: int | None = None
) -> dict[str, Any]:
    return ctx.world.view_journal(start_line, end_line)


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


def add_condition(name: str, condition: str, cause: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.add_condition(name, condition, cause)


def end_condition(name: str, condition: str, how: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.end_condition(name, condition, how)


def all_at_location(ctx: ToolContext, location: str | None = None) -> dict[str, Any]:
    return ctx.world.all_at_location(location)


def search_location(query: str, ctx: ToolContext) -> dict[str, list[dict[str, Any]]]:
    return ctx.world.search_location(query)


def search_npc(query: str, ctx: ToolContext) -> dict[str, list[dict[str, Any]]]:
    return ctx.world.search_npc(query)


def get_statblock(name: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.get_statblock(name)


def character_options(ctx: ToolContext, field: str | None = None) -> dict[str, Any]:
    return ctx.world.character_options(field)


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


def lookup_condition(condition: str, ctx: ToolContext) -> dict[str, Any]:
    """Read a condition's effects and the ways it ends."""
    return ctx.world.lookup_condition(condition)


def lookup_feature(feature: str, ctx: ToolContext) -> dict[str, Any]:
    """Read a feature's full text, such as a spell, art, Focus, or class ability."""
    return ctx.world.lookup_feature(feature)


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
