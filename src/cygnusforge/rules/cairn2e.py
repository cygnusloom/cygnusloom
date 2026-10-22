"""Python side of the Cairn 2e rule set. Its JSON names these functions."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from cygnusforge.rules.context import ToolContext

FUMBLE = 20


def save_succeeds(roll: int, score: int) -> bool:
    """A d20 save succeeds when the roll is at or under the score. A roll of 1 always succeeds,
    and a 20 always fails."""
    if roll == 1:
        return True
    if roll == FUMBLE:
        return False
    return roll <= score


def initiative_order(entries: Sequence[tuple[str, bool | None]]) -> list[str]:
    """The order of round one. Player characters who passed their DEX save act first, then the
    NPCs (given as None), then those who failed. Each group keeps the order it came in."""
    passed = [name for name, result in entries if result is True]
    npcs = [name for name, result in entries if result is None]
    failed = [name for name, result in entries if result is False]
    return passed + npcs + failed


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
    )


def attack(attacker: str, attack: str, target: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.attack(attacker, attack, target)


def player_attack(target: str, weapon: str, ctx: ToolContext, damage: int = 0) -> dict[str, Any]:
    return ctx.world.player_attack(target, weapon, damage)


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


def end_condition(name: str, condition: str, how: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.end_condition(name, condition, how)


def retreat(name: str, safe: bool, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.retreat(name, safe)


def player_retreat(safe: bool, ctx: ToolContext, roll: int = 0) -> dict[str, Any]:
    return ctx.world.player_retreat(safe, roll)
