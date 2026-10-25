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
) -> dict[str, Any]:
    return ctx.world.create_region(name, terrain, points, force=force)


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
    pronouns: str | None = None,
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
        pronouns=pronouns,
    )


def dismiss_hireling(name: str, cause: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.dismiss_hireling(name, cause)


def change_gold(name: str, amount: int, cause: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.change_gold(name, amount, cause)


def place_object(
    place: str,
    contents: str,
    hidden: bool,
    reason: str,
    ctx: ToolContext,
    gold: int | None = None,
    item: str | None = None,
    item_size: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    return ctx.world.place_object(
        place, contents, hidden, reason, gold, item, item_size or "", name or ""
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


def delete_npc(name: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.delete_npc(name)


def move_actor(
    name: str,
    mode: str,
    ctx: ToolContext,
    location: str | None = None,
    travel: str = "on_foot",
    route: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return ctx.world.move_actor(name, location, travel, mode, route)


def create_encounter_map(
    name: str,
    location: str,
    description: str,
    ctx: ToolContext,
    columns: int | None = None,
    rows: int | None = None,
    template: str = "",
) -> dict[str, Any]:
    return ctx.world.create_encounter_map(
        name, location, columns, rows, description, template=template
    )


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


def set_map_cells(map: str, cells: list[dict[str, Any]], ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.set_encounter_map_cells(map, cells)


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
