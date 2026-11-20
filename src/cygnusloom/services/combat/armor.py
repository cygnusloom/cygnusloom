"""Armor and defense: what armor is worth and when equipment changes defense."""

from dataclasses import replace
from typing import Any

from sqlmodel import Session

from cygnusloom.content.character import CharacterSpec
from cygnusloom.content.library import (
    CombatRules,
)
from cygnusloom.services.combat.constants import UNCAPPED_ARMOR
from cygnusloom.services.common import InvalidInput, whole_number
from cygnusloom.services.events import (
    ArmorSet,
    Character,
    DefenseEquipmentSet,
    load_state,
    name_key,
    record,
)
from cygnusloom.services.state import same_item_name


def armor_after(current: int, listed: int, mode: str, defense: int = 0) -> int:
    """Combine an armor value with the current defense according to its configured mode.
    `shield_flat` adds the shield's full listed value whether or not body armor is worn."""
    if mode == "set":
        return defense + listed
    if mode == "max":
        return max(current, defense + listed)
    if mode == "shield_flat":
        return max(current, defense) + listed
    if mode == "shield":
        if current <= defense:
            return defense + listed
        return max(current + 1, defense + listed)
    return current + listed


def equipped_defense(character: Character, spec: CharacterSpec, base: int, modifier: str) -> int:
    """Calculate defense from passive floors, worn armor, a wielded shield, and adjustments.
    An equipped body armor may cap the Dexterity modifier its wearer adds."""
    bonus = character.modifier(spec, modifier) if modifier else 0
    if character.equipped_armor:
        armor = next(
            (
                armor
                for armor in spec.items.values()
                if same_item_name(armor.name, character.equipped_armor)
            ),
            None,
        )
        if armor is not None and armor.defense_dex_cap is not None:
            bonus = min(bonus, armor.defense_dex_cap)
    unarmored = base + bonus
    floor = max(base, character.innate_defense)
    flat_bonus = 0
    for feature_id, count in character.features.items():
        feature = spec.features.get(feature_id)
        if count <= 0 or feature is None:
            continue
        flat_bonus += feature.defense_bonus * count
        if feature.defense_floor is None:
            continue
        rule = feature.defense_floor
        if rule.requires_no_armor and (character.equipped_armor or character.equipped_armor_value):
            continue
        if rule.requires_no_shield and (
            character.equipped_shield or character.equipped_shield_value
        ):
            continue
        value = rule.base
        value += sum(character.modifier(spec, name) for name in rule.modifiers)
        if rule.counter:
            counter = spec.counters.get(rule.counter)
            if counter is None:
                raise InvalidInput(f"defense floor names no counter {rule.counter!r}")
            amount = character.counters.get(rule.counter, counter.initial)
            value += -(-amount // rule.divisor) if rule.round_up else amount // rule.divisor
        floor = max(floor, value)
    armor = max(unarmored + character.equipped_armor_value, floor + bonus) + flat_bonus
    if character.equipped_shield_value and not character.shield_suspended:
        armor = _shield_value(
            armor,
            character.equipped_shield_value,
            character.shield_mode or "shield",
            unarmored,
        )
    return armor + character.defense_adjustment


def recalculate_defense(
    db: Session,
    campaign_id: int,
    name: str,
    spec: CharacterSpec,
    rules: CombatRules,
) -> int:
    """Record the current defense after an equipment, feature, counter, or stat change."""
    character = load_state(db, campaign_id).characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    value = equipped_defense(character, spec, rules.defense_base, rules.defense_modifier)
    value = max(0, min(value, rules.armor_max or UNCAPPED_ARMOR))
    if value != character.armor:
        record(db, campaign_id, ArmorSet(name=character.name, armor=value))
    return value


def _defense_bonus(character: Character, spec: CharacterSpec, rules: CombatRules) -> int:
    """The Dexterity (or other) modifier a character adds to base defense, after an armor cap."""
    bonus = character.modifier(spec, rules.defense_modifier) if rules.defense_modifier else 0
    if character.equipped_armor:
        armor = next(
            (
                armor
                for armor in spec.items.values()
                if same_item_name(armor.name, character.equipped_armor)
            ),
            None,
        )
        if armor is not None and armor.defense_dex_cap is not None:
            bonus = min(bonus, armor.defense_dex_cap)
    return bonus


def _shield_value(current: int, value: int, mode: str, unarmored: int) -> int:
    """The total defense after adding a shield of `value` in its shop-item mode."""
    if mode == "shield_flat":
        return current + value
    return armor_after(current, value, "shield", unarmored)


def set_defense_equipment(
    db: Session,
    campaign_id: int,
    character: Character,
    spec: CharacterSpec,
    rules: CombatRules,
    *,
    armor: tuple[str, int] | None = None,
    shield: tuple[str, int] | None = None,
    adjustment: int | None = None,
    shield_mode: str = "shield",
) -> int:
    """Change either equipped defense slot or its adjustment, then record the resulting AC.

    `shield_mode` is the shop item's armor mode for a shield being equipped now; leaving it
    out keeps the shield contribution already recorded, which is what an adjustment expects."""
    body = (
        armor if armor is not None else (character.equipped_armor, character.equipped_armor_value)
    )
    held_shield = (
        shield
        if shield is not None
        else (character.equipped_shield, character.equipped_shield_value)
    )
    if body[1] < 0 or held_shield[1] < 0:
        raise InvalidInput("equipped armor values must not be negative")
    if (not body[0] and body[1]) or (not held_shield[0] and held_shield[1]):
        raise InvalidInput("equipped armor values need item names")
    innate = character.innate_defense
    if not character.defense_tracked and character.armor and adjustment is None:
        bonus = character.modifier(spec, rules.defense_modifier) if rules.defense_modifier else 0
        innate = max(0, character.armor - bonus)
    without_shield = equipped_defense(
        replace(
            character,
            equipped_armor=body[0],
            equipped_armor_value=body[1],
            equipped_shield="",
            equipped_shield_value=0,
            shield_bonus=0,
            shield_suspended=False,
        ),
        spec,
        rules.defense_base,
        rules.defense_modifier,
    )
    mode = (shield_mode if shield is not None else character.shield_mode) or "shield"
    shield_bonus = 0
    if held_shield[0]:
        unarmored = rules.defense_base + _defense_bonus(
            replace(
                character,
                equipped_armor=body[0],
                equipped_armor_value=body[1],
            ),
            spec,
            rules,
        )
        with_shield = _shield_value(without_shield, held_shield[1], mode, unarmored)
        shield_bonus = max(0, with_shield - without_shield)
    record(
        db,
        campaign_id,
        DefenseEquipmentSet(
            name=character.name,
            equipped_armor=body[0],
            equipped_armor_value=body[1],
            equipped_shield=held_shield[0],
            equipped_shield_value=held_shield[1],
            shield_mode=mode,
            shield_bonus=shield_bonus,
            innate_defense=innate,
            defense_adjustment=(character.defense_adjustment if adjustment is None else adjustment),
        ),
    )
    return recalculate_defense(db, campaign_id, character.name, spec, rules)


def set_armor(
    db: Session,
    campaign_id: int,
    name: str,
    armor: int,
    armor_max: int = 0,
    *,
    spec: CharacterSpec | None = None,
    rules: CombatRules | None = None,
) -> dict[str, Any]:
    """Set what each hit against a character absorbs, up to the rule set's armor cap (0 means
    no cap)."""
    state = load_state(db, campaign_id)
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    value = whole_number(armor, "armor", 0, armor_max or UNCAPPED_ARMOR)
    if character.defense_tracked and rules is not None and rules.defense_base:
        if spec is None:
            raise InvalidInput("equipped defense requires the character rules")
        unadjusted = equipped_defense(
            replace(character, defense_adjustment=0),
            spec,
            rules.defense_base,
            rules.defense_modifier,
        )
        set_defense_equipment(
            db, campaign_id, character, spec, rules, adjustment=value - unadjusted
        )
    else:
        record(db, campaign_id, ArmorSet(name=character.name, armor=value))
    db.commit()
    return {"character": character.name, "armor": value}
