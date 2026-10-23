"""Python side of the core rule set. Its JSON names these functions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from cygnusforge.rules.context import ToolContext, brief


def create_location(
    name: str,
    description: str,
    ctx: ToolContext,
    discovered: bool = False,
    x: float | None = None,
    y: float | None = None,
    force: bool = False,
    parent: str | None = None,
) -> dict[str, Any]:
    return ctx.world.create_location(
        name, description, discovered=discovered, x=x, y=y, force=force, parent=parent
    )


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


def discover_location(name: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.discover_location(name)


def delete_location(name: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.delete_location(name)


def hire_hireling(
    name: str,
    employer: str,
    wage: int,
    description: str,
    ctx: ToolContext,
    personality: str | None = None,
    accent: str | None = None,
    quirks: Sequence[str] | None = None,
    character: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return ctx.world.hire_hireling(
        name=name,
        employer=employer,
        wage=wage,
        description=description,
        personality=personality,
        accent=accent,
        quirks=quirks,
        character=character,
    )


def dismiss_hireling(name: str, cause: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.dismiss_hireling(name, cause)


def change_gold(name: str, amount: int, cause: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.change_gold(name, amount, cause)


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


def move_actor(name: str, ctx: ToolContext, location: str | None = None) -> dict[str, str]:
    return ctx.world.move_actor(name, location)


def create_encounter_map(
    name: str,
    location: str,
    columns: int,
    rows: int,
    description: str,
    ctx: ToolContext,
) -> dict[str, Any]:
    return ctx.world.create_encounter_map(name, location, columns, rows, description)


def update_encounter_map(
    name: str,
    columns: int,
    rows: int,
    description: str,
    ctx: ToolContext,
    new_name: str | None = None,
    location: str | None = None,
) -> dict[str, Any]:
    return ctx.world.update_encounter_map(
        name, columns, rows, description, new_name=new_name, location=location
    )


def delete_encounter_map(name: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.delete_encounter_map(name)


def start_encounter(map: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.start_encounter(map)


def end_encounter(ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.end_encounter()


def add_participant(name: str, ctx: ToolContext, side: str | None = None) -> dict[str, Any]:
    return ctx.world.add_participant(name, side)


def remove_participant(name: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.remove_participant(name)


def place_participant(name: str, column: int, row: int, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.place_participant(name, column, row)


def end_npc_turn(name: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.end_npc_turn(name)


def add_note(text: str, ctx: ToolContext, subject: str | None = None) -> dict[str, Any]:
    return ctx.world.add_note(text, subject)


def update_note(note_id: int, text: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.update_note(note_id, text)


def delete_note(note_id: int, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.delete_note(note_id)


def search_notes(query: str, ctx: ToolContext) -> dict[str, list[dict[str, Any]]]:
    return ctx.world.search_notes(query)


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


async def speak(npc: str, intent: str, ctx: ToolContext, replace: bool = False) -> dict[str, str]:
    profile = ctx.world.speaker_profile(npc)
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
