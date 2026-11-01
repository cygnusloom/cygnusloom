"""Python side of the Cairn 2e rule set. Its JSON names these functions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from cygnusforge.content.definitions import ContentError, required_text
from cygnusforge.content.library import InventoryRules
from cygnusforge.rules.context import ToolContext
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import (
    PC_KIND,
    Item,
    ItemRemoved,
    ItemUsesSet,
    name_key,
    record,
)
from cygnusforge.services.hooks import HookContext
from cygnusforge.services.inventory import carry_item

if TYPE_CHECKING:
    from cygnusforge.content.character import CharacterSpec


def configure_hooks(
    options: Mapping[str, Any], character: CharacterSpec, label: str
) -> Mapping[str, Any]:
    """Resolve Cairn's configured ration feature to the item name used by its rest hook."""
    configured = dict(options)
    if "feature" not in configured:
        return configured

    feature_id = required_text(configured, "feature", f"{label}: options")
    if feature_id not in character.features:
        raise ContentError(f"{label}: 'feature' names no character feature ({feature_id!r})")
    feature = character.features[feature_id]
    if feature.feature_type != "item" or feature.uses < 1:
        raise ContentError(
            f"{label}: 'feature' must be a consumable item with uses ({feature_id!r})"
        )
    configured["item"] = feature.name
    return configured


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


def player_attack(
    target: str, weapon: str, ctx: ToolContext, modifier: str = "", damage: int = 0
) -> dict[str, Any]:
    return ctx.world.player_attack(target, weapon, modifier, damage_roll=damage or None)


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
    """Mark a character deprived, with the cause, or clear the mark. Returns the day the
    deprivation began, or None when the character is not deprived."""

    def action(hc: HookContext) -> dict[str, Any]:
        state = hc.state()
        character = state.characters.get(name_key(name))
        if character is None:
            raise InvalidInput(f"there is no character named {name!r}")
        if deprived and not cause.strip():
            raise InvalidInput("say what the character is deprived of")
        if deprived:
            since = _deprived_since(character.marks, state.clock.day)
            hc.set_mark(
                character.name,
                DEPRIVED,
                {"since": since, "cause": cause.strip()},
                f"{character.name} is deprived: {cause.strip()}.",
            )
            return {"name": character.name, "deprived": True, "since_day": since}
        hc.set_mark(character.name, DEPRIVED, None, f"{character.name} is no longer deprived.")
        return {"name": character.name, "deprived": False, "since_day": None}

    return ctx.world.with_hook_context(action)


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
    return ctx.world.give_spell_item(name, spell, description, role="reusable")


def give_scroll(name: str, spell: str, description: str, ctx: ToolContext) -> dict[str, Any]:
    return ctx.world.give_spell_item(name, spell, description, role="single_use")


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


# The character mark that records deprivation: {"since": day, "cause": text}.
DEPRIVED = "deprived"
# The character mark that records a bed for the night.
SHELTERED = "sheltered"
# The character mark that records the hour of the last meal, in hours since day 1.
MEAL_AT = "meal_at"


def _deprived_since(marks: Mapping[str, Any], day: int) -> int:
    held = marks.get(DEPRIVED)
    return int(held["since"]) if held else day


def _deprived_cause(marks: Mapping[str, Any]) -> str:
    held = marks.get(DEPRIVED)
    return str(held["cause"]) if held else ""


def _options(hc: HookContext) -> Mapping[str, Any]:
    return hc.hooks.options if hc.hooks is not None else {}


def _held_ration(character: Any, item: str) -> Any:
    return next((i for i in character.items if name_key(i.name) == name_key(item)), None)


def _spend_ration(hc: HookContext, character: Any, held: Any) -> None:
    db, campaign_id = hc.session()
    if held.max_uses and held.uses > 1:
        record(
            db,
            campaign_id,
            ItemUsesSet(name=character.name, item=held.name, uses=held.uses - 1, cause="eaten"),
        )
    else:
        record(db, campaign_id, ItemRemoved(name=character.name, item=held.name))


def new_day(hc: HookContext, day: int) -> None:
    """Called once for each newly started day. A deprived character who has gone more than
    `after_days` days without what they need gains the penalty item, once per day."""
    options = _options(hc)
    inventory: InventoryRules | None = hc.inventory
    if inventory is None:
        return
    after_days = int(options.get("after_days", 1))
    db, campaign_id = hc.session()
    for character in hc.state().characters.values():
        if DEPRIVED not in character.marks:
            continue
        since = _deprived_since(character.marks, day)
        if day - since <= after_days:
            continue
        penalty = Item(
            inventory.penalty_name, "", inventory.penalty_slots, kind=inventory.penalty_item
        )
        carry_item(db, campaign_id, inventory, character.name, penalty, cause="need")


def before_rest(hc: HookContext, rest_id: str, start: int, hours_per_part: int) -> None:
    """Called before time passes on an upkeep rest that starts `start` hours into the campaign.
    Each party member who is not sheltered eats one ration. A sheltered member's bed is used up
    instead. A member who ate a meal one part before this rest is fed by that meal. A member with
    no ration is deprived, unless they already are."""
    options = _options(hc)
    state = hc.state()
    party = [c for c in state.characters.values() if c.kind == PC_KIND]
    for character in party:
        meal_at = character.marks.get(MEAL_AT)
        if meal_at is not None:
            hc.set_mark(character.name, MEAL_AT, None, f"{character.name}'s meal is used up.")
        if character.marks.get(SHELTERED):
            hc.set_mark(character.name, SHELTERED, None, f"{character.name}'s bed is used up.")
            continue
        if meal_at is not None and start - int(meal_at) == hours_per_part:
            continue
        held = _held_ration(character, options["item"])
        if held is None:
            if DEPRIVED not in character.marks:
                hc.set_mark(
                    character.name,
                    DEPRIVED,
                    {"since": state.clock.day, "cause": "no food"},
                    f"{character.name} is deprived: no food.",
                )
            continue
        _spend_ration(hc, character, held)


def held_back(hc: HookContext, name: str) -> str:
    """The cause a character is held back by, or "" when they are not held back."""
    return _deprived_cause(hc.marks(name))


def after_consume(
    hc: HookContext, item: str, used: list[str], short: list[str], at: int, day: int
) -> str:
    """Called when a tracked wilderness item is consumed. Members who used one have eaten a meal
    at `at`. Members who had none become deprived, unless they already are."""
    for name in used:
        hc.set_mark(name, MEAL_AT, at, f"{name} has eaten a meal.")
    for name in short:
        if DEPRIVED not in hc.marks(name):
            hc.set_mark(
                name,
                DEPRIVED,
                {"since": day, "cause": f"no {item}"},
                f"{name} is deprived: no {item}.",
            )
    return ""


def lodge(hc: HookContext, names: list[str]) -> None:
    """Called after a paid lodging. Each buyer has a bed for the night."""
    for name in names:
        hc.set_mark(name, SHELTERED, True, f"{name} has a bed for the night.")


def status(hc: HookContext, character: Any) -> list[str]:
    """Status lines for a character: the deprivation, if any."""
    held = character.marks.get(DEPRIVED)
    if not held:
        return []
    return [f"Deprived: {held['cause']} (since day {held['since']})"]
