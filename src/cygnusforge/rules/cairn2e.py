"""Python side of the Cairn 2e rule set. Its JSON names these functions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from cygnusforge.rules.context import ToolContext


def search_monster(query: str, ctx: ToolContext) -> dict[str, list[dict[str, Any]]]:
    return ctx.world.search_monster(query)


def create_monster(
    name: str,
    location: str,
    ctx: ToolContext,
    description: str | None = None,
    template: str | None = None,
    hit_points: int | None = None,
    armor: int | None = None,
    strength: int | None = None,
    dexterity: int | None = None,
    willpower: int | None = None,
    attacks: list[dict[str, str]] | None = None,
    force: bool = False,
    detachment: bool | None = None,
) -> dict[str, Any]:
    return ctx.world.create_monster(
        name,
        location,
        description=description,
        template=template,
        stats={
            "hp": hit_points,
            "str": strength,
            "dex": dexterity,
            "wil": willpower,
        },
        armor=armor,
        attacks=attacks,
        force=force,
        detachment=detachment,
    )


def attack(
    attacker: str,
    attack: str,
    target: str,
    ctx: ToolContext,
    modifier: str = "",
    targets: list[str] | None = None,
) -> dict[str, Any]:
    return ctx.world.attack(attacker, attack, target, modifier, targets)


def resolve_attacks(target: str, attacks: list[dict[str, str]], ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.resolve_attacks(target, attacks)


def player_attack(target: str, weapon: str, ctx: ToolContext, modifier: str = "") -> dict[str, Any]:
    return ctx.world.player_attack(target, weapon, modifier)


def resolve_group_break(name: str, outcome: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.resolve_group_break(name, outcome)


def roll_tables(tables: list[str], ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.roll_tables(tables)


def dungeon_cycle(triggers: list[str], reason: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.dungeon_cycle(triggers, reason)


def wilderness_action(
    action: str, reason: str, ctx: ToolContext, purchases: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return ctx.world.wilderness_action(action, reason, purchases=purchases)


def roll_reaction(reason: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.roll_reaction(reason)


def advance_time(ctx: ToolContext, days: int = 0, parts: int = 0, hours: int = 0) -> dict[str, Any]:
    return ctx.world.advance_time(days=days, parts=parts, hours=hours)


def rest(length: str, reason: str, ctx: ToolContext, interrupted: bool = False) -> dict[str, Any]:
    return ctx.world.rest(length, interrupted=interrupted, reason=reason)


def rest_history(ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.rest_history()


def set_condition(name: str, deprived: bool, cause: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.set_condition(name, deprived, cause=cause)


def add_penalty(name: str, cause: str, ctx: ToolContext, force: bool = False) -> dict[str, Any]:
    return ctx.world.add_penalty(name, cause, force=force)


def search_marketplace(query: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.search_marketplace(query)


def buy_item(
    name: str, item: str, ctx: ToolContext, price: int | None = None, size: str = ""
) -> dict[str, Any]:
    return ctx.world.buy_item(name, item, price=price, size=size)


def buy_service(names: list[str], service: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.buy_service(names, service)


def give_item(
    name: str, item: str, ctx: ToolContext, size: str = "normal", damage: str = ""
) -> dict[str, Any]:
    return ctx.world.give_item(name, item, size=size, damage=damage)


def drop_item(name: str, item: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.drop_item(name, item)


def use_item(name: str, item: str, ctx: ToolContext, target: str | None = None) -> dict[str, Any]:
    return ctx.world.use_item(name, item, target)


def stabilize(name: str, explanation: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.stabilize(name, explanation)


def give_spellbook(name: str, spell: str, description: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.give_spellbook(name, spell, description)


def give_scroll(name: str, spell: str, description: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.give_scroll(name, spell, description)


def give_charged_item(
    name: str, item: str, uses: int, recharge: str, ctx: ToolContext, size: str = "normal"
) -> dict[str, Any]:
    return ctx.world.give_charged_item(name, item, uses, recharge, size=size)


def cast_spell(name: str, spell: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.cast_spell(name, spell)


def use_charge(name: str, item: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.use_charge(name, item)


def recharge_charged_item(name: str, item: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.recharge_charged_item(name, item)


def roll_save(name: str, stat: str, reason: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.roll_save(name, stat, reason)


def player_save(ctx: ToolContext, roll: int = 0) -> dict[str, Any]:
    return ctx.world.player_save(roll)


def player_initiative(ctx: ToolContext, roll: int = 0) -> dict[str, Any]:
    return ctx.world.player_initiative(roll)


def set_armor(name: str, armor: int, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.set_armor(name, armor)


def recover_character(
    name: str, explanation: str, consequence: str, ctx: ToolContext
) -> dict[str, Any]:
    return ctx.world.recover_character(name, explanation, consequence)


def harm_character(
    name: str, stat: str, amount: int, cause: str, ctx: ToolContext
) -> dict[str, Any]:
    return ctx.world.harm_character(name, stat, amount, cause)


def retreat(name: str, safe: bool, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.retreat(name, safe)


def surrender(name: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.surrender(name)


def player_retreat(safe: bool, ctx: ToolContext, roll: int = 0) -> dict[str, Any]:
    return ctx.world.player_retreat(safe, roll)


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
