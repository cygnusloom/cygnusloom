"""Attacks: the roll, the hit, the blast, and a party attacking together."""

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlmodel import Session

from cygnusloom.content.character import MASTERY_PROPERTIES, CharacterSpec, ItemTemplate
from cygnusloom.content.library import (
    ALL_STATS,
    CombatRules,
    ConditionRules,
    GroupRules,
)
from cygnusloom.rules.dice import ROLL_MODES, roll_mode_values, roll_total, roll_values
from cygnusloom.services import death, ranges, reactions
from cygnusloom.services.attacks import (
    AttackFlags,
    highest_index,
    modifier_dice,
    modifier_name,
    rolls_text,
)
from cygnusloom.services.campaign_switches import gm_plays_hirelings
from cygnusloom.services.combat.constants import NO_UNSKILLED_PENALTY
from cygnusloom.services.combat.damage import (
    Damage,
    WeaponBonuses,
    _defeat_after_harm,
    _end_if_one_side_left,
    _harm_stat,
    _injury_from_hit,
    _living,
    _require_impaired,
    apply_damage,
    notify_damage_taken,
)
from cygnusloom.services.combat.initiative import _no_initiative_pending
from cygnusloom.services.combat.save import _save
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.conditions import active_conditions, condition_roll_mode
from cygnusloom.services.encounters import (
    _participant,
    acting_error,
)
from cygnusloom.services.events import (
    NPC_KIND,
    PC_KIND,
    CampaignState,
    Character,
    ConditionAdded,
    EncounterAttacked,
    MarkSet,
    ParticipantPlaced,
    ShieldBashed,
    Struck,
    current,
    load_state,
    name_key,
    out_of_fight,
    record,
    roll_breakdown,
    same_item_name,
)


def _damage_total(dice: str, rng: random.Random, *, plus: str, lucky: bool) -> int:
    """Roll damage, and roll it twice and keep the better when Lucky applies."""
    total = roll_total(dice, rng, plus=plus)
    return max(total, roll_total(dice, rng, plus=plus)) if lucky else total


def _stat_bonus(combat: CombatRules, character: Character, stat: str) -> int:
    """What one of the rule set's attack stats adds, such as a hit or damage bonus. A rule set
    that names no stat adds nothing."""
    return current(character, stat) if stat else 0


def _attribute_modifier(
    spec: CharacterSpec | None, character: Character, attributes: Sequence[str]
) -> int:
    """The best of the weapon's attribute modifiers, or 0 when the rule set has no attributes."""
    return _attribute_choice(spec, character, attributes)[0]


def _attribute_choice(
    spec: CharacterSpec | None, character: Character, attributes: Sequence[str]
) -> tuple[int, str]:
    """The best of the weapon's attribute modifiers and the stat that gave it."""
    if spec is None:
        return 0, ""
    scores = [
        (character.modifier(spec, stat), stat) for stat in attributes if stat in character.stats
    ]
    if not scores:
        return 0, ""
    value, stat = max(scores, key=lambda item: item[0])
    return value, stat


def _stat_label(spec: CharacterSpec, stat: str, fallback: str = "") -> str:
    """A stat's display name for the Log, or a readable fallback when the rule set has no such
    stat."""
    found = spec.stats.get(stat)
    if found is not None:
        return found.name
    return fallback or stat.replace("_", " ")


def _skill_label(spec: CharacterSpec, skill: str) -> str:
    found = spec.skills.get(skill)
    return found.name if found is not None else skill.replace("_", " ")


def _weapon_skill(combat: CombatRules, weapon: str, feature: ItemTemplate | None) -> str:
    """The skill an attack with `weapon` rolls: the weapon's own when it has one, the unarmed
    skill for an unarmed strike, and no skill for a rule set with neither."""
    if feature is not None and feature.skill:
        return feature.skill
    if name_key(weapon) in combat.unarmed_names:
        return combat.unarmed_skill
    return ""


def _weapon_trained(spec: CharacterSpec, character: Character, feature: ItemTemplate) -> bool:
    """Whether the character's training covers a weapon. A weapon with no category is not
    covered by these rules, so it keeps the rule set's full attack bonus."""
    category = feature.weapon_category.casefold()
    if not category:
        return True
    properties = {prop.casefold() for prop in feature.weapon_properties}

    def covers(entry: str) -> bool:
        kind, _, property_name = entry.casefold().partition(":")
        if kind == category and (not property_name or property_name in properties):
            return True
        return kind == "weapon" and property_name == feature.id.casefold()

    for feature_id in character.features:
        held = spec.features.get(feature_id)
        if held is None:
            continue
        if any(covers(entry) for entry in held.weapon_training):
            return True
    for carried in character.items:
        template = spec.item_named(carried.name)
        if template is not None and any(covers(entry) for entry in template.weapon_training):
            return True
    return False


def _critical_threshold(
    spec: CharacterSpec | None, character: Character, combat: CombatRules
) -> int:
    """The lowest natural roll that crits, lowered by features such as Improved Critical."""
    threshold = combat.check_sides
    if spec is None:
        return threshold
    for feature_id in character.features:
        feature = spec.features.get(feature_id)
        if feature is None or feature.critical_min >= threshold:
            continue
        if feature.active_mark and not character.marks.get(feature.active_mark):
            continue
        threshold = feature.critical_min
    return threshold


def _is_light_weapon(feature: ItemTemplate | None) -> bool:
    """Whether a weapon has the Light property."""
    if feature is None:
        return False
    return "light" in feature.flags or "light" in feature.weapon_properties


def _check_extra_attack(
    state: CampaignState,
    attacker: Character,
    target: Character,
    weapon: str,
    weapon_feature: ItemTemplate | None,
    spec: CharacterSpec | None,
    mastery: str,
    extra: str,
    extra_from: str,
    cell_feet: int | None,
    combat: CombatRules,
) -> bool:
    """Check an extra attack the rules grant after an earlier attack this turn. Returns whether
    the extra attack spends the bonus action, which Light does unless the weapon has Nick."""
    encounter = state.encounter
    if encounter is None:
        raise InvalidInput("an extra attack needs an encounter in progress")
    if extra not in ("cleave", "light"):
        raise InvalidInput(f"there is no extra attack named {extra!r}; choose cleave or light")
    if extra in encounter.extra_attacks:
        raise InvalidInput(f"{attacker.name} has already made a {extra} extra attack this turn")
    weapon_id = weapon_feature.id if weapon_feature is not None else name_key(weapon)
    if extra == "cleave":
        if mastery != "cleave":
            raise InvalidInput(f"{weapon} does not have the Cleave mastery property")
        if weapon_id not in encounter.hit_with:
            raise InvalidInput(
                f"{attacker.name} has not hit with {weapon} this turn, so Cleave has no first "
                "target"
            )
        first = state.characters.get(name_key(extra_from))
        if not extra_from or first is None:
            raise InvalidInput("Cleave needs the first target's name in extra_from")
        if name_key(first.name) not in encounter.hit_targets:
            raise InvalidInput(f"{attacker.name} has not hit {first.name} with {weapon} this turn")
        if name_key(first.name) == name_key(target.name):
            raise InvalidInput("Cleave's second target must be a different creature")
        if cell_feet:
            first_cell = encounter.cells.get(name_key(first.name))
            target_cell = encounter.cells.get(name_key(target.name))
            if first_cell is not None and target_cell is not None:
                feet = max(abs(target_cell[0] - first_cell[0]), abs(target_cell[1] - first_cell[1]))
                feet *= cell_feet
                if feet > 5:
                    raise InvalidInput(
                        f"{target.name} is {feet} feet from {first.name}; Cleave needs a second "
                        "creature within 5 feet of the first"
                    )
        return False
    if not _is_light_weapon(weapon_feature):
        raise InvalidInput(f"{weapon} is not a Light weapon")
    light_ids = (
        {item.id for item in spec.items.values() if _is_light_weapon(item)}
        if spec is not None
        else set()
    )
    previous = [used for used in encounter.attacked_with if used != weapon_id and used in light_ids]
    if not previous:
        raise InvalidInput(
            f"{attacker.name} has not attacked with a different Light weapon this turn"
        )
    if mastery == "nick":
        return False
    limit = combat.bonus_actions_per_turn
    if limit and encounter.bonus_actions_used >= limit:
        raise InvalidInput(
            f"{attacker.name} has no bonus action left this turn for the Light extra attack"
        )
    return True


def _mastered_property(
    spec: CharacterSpec | None,
    character: Character,
    feature: ItemTemplate | None,
    override: str = "",
) -> str:
    """The weapon's mastery property when the character's Weapon Mastery choices unlock it. A
    feature such as Tactical Master may let the character use another property instead."""
    if spec is None or feature is None or not feature.mastery:
        return ""
    unlocked = any(
        "weapon-mastery" in key.split(":", 1)[0] and feature.id in values
        for key, values in character.feature_choices.items()
    )
    if not unlocked:
        return ""
    if not override or override == feature.mastery:
        return feature.mastery
    for feature_id in character.features:
        held = spec.features.get(feature_id)
        if held is not None and override in held.mastery_options:
            return override
    raise InvalidInput(f"{character.name} cannot replace {feature.name}'s mastery with {override}")


def _player_bonuses(
    combat: CombatRules,
    character: Character,
    weapon: str,
    spec: CharacterSpec | None,
    feature: ItemTemplate | None,
    *,
    omit_attribute_damage: bool = False,
) -> WeaponBonuses:
    """What a player character's attack with `weapon` adds, from the rule set's combat fields, the
    weapon's skill and attributes, and the character's skills. A rule set with no spec adds
    nothing, so its attacks stay exactly as they were."""
    if spec is None:
        return WeaponBonuses()
    unarmed = name_key(weapon) in combat.unarmed_names
    attributes = (
        feature.attributes
        if feature is not None and feature.attributes
        else combat.unarmed_attributes
        if unarmed
        else ()
    )
    attribute, attribute_stat = _attribute_choice(spec, character, attributes)
    to_hit_parts: list[tuple[str, int]] = []
    damage_parts: list[tuple[str, int]] = []
    shock_parts: list[tuple[str, int]] = []
    if combat.hit_bonus_stat:
        to_hit_parts.append(
            (
                _stat_label(spec, combat.hit_bonus_stat),
                _stat_bonus(combat, character, combat.hit_bonus_stat),
            )
        )
    if (
        combat.weapon_proficiency_stat
        and feature is not None
        and feature.weapon_category
        and not _weapon_trained(spec, character, feature)
    ):
        penalty = _stat_bonus(combat, character, combat.weapon_proficiency_stat)
        if penalty:
            to_hit_parts.append(("not proficient", -penalty))
    if attribute:
        to_hit_parts.append((_stat_label(spec, attribute_stat), attribute))
    if combat.damage_bonus_stat:
        damage_parts.append(
            (
                _stat_label(spec, combat.damage_bonus_stat),
                _stat_bonus(combat, character, combat.damage_bonus_stat),
            )
        )
    class_bonus = _level_damage_bonus(combat, character, spec, unarmed=unarmed)
    if class_bonus:
        label = combat.level_damage.label if combat.level_damage is not None else "level damage"
        damage_parts.append((label, class_bonus))
    skill = _weapon_skill(combat, weapon, feature)
    if skill and skill in spec.skills:
        level = spec.skill_level(character.skills, skill)
        if spec.skill_trained(character.skills, skill):
            to_hit_parts.append((_skill_label(spec, skill), level))
            if unarmed:
                damage_parts.append((_skill_label(spec, skill), level))
        elif not (feature is not None and NO_UNSKILLED_PENALTY in feature.flags):
            to_hit_parts.append(("unskilled", -combat.unskilled_attack_penalty))
    if (
        combat.attribute_damage
        and attributes
        and attribute
        and not (omit_attribute_damage and attribute > 0)
    ):
        damage_parts.append((_stat_label(spec, attribute_stat), attribute))
    if feature is not None and feature.shock:
        # Shock takes the weapon's own rating and the attribute modifier. Other damage bonuses,
        # such as a Warrior's Killing Blow, do not apply unless the rules say they reach Shock.
        shock_parts.append(("Shock", feature.shock))
        if attribute:
            shock_parts.append((_stat_label(spec, attribute_stat), attribute))
    for feature_id, count in character.features.items():
        if count < 1:
            continue
        held = spec.features.get(feature_id)
        if held is None:
            continue
        if held.active_mark and not character.marks.get(held.active_mark):
            continue
        for bonus in held.weapon_bonuses:
            if bonus.skill and bonus.skill != skill:
                continue
            if bonus.skill and not spec.skill_trained(character.skills, bonus.skill):
                continue
            level = spec.skill_level(character.skills, bonus.skill) if bonus.skill else 1
            if not level:
                continue
            label = _skill_label(spec, bonus.skill) if bonus.skill else held.name
            if bonus.hit:
                to_hit_parts.append((label, level))
            if bonus.damage:
                damage_parts.append((label, level))
            if bonus.shock and feature is not None and feature.shock:
                shock_parts.append((label, level))
        ranged = bool(feature is not None and feature.ranges)
        kind = "ranged" if ranged else "melee"
        if held.weapon_kind and held.weapon_kind != kind:
            continue
        if held.attack_bonus:
            to_hit_parts.append((held.name, held.attack_bonus))
        if held.damage_bonus:
            damage_parts.append((held.name, held.damage_bonus))
        if held.damage_bonus_levels:
            level = int(character.counters.get("level", 1))
            bonus = max(
                (value for at, value in held.damage_bonus_levels.items() if at <= level),
                default=0,
            )
            if bonus:
                damage_parts.append((held.name, bonus))
    return WeaponBonuses(
        to_hit=sum(value for _, value in to_hit_parts),
        damage=sum(value for _, value in damage_parts),
        shock=sum(value for _, value in shock_parts),
        shock_ac=feature.shock_ac if feature is not None and feature.shock else 0,
        attribute=attribute or 0,
        to_hit_parts=tuple(to_hit_parts),
        damage_parts=tuple(damage_parts),
        shock_parts=tuple(shock_parts),
    )


def _level_damage_bonus(
    combat: CombatRules, character: Character, spec: CharacterSpec, *, unarmed: bool = False
) -> int:
    """The damage the configured template's counter adds, rounded up by its divisor. A rule set
    whose level damage applies only to weapons gives an unarmed attack nothing."""
    rules = combat.level_damage
    if rules is None:
        return 0
    if unarmed and rules.weapons_only:
        return 0
    counter = spec.counters.get(rules.counter)
    if counter is None:
        return 0
    if character.templates.get(rules.template_kind, "") not in rules.templates:
        return 0
    level = character.counters.get(rules.counter, counter.initial)
    return -(-level // rules.divisor)


def _attack_shock(source: Character, name: str) -> tuple[int, int]:
    """The shock one of a character's attacks does, and the armor it shocks through, or zeros when
    the attack has none."""
    pair = source.attack_shock.get(name) or ()
    if len(pair) < 2:
        return 0, 0
    return int(pair[0]), int(pair[1])


def _strike(
    db: Session,
    campaign_id: int,
    combat: CombatRules,
    rng: random.Random,
    target: Character,
    attacker: str,
    weapon: str,
    raw: int,
    modifier: str = "",
    rolled: str = "",
    *,
    plus: str,
    ignores_armor: bool = False,
    to_hit: int | None = None,
    natural: int | None = None,
    hit_bonus: int = 0,
    shock: int = 0,
    shock_ac: int = 0,
    no_shock: bool = False,
    nonlethal: bool = False,
    damage_type: str = "",
    spec: CharacterSpec | None = None,
    hit_breakdown: dict[str, Any] | None = None,
    damage_breakdown: dict[str, Any] | None = None,
    shock_breakdown: dict[str, Any] | None = None,
    roll_mode: str = "normal",
    critical: bool = False,
    mastery: str = "",
    graze_ability: int = 0,
    mastery_dc: int = 0,
) -> dict[str, Any]:
    """Apply a hit through the rule set's damage steps. A target that owes a save takes no more
    hits. An NPC saves at once; a player character saves when the player rolls. A hit on an NPC
    may also bring its side's morale check. An attack that ignores armor skips the target's armor.

    Under the "auto" hit mode the blow always lands. Under "roll_vs_defense" a roll below the
    target's armor, which is its defense (none when the attack ignores armor), misses: the miss is
    logged and no damage is rolled. `to_hit` is a hit roll the caller already made, with its own
    bonuses; when it is None the check die is rolled here and `hit_bonus` is added to it.

    A rule set whose shock does harm on a miss (`shock_on_miss`) harms the first harm stat by
    `shock` when the defense is at most `shock_ac`, and no armor stops it. One whose hits never do
    less than their shock (`minimum_shock_damage`) raises the damage to `shock` when it applies.
    Harm that takes a stat in `defeat_at_zero` to 0 defeats the target, with the rule set's status."""
    if roll_mode not in ROLL_MODES:
        raise InvalidInput(
            f"roll mode must be normal, advantage, or disadvantage, not {roll_mode!r}"
        )
    if target.save_due is not None:
        raise InvalidInput(f"{target.name} must make their {target.save_due} save first")
    if damage_type and spec is not None:
        resisted = any(
            damage_type.casefold() in (feature.resistances or ())
            and (not feature.active_mark or target.marks.get(feature.active_mark))
            for feature_id in target.features
            if (feature := spec.features.get(feature_id)) is not None
        )
        if resisted:
            raw = max(0, raw // 2)
    defense = 0 if ignores_armor else target.armor
    # Total defense also turns aside Shock until the character's next turn; the source grants
    # the immunity alongside the AC bonus. A thrown or ranged attack never inflicts Shock.
    shock_applies = (
        bool(shock) and defense <= shock_ac and not target.total_defense and not no_shock
    )
    if combat.hit_mode == "roll_vs_defense":
        if to_hit is None:
            rolled_dice, natural = roll_mode_values(
                combat.check_die, rng, plus=plus, mode=roll_mode
            )
            to_hit = natural + hit_bonus
        else:
            rolled_dice = [] if natural is None else [natural]
        if hit_breakdown is None:
            hit_breakdown = roll_breakdown(
                combat.check_die,
                rolled_dice,
                [("bonuses", to_hit - (natural or 0))],
                to_hit,
                target=defense,
                target_label="AC",
            )
        hit = to_hit >= defense
        if natural == 1 and combat.natural_one_misses:
            hit = False
        elif natural == combat.check_sides and combat.natural_max_hits:
            hit = True
        if not hit:
            harm: dict[str, int] = {}
            shock_taken = 0
            shield_round = 0
            stat = _harm_stat(combat)
            if (
                combat.shock_on_miss
                and shock_applies
                and stat is not None
                and not target.invincible
            ):
                shield_round = _shield_round(db, campaign_id, target)
                if shield_round:
                    # The shield stops this Shock, and cannot stop another until the next round.
                    pass
                else:
                    harm[stat] = shock
                    shock_taken = shock
                    if shock_breakdown is None:
                        shock_breakdown = roll_breakdown(
                            "",
                            [],
                            [("Shock", shock)],
                            shock,
                            target=shock_ac,
                            target_label="Shock AC",
                        )
            graze_taken = 0
            if mastery == "graze" and graze_ability and not target.invincible:
                graze_damage = apply_damage(
                    combat.damage, 0, lambda stat: current(target, stat), graze_ability
                )
                for stat, amount in graze_damage.harm.items():
                    harm[stat] = harm.get(stat, 0) + amount
                graze_taken = sum(graze_damage.harm.values())
            defeats, status = _defeat_after_harm(combat, target, harm, nonlethal=nonlethal)
            record(
                db,
                campaign_id,
                Struck(
                    name=target.name,
                    attacker=attacker,
                    weapon=weapon,
                    raw=raw,
                    armor=0,
                    harm=harm,
                    modifier=modifier,
                    hit=False,
                    to_hit=to_hit,
                    shock=shock_taken,
                    shield_round=shield_round,
                    defeats=defeats,
                    status=status,
                    hit_breakdown=hit_breakdown,
                    shock_breakdown=shock_breakdown,
                    mastery=mastery,
                    graze=graze_taken,
                    critical=critical,
                ),
            )
            _record_shield_bash(db, campaign_id, attacker, weapon)
            db.commit()
            if defeats:
                if not nonlethal and (target.kind == PC_KIND or target.important):
                    death.begin_dying(db, campaign_id, combat, target.name, spec=spec)
                _end_if_one_side_left(db, campaign_id)
            result = {
                "target": target.name,
                "raw": raw,
                "armor": 0,
                "harm": harm,
                "save_due": None,
                "hit": False,
                "graze": graze_taken,
                "to_hit": to_hit,
                "defeated": load_state(db, campaign_id).characters[name_key(target.name)].defeated,
            }
            result.update(
                notify_damage_taken(
                    db,
                    campaign_id,
                    combat,
                    target.name,
                    sum(harm.values()),
                    f"{attacker}'s {weapon}",
                    spec=spec,
                    rng=rng,
                    plus=plus,
                )
            )
            return result
    armor = 0 if ignores_armor else target.armor
    if shock_applies and combat.minimum_shock_damage:
        raw = max(raw, shock)
    damage = apply_damage(combat.damage, armor, lambda stat: current(target, stat), raw)
    if target.invincible:
        # The whole hit is absorbed, so the log reads as a blow turned aside rather than a miss.
        damage = Damage(absorbed=raw, harm={}, save=None)
    defeats, status = _defeat_after_harm(combat, target, damage.harm, nonlethal=nonlethal)
    record(
        db,
        campaign_id,
        Struck(
            name=target.name,
            attacker=attacker,
            weapon=weapon,
            raw=raw,
            armor=damage.absorbed,
            harm=damage.harm,
            save=damage.save,
            modifier=modifier,
            rolled=rolled,
            to_hit=to_hit,
            defeats=defeats,
            status=status,
            hit_breakdown=hit_breakdown,
            damage_breakdown=damage_breakdown,
            critical=critical,
            mastery=mastery,
        ),
    )
    mastery_condition = {"sap": "sapped", "slow": "slowed"}.get(mastery)
    if mastery_condition and not target.invincible:
        record(
            db,
            campaign_id,
            ConditionAdded(
                name=target.name,
                condition=mastery_condition,
                cause=f"{mastery} mastery",
                until=attacker,
            ),
        )
    if mastery == "topple" and mastery_dc and not target.invincible:
        save = roll_total(combat.check_die, rng, plus=plus)
        con = int(target.stats.get("con", 10))
        save_bonus = int(target.stats.get("save_con", (con - 10) // 2))
        if save + save_bonus < mastery_dc:
            record(
                db,
                campaign_id,
                ConditionAdded(
                    name=target.name,
                    condition="prone",
                    cause=f"topple mastery (DC {mastery_dc})",
                ),
            )
    if mastery == "vex" and not target.invincible:
        record(
            db,
            campaign_id,
            MarkSet(
                name=attacker,
                key=f"vex:{target.name}",
                value=True,
                text=f"{attacker} has advantage against {target.name} from Vex.",
            ),
        )
    if mastery == "push" and not target.invincible:
        pushed = load_state(db, campaign_id)
        encounter = pushed.encounter
        if encounter is not None:
            source_cell = encounter.cells.get(name_key(attacker))
            target_cell = encounter.cells.get(name_key(target.name))
            if source_cell is not None and target_cell is not None:
                dx = target_cell[0] - source_cell[0]
                dy = target_cell[1] - source_cell[1]
                step_x = 1 if dx > 0 else -1 if dx < 0 else 0
                step_y = 1 if dy > 0 else -1 if dy < 0 else 0
                column = max(0, min(target_cell[0] + step_x * 2, 1000))
                row = max(0, min(target_cell[1] + step_y * 2, 1000))
                record(
                    db,
                    campaign_id,
                    ParticipantPlaced(name=target.name, column=column, row=row),
                )
    _record_shield_bash(db, campaign_id, attacker, weapon)
    db.commit()
    if defeats and not nonlethal and (target.kind == PC_KIND or target.important):
        death.begin_dying(db, campaign_id, combat, target.name, spec=spec)
    result: dict[str, Any] = {
        "target": target.name,
        "raw": raw,
        "armor": damage.absorbed,
        "harm": damage.harm,
        "save_due": damage.save,
    }
    if critical:
        result["critical"] = True
    injury = _injury_from_hit(
        db, campaign_id, combat, rng, target.name, damage, plus=plus, spec=spec
    )
    if injury is not None:
        result["injury"] = injury
    if damage.save is not None and target.kind == NPC_KIND:
        state = load_state(db, campaign_id)
        character = state.characters[name_key(target.name)]
        result["save"] = _save(db, campaign_id, combat, rng, character, damage.save, plus=plus)
    if target.kind == NPC_KIND and not target.large_group and combat.morale is not None:
        # morale imports this module for the shared helpers, so it is imported here to avoid a cycle.
        from cygnusloom.services.morale import check_morale

        checks = check_morale(db, campaign_id, combat, rng, target.name, plus=plus)
        if checks:
            result["morale"] = checks
    result["defeated"] = load_state(db, campaign_id).characters[name_key(target.name)].defeated
    _end_if_one_side_left(db, campaign_id)
    result.update(
        notify_damage_taken(
            db,
            campaign_id,
            combat,
            target.name,
            sum(damage.harm.values()),
            f"{attacker}'s {weapon}",
            spec=spec,
            rng=rng,
            plus=plus,
        )
    )
    return result


def _attack_named(source: Character, attack_name: str) -> str:
    """The name of one of the NPC's attacks, matched without regard to case."""
    wanted = attack_name.strip().casefold()
    matches = [name for name in source.attacks if name.casefold() == wanted]
    if not matches:
        options = ", ".join(source.attacks) or "none"
        raise InvalidInput(f"{source.name} has no attack named {attack_name!r}; attacks: {options}")
    return matches[0]


def attack(
    db: Session,
    campaign_id: int,
    attacker: str,
    attack_name: str,
    target: str,
    combat: CombatRules,
    rng: random.Random,
    modifier: str = "",
    *,
    plus: str,
    targets: Sequence[str] | None = None,
    hit_modifier: int = 0,
    damage_modifier: int = 0,
    shock_modifier: int = 0,
    conditions: Mapping[str, ConditionRules] | None = None,
    spec: CharacterSpec | None = None,
    force: bool = False,
    cell_feet: int | None = None,
    nonlethal: bool = False,
    roll_mode: str = "normal",
) -> dict[str, Any]:
    """An NPC's attack. Under the auto hit mode it always hits, so the attack's damage is the roll.
    A modifier rolls its own dice instead of the attack's. An attack with Blast hits every target in
    its area, which is `targets` (it must include `target`), and each target rolls separately. An
    attacker may only strike on their own turn unless `force` overrides that; the turn order's
    Start and End buttons are what hand out turns.

    `cell_feet` turns on the rule set's distance limits: when both the attacker and target have a
    cell, the attack must be within its reach or a ranged band, with a clear line of sight."""
    state = load_state(db, campaign_id)
    _no_initiative_pending(state)
    source = _participant(state, attacker)
    _require_attack(state, source, combat, force)
    if source.grappled_by and name_key(attack_name) not in combat.unarmed_names:
        raise InvalidInput(f"{source.name} is grappled and can only make unarmed attacks")
    if source.surprised_turns > 0:
        raise InvalidInput(f"{source.name} is surprised and cannot act this turn")
    if source.kind != NPC_KIND:
        raise InvalidInput(f"{source.name} is a player character; use player_attack instead")
    _living(source, combat)
    struck = _participant(state, target)
    _living(struck, combat)
    roll_mode = _attack_roll_mode(source, struck, conditions, roll_mode)
    name = _attack_named(source, attack_name)
    distance = ranges.check_attack(
        state,
        combat,
        cell_feet=cell_feet,
        attacker=source,
        target=struck,
        profile=ranges.AttackRange.for_attack(source, name, combat),
    )
    placement = {"range": distance.as_dict()} if distance.cells else {}
    if source.large_group:
        if targets:
            raise InvalidInput(
                f"{source.name} is a group; its Blast hits every living individual on the other "
                "side, so leave targets out"
            )
        result = _blast(
            db,
            campaign_id,
            combat,
            rng,
            state,
            source,
            struck,
            name,
            modifier,
            plus=plus,
            spec=spec,
            hit_penalty=distance.penalty,
            damage_modifier=damage_modifier,
            roll_mode=roll_mode,
        )
        if state.encounter is not None:
            _mark_attack(db, campaign_id, source.name, weapon=name)
        return {**result, **placement}
    flags = AttackFlags.of(source.attack_flags.get(name, ()))
    if flags.hits_all_targets:
        result = _blast_attack(
            db,
            campaign_id,
            combat,
            rng,
            state,
            source,
            struck,
            name,
            targets,
            modifier,
            plus=plus,
            spec=spec,
            hit_penalty=distance.penalty,
            damage_modifier=damage_modifier,
            roll_mode=roll_mode,
        )
        if state.encounter is not None:
            _mark_attack(db, campaign_id, source.name, weapon=name)
        return {**result, **placement}
    if targets:
        raise InvalidInput(f"{name} has no Blast, so it hits one target; leave targets out")
    _require_impaired(struck, modifier_name(modifier), combat)
    dice = modifier_dice(combat.modifiers, modifier) or source.attacks[name]
    lucky = spec is not None and reactions.has_trigger(source, spec, None, "damage")
    damage_rolls, rolled = roll_values(dice, rng, plus=plus)
    if lucky:
        second_rolls, second = roll_values(dice, rng, plus=plus)
        if second > rolled:
            damage_rolls, rolled = second_rolls, second
    # Savage Attacker rolls the weapon's damage dice twice and keeps the better roll.
    if spec is not None and any(
        feature.weapon_damage_reroll
        for feature_id in source.features
        if (feature := spec.features.get(feature_id)) is not None
    ):
        second_rolls, second = roll_values(dice, rng, plus=plus)
        if second > rolled:
            damage_rolls, rolled = second_rolls, second
    damage_parts: list[tuple[str, int]] = []
    if combat.damage_bonus_stat:
        label = _stat_label(spec, combat.damage_bonus_stat) if spec is not None else ""
        damage_parts.append(
            (
                label or combat.damage_bonus_stat,
                _stat_bonus(combat, source, combat.damage_bonus_stat),
            )
        )
    if damage_modifier:
        damage_parts.append(("situational", damage_modifier))
    raw = max(
        0,
        rolled + _stat_bonus(combat, source, combat.damage_bonus_stat) + damage_modifier,
    )
    damage_breakdown = roll_breakdown(dice, damage_rolls, damage_parts, raw)
    shock_base, shock_ac = _attack_shock(source, name)
    shock_parts: list[tuple[str, int]] = []
    if distance.melee:
        shock_parts = [("Shock", shock_base)]
        if shock_modifier:
            shock_parts.append(("situational", shock_modifier))
    else:
        # A thrown or ranged attack never inflicts Shock, even on a miss.
        shock_ac = 0
    shock = sum(value for _, value in shock_parts)
    shock_breakdown = (
        roll_breakdown(
            "",
            [],
            shock_parts,
            shock,
            target=shock_ac,
            target_label="Shock AC",
        )
        if shock
        else None
    )
    hit_bonus = (
        _stat_bonus(combat, source, combat.hit_bonus_stat)
        + hit_modifier
        + distance.penalty
        + _condition_attack_modifier(source, conditions)
    )
    hit_parts: list[tuple[str, int]] = []
    if combat.hit_bonus_stat:
        # A statblock prints the creature's whole attack bonus, so naming the attack reads
        # correctly where the stat's own name, such as Proficiency Bonus, would not.
        hit_parts.append((name, _stat_bonus(combat, source, combat.hit_bonus_stat)))
    if hit_modifier:
        hit_parts.append(("situational", hit_modifier))
    if distance.penalty:
        hit_parts.append(("range", distance.penalty))
    condition_modifier = _condition_attack_modifier(source, conditions)
    if condition_modifier:
        hit_parts.append(("conditions", condition_modifier))
    to_hit: int | None = None
    natural: int | None = None
    hit_breakdown: dict[str, Any] | None = None
    if combat.hit_mode == "roll_vs_defense":
        hit_dice, natural = roll_mode_values(combat.check_die, rng, plus=plus, mode=roll_mode)
        to_hit = natural + hit_bonus
        hit_breakdown = roll_breakdown(
            combat.check_die,
            hit_dice,
            hit_parts,
            to_hit,
            target=struck.armor,
            target_label="AC",
        )
    critical = bool(
        combat.critical_dice
        and natural is not None
        and natural >= _critical_threshold(spec, source, combat)
    )
    if critical:
        extra_values, _ = roll_values(dice, rng, plus=plus)
        raw += sum(extra_values)
        damage_rolls += extra_values
        damage_breakdown = roll_breakdown(
            dice, damage_rolls, damage_parts, raw, note="critical hit"
        )
    result = _strike(
        db,
        campaign_id,
        combat,
        rng,
        struck,
        source.name,
        name,
        raw,
        modifier=modifier_name(modifier),
        plus=plus,
        ignores_armor=flags.ignores_armor,
        to_hit=to_hit,
        natural=natural,
        hit_bonus=hit_bonus,
        shock=shock,
        shock_ac=shock_ac,
        no_shock=not distance.melee,
        nonlethal=nonlethal,
        spec=spec,
        hit_breakdown=hit_breakdown,
        damage_breakdown=damage_breakdown,
        shock_breakdown=shock_breakdown,
        roll_mode=roll_mode,
        critical=critical,
    )
    if state.encounter is not None:
        _mark_attack(
            db,
            campaign_id,
            source.name,
            weapon=name,
            target=struck.name,
            hit=bool(result.get("hit", True)),
        )
    return {**result, **placement}


def _blast_attack(
    db: Session,
    campaign_id: int,
    combat: CombatRules,
    rng: random.Random,
    state: CampaignState,
    source: Character,
    struck: Character,
    name: str,
    targets: Sequence[str] | None,
    modifier: str,
    *,
    plus: str,
    spec: CharacterSpec | None = None,
    hit_penalty: int = 0,
    damage_modifier: int = 0,
    roll_mode: str = "normal",
) -> dict[str, Any]:
    """An NPC's attack with Blast. Each target in its area rolls separately. With no area given,
    the area is the named target alone, so its dice roll once. Against a group, blast damage is
    not impaired, so the attack rolls its own dice."""
    encounter = state.encounter
    assert encounter is not None
    own_side = encounter.sides.get(name_key(source.name), "")
    names = list(targets) if targets else [struck.name]
    keys = [name_key(n) for n in names]
    if len(set(keys)) != len(keys):
        raise InvalidInput(f"{name} lists a target twice")
    if name_key(struck.name) not in keys:
        raise InvalidInput(f"{struck.name} must be one of the targets of {name}")
    chosen = [_living(_participant(state, n), combat) for n in names]
    for target in chosen:
        if encounter.sides.get(name_key(target.name), "") == own_side:
            raise InvalidInput(f"{source.name} is on {target.name}'s side and cannot attack them")
    owed = [target.name for target in chosen if target.save_due is not None]
    if owed:
        raise InvalidInput(f"{', '.join(owed)} must make their save first")
    dice = modifier_dice(combat.modifiers, modifier) or source.attacks[name]
    damage_bonus = _stat_bonus(combat, source, combat.damage_bonus_stat)
    hit_bonus = _stat_bonus(combat, source, combat.hit_bonus_stat) + hit_penalty
    shock, shock_ac = _attack_shock(source, name)
    ignores_armor = AttackFlags.of(source.attack_flags.get(name, ())).ignores_armor
    results = []
    for target in chosen:
        # An earlier hit in the blast can break a later target's side, so each is checked as it comes up.
        current_target = load_state(db, campaign_id).characters[name_key(target.name)]
        if out_of_fight(current_target):
            continue
        raw = (
            _damage_total(
                dice,
                rng,
                plus=plus,
                lucky=spec is not None and reactions.has_trigger(source, spec, None, "damage"),
            )
            + damage_bonus
            + damage_modifier
        )
        results.append(
            _strike(
                db,
                campaign_id,
                combat,
                rng,
                current_target,
                source.name,
                name,
                raw,
                modifier=modifier_name(modifier),
                plus=plus,
                ignores_armor=ignores_armor,
                hit_bonus=hit_bonus,
                shock=shock,
                shock_ac=shock_ac,
                spec=spec,
                roll_mode=roll_mode,
            )
        )
    return {"attacker": source.name, "attack": name, "blast": True, "targets": results}


def _blast(
    db: Session,
    campaign_id: int,
    combat: CombatRules,
    rng: random.Random,
    state: CampaignState,
    source: Character,
    struck: Character,
    name: str,
    modifier: str,
    *,
    plus: str,
    spec: CharacterSpec | None = None,
    hit_penalty: int = 0,
    damage_modifier: int = 0,
    roll_mode: str = "normal",
) -> dict[str, Any]:
    """A group's attack, which has Blast: it rolls the rule set's blast dice separately for each living
    individual on the other side. The named target must be one of them. Every one is hit unless the
    rule set's groups say a blast hits only the named target."""
    encounter = state.encounter
    assert encounter is not None
    groups = combat.groups or GroupRules()
    own_side = encounter.sides.get(name_key(source.name), "")
    if encounter.sides.get(name_key(struck.name), "") == own_side:
        raise InvalidInput(f"{source.name} is on {struck.name}'s side and cannot attack them")
    if struck.large_group:
        raise InvalidInput(
            f"{source.name} is a group and cannot attack {struck.name}, another group"
        )
    if modifier_name(modifier) not in ("", groups.blast_modifier):
        raise InvalidInput(
            f"{source.name} is a group; its attacks are {groups.blast_modifier}, so leave the "
            "modifier out"
        )
    targets = [
        state.characters[name_key(display)]
        for display in encounter.order
        if encounter.sides.get(name_key(display), "") != own_side
        and not state.characters[name_key(display)].large_group
        and not out_of_fight(state.characters[name_key(display)])
    ]
    if not groups.blast_hits_all:
        targets = [target for target in targets if target.name == struck.name]
    if not targets:
        raise InvalidInput(
            f"there is no living individual on the other side for {source.name} to hit"
        )
    owed = [target.name for target in targets if target.save_due is not None]
    if owed:
        raise InvalidInput(f"{', '.join(owed)} must make their save first")
    dice = modifier_dice(combat.modifiers, groups.blast_modifier) or source.attacks[name]
    damage_bonus = _stat_bonus(combat, source, combat.damage_bonus_stat)
    hit_bonus = _stat_bonus(combat, source, combat.hit_bonus_stat) + hit_penalty
    shock, shock_ac = _attack_shock(source, name)
    ignores_armor = AttackFlags.of(source.attack_flags.get(name, ())).ignores_armor
    results = []
    for target in targets:
        # An earlier hit in the blast can break another target's side, so each is checked as it comes up.
        current_target = load_state(db, campaign_id).characters[name_key(target.name)]
        if out_of_fight(current_target):
            continue
        raw = (
            _damage_total(
                dice,
                rng,
                plus=plus,
                lucky=spec is not None and reactions.has_trigger(source, spec, None, "damage"),
            )
            + damage_bonus
            + damage_modifier
        )
        results.append(
            _strike(
                db,
                campaign_id,
                combat,
                rng,
                current_target,
                source.name,
                name,
                raw,
                modifier=groups.blast_modifier,
                plus=plus,
                ignores_armor=ignores_armor,
                hit_bonus=hit_bonus,
                shock=shock,
                shock_ac=shock_ac,
                spec=spec,
                roll_mode=roll_mode,
            )
        )
    return {"attacker": source.name, "attack": name, "blast": True, "targets": results}


def resolve_attacks(
    db: Session,
    campaign_id: int,
    target: str,
    attacks: Sequence[Mapping[str, Any]],
    combat: CombatRules,
    rng: random.Random,
    *,
    plus: str,
    conditions: Mapping[str, ConditionRules] | None = None,
    spec: CharacterSpec | None = None,
    force: bool = False,
    cell_feet: int | None = None,
    roll_mode: str = "normal",
) -> dict[str, Any]:
    """Several NPC attacks on one character, such as two NPCs of the foe side or one NPC's two
    weapons. Every attack rolls its own dice. Under the rule set's "highest" multi-attack the
    highest roll is the one hit and the rest are recorded; under "each" every attack hits in turn.
    Each attacker must be a living NPC on the foe side, every attack is checked before any die is
    rolled, and each attacker needs their own turn unless `force` overrides that. `cell_feet`
    turns on the rule set's reach, range, and sight limits for each attacker."""
    state = load_state(db, campaign_id)
    _no_initiative_pending(state)
    struck = _living(_participant(state, target), combat)
    roll_mode = _altitude_attack_mode(struck, conditions, roll_mode)
    if not attacks:
        raise InvalidInput("list at least one attack")
    encounter = state.encounter
    assert encounter is not None
    foe_side = encounter.sides.get(name_key(struck.name), "")
    # Each planned attack is the attacker, the attack, its modifier, its dice, and whether it ignores armor.
    planned: list[PlannedAttack] = []
    placements: list[dict[str, Any]] = []
    for entry in attacks:
        attacker_name = entry.get("attacker")
        attack_name = entry.get("attack")
        if not isinstance(attacker_name, str) or not isinstance(attack_name, str):
            raise InvalidInput("each attack needs an attacker and an attack name")
        source = _living(_participant(state, attacker_name), combat)
        if source.kind != NPC_KIND:
            raise InvalidInput(f"{source.name} is a player character; use player_attack instead")
        _require_attack(state, source, combat, force)
        if source.large_group:
            raise InvalidInput(f"{source.name} is a group; use attack, which has its Blast")
        if encounter.sides.get(name_key(source.name), "") == foe_side:
            raise InvalidInput(f"{source.name} is on {struck.name}'s side and cannot attack them")
        _require_impaired(struck, modifier_name(entry.get("modifier")), combat)
        name = _attack_named(source, attack_name)
        flags = AttackFlags.of(source.attack_flags.get(name, ()))
        if flags.hits_all_targets:
            raise InvalidInput(
                f"{source.name}'s {name} has Blast, which hits each target in its area; use "
                "attack with targets instead"
            )
        dice = modifier_dice(combat.modifiers, entry.get("modifier"))
        shock, shock_ac = _attack_shock(source, name)
        distance = ranges.check_attack(
            state,
            combat,
            cell_feet=cell_feet,
            attacker=source,
            target=struck,
            profile=ranges.AttackRange.for_attack(source, name, combat),
        )
        if distance.cells:
            placements.append({"attacker": source.name, "attack": name, **distance.as_dict()})
        if not distance.melee:
            # A thrown or ranged attack never inflicts Shock, even on a miss.
            shock, shock_ac = 0, 0
        else:
            shock += int(entry.get("shock_modifier", 0))
        planned.append(
            PlannedAttack(
                attacker=source.name,
                weapon=name,
                modifier=modifier_name(entry.get("modifier")),
                dice=dice or source.attacks[name],
                ignores_armor=flags.ignores_armor,
                nonlethal=bool(entry.get("nonlethal", False)),
                hit_bonus=_stat_bonus(combat, source, combat.hit_bonus_stat)
                + int(entry.get("hit_modifier", 0))
                + distance.penalty
                + _condition_attack_modifier(source, conditions),
                damage_bonus=_stat_bonus(combat, source, combat.damage_bonus_stat)
                + int(entry.get("damage_modifier", 0)),
                shock=shock,
                shock_ac=shock_ac,
                lucky_damage=spec is not None
                and reactions.has_trigger(source, spec, None, "damage"),
            )
        )
    rolls = [
        max(
            0,
            _damage_total(attack.dice, rng, plus=plus, lucky=attack.lucky_damage)
            + attack.damage_bonus,
        )
        for attack in planned
    ]
    if combat.multi_attack == "each":
        result = _strike_each(
            db,
            campaign_id,
            combat,
            rng,
            struck.name,
            planned,
            rolls,
            plus=plus,
            spec=spec,
            roll_mode=roll_mode,
        )
        if state.encounter is not None:
            _mark_attacks(db, campaign_id, [attack.attacker for attack in planned])
        return {**result, **({"ranges": placements} if placements else {})}
    best = highest_index(rolls)
    chosen = planned[best]
    rolled = ""
    if len(planned) > 1:
        rolled = rolls_text(
            [
                (attack.attacker, attack.weapon, attack.modifier, roll)
                for attack, roll in zip(planned, rolls)
            ]
        )
    result = _strike(
        db,
        campaign_id,
        combat,
        rng,
        struck,
        chosen.attacker,
        chosen.weapon,
        rolls[best],
        modifier=chosen.modifier,
        rolled=rolled,
        plus=plus,
        ignores_armor=chosen.ignores_armor,
        hit_bonus=chosen.hit_bonus,
        shock=chosen.shock,
        shock_ac=chosen.shock_ac,
        nonlethal=chosen.nonlethal,
        spec=spec,
        roll_mode=roll_mode,
    )
    if state.encounter is not None:
        _mark_attacks(db, campaign_id, [attack.attacker for attack in planned])
    return {**result, **({"ranges": placements} if placements else {})}


@dataclass(frozen=True)
class PlannedAttack:
    """One of several NPC attacks on the same target, before any of its dice are rolled."""

    attacker: str
    weapon: str
    modifier: str
    dice: str
    ignores_armor: bool = False
    nonlethal: bool = False
    hit_bonus: int = 0
    damage_bonus: int = 0
    shock: int = 0
    shock_ac: int = 0
    lucky_damage: bool = False


def _strike_each(
    db: Session,
    campaign_id: int,
    combat: CombatRules,
    rng: random.Random,
    target: str,
    planned: Sequence[PlannedAttack],
    rolls: Sequence[int],
    *,
    plus: str,
    spec: CharacterSpec | None = None,
    roll_mode: str = "normal",
) -> dict[str, Any]:
    """Apply each planned attack in turn, with the damage it already rolled. The attacks stop once
    the target is out of the fight or owes a save, and the result is the last hit, with every hit
    under "attacks"."""
    results = []
    for attack, raw in zip(planned, rolls):
        current_target = load_state(db, campaign_id).characters[name_key(target)]
        blocked = out_of_fight(current_target) or current_target.critical_due
        if blocked or current_target.save_due is not None:
            break
        results.append(
            _strike(
                db,
                campaign_id,
                combat,
                rng,
                current_target,
                attack.attacker,
                attack.weapon,
                raw,
                modifier=attack.modifier,
                plus=plus,
                ignores_armor=attack.ignores_armor,
                hit_bonus=attack.hit_bonus,
                shock=attack.shock,
                shock_ac=attack.shock_ac,
                nonlethal=attack.nonlethal,
                spec=spec,
                roll_mode=roll_mode,
            )
        )
    return {**results[-1], "attacks": results}


def attack_dice(
    db: Session,
    campaign_id: int,
    weapon: str,
    combat: CombatRules,
    modifier: str = "",
    two_handed: bool = False,
    spec: CharacterSpec | None = None,
) -> str:
    """The dice a player character's attack with `weapon` rolls: the modifier's dice when one is
    named, else the weapon's own. The weapon must be carried, or be unarmed."""
    state = load_state(db, campaign_id)
    return _attack_dice(
        _player_character(state),
        _weapon_name(weapon),
        modifier,
        combat,
        two_handed=two_handed,
        spec=spec,
    )


def player_attack(
    db: Session,
    campaign_id: int,
    target: str,
    weapon: str,
    combat: CombatRules,
    rng: random.Random,
    modifier: str = "",
    *,
    plus: str,
    spec: CharacterSpec | None = None,
    weapon_feature: ItemTemplate | None = None,
    attack_roll: int | None = None,
    damage_roll: int | None = None,
    hit_modifier: int = 0,
    damage_modifier: int = 0,
    shock_modifier: int = 0,
    conditions: Mapping[str, ConditionRules] | None = None,
    range_band: str = "",
    dual_wield: bool = False,
    force: bool = False,
    character: str = "",
    cell_feet: int | None = None,
    nonlethal: bool = False,
    two_handed: bool = False,
    roll_mode: str = "normal",
    mastery: str = "",
    extra: str = "",
    extra_from: str = "",
    gm_played: bool = False,
) -> dict[str, Any]:
    """A player character's hit. The engine rolls the damage unless the player's roll is passed as
    `damage_roll`. The hit mode decides whether it lands. A group takes only impaired attacks.
    When the player has already rolled the d20, pass it as `attack_roll`; the engine uses that
    natural roll. The character strikes on their own turn unless `force` overrides that. `character`
    names the player character or controlled hireling attacking; the campaign's primary player
    character is used when it is left out.

    A rule set with modifier bands and skills passes its `spec` and the weapon's `feature`. The
    hit roll then adds the rule set's hit bonus stat, the weapon skill (or its unskilled penalty),
    and the weapon's best attribute modifier; the damage adds the rule set's damage bonus stat,
    the attribute modifier when the rule set uses attribute damage, and the unarmed skill for an
    unarmed strike; and shock comes from the weapon plus the attribute modifier. `hit_modifier`,
    `damage_modifier`, and `shock_modifier` add bonuses the rule set does not compute itself, such
    as cover or a Focus. A ranged attack never inflicts Shock, so a thrown weapon's Shock reaches
    only a melee throw.

    `cell_feet` turns on the rule set's reach, range, and sight limits. A weapon's `feature`
    supplies its reach and ranged bands; without one the attack is melee at the rule set's reach.
    When the target lies beyond the reach, `range_band` names the band to use, or the nearest one
    that covers the distance is chosen.

    `mastery` replaces the weapon's mastery property for this attack when a feature such as
    Tactical Master allows it. `extra` marks an attack the rules grant after an earlier attack
    this turn: `cleave` needs a hit with the same weapon and names that first target in
    `extra_from`, while `light` needs a different Light weapon. Extra attacks are once per turn,
    spend no further attack action, leave a positive ability modifier out of their damage, and
    `light` spends the bonus action unless the weapon's mastery is Nick."""
    prepared = _prepare_player_attack(
        db,
        campaign_id,
        target,
        weapon,
        combat,
        modifier,
        spec=spec,
        weapon_feature=weapon_feature,
        damage_roll=damage_roll,
        range_band=range_band,
        dual_wield=dual_wield,
        force=force,
        character=character,
        cell_feet=cell_feet,
        two_handed=two_handed,
        mastery=mastery,
        extra=extra,
        extra_from=extra_from,
        gm_played=gm_played,
    )
    state, pc, struck = prepared.state, prepared.pc, prepared.struck
    mastery = prepared.mastery
    if "heavy" in (weapon_feature.flags if weapon_feature is not None else ()):
        stat = "dex" if weapon_feature is not None and weapon_feature.ranges else "str"
        if int(pc.stats.get(stat, 10)) < 13:
            roll_mode = "normal" if roll_mode == "advantage" else "disadvantage"
    roll_mode = _attack_roll_mode(pc, struck, conditions, roll_mode)
    attack_dice = prepared.dice
    if roll_mode == "advantage" and spec is not None:
        level = int(pc.counters.get("level", 1))
        for feature_id in pc.features:
            held = spec.features.get(feature_id)
            if held is None or not held.damage_dice_levels:
                continue
            extra = max(
                (formula for at, formula in held.damage_dice_levels.items() if at <= level),
                default="",
            )
            if extra:
                attack_dice = f"{attack_dice}+{extra}"
    damage_note = ""
    damage_rolls: list[int] = []
    if damage_roll is not None:
        raw = damage_roll
        damage_note = "the player's roll"
    else:
        damage_rolls, raw = roll_values(attack_dice, rng, plus=plus)
    if spec is not None and reactions.has_trigger(pc, spec, None, "damage"):
        second_rolls, second = roll_values(attack_dice, rng, plus=plus)
        if second > raw:
            raw, damage_rolls = second, second_rolls
            damage_note = ""
    damage_parts = list(prepared.bonuses.damage_parts)
    if prepared.dual_damage:
        damage_parts.append(("dual wielding", prepared.dual_damage))
    if damage_modifier:
        damage_parts.append(("situational", damage_modifier))
    raw = max(0, raw + prepared.bonuses.damage + prepared.dual_damage + damage_modifier)
    damage_breakdown = roll_breakdown(
        attack_dice, damage_rolls, damage_parts, raw, note=damage_note
    )
    # A thrown or ranged weapon never inflicts Shock, even on a miss, so only a melee attack
    # carries the weapon's Shock and any bonus that reaches it.
    ranged = not prepared.distance.melee or bool(range_band and not prepared.distance.cells)
    shock_parts: list[tuple[str, int]] = []
    if not ranged:
        shock_parts = list(prepared.bonuses.shock_parts)
        if shock_modifier:
            shock_parts.append(("situational", shock_modifier))
    shock = sum(value for _, value in shock_parts)
    shock_ac = prepared.bonuses.shock_ac if shock else 0
    shock_breakdown = (
        roll_breakdown(
            "",
            [],
            shock_parts,
            shock,
            target=shock_ac,
            target_label="Shock AC",
        )
        if shock
        else None
    )
    # The hit is rolled after the damage, so a roll_vs_defense rule set draws its dice in the same
    # order it always has.
    to_hit: int | None = None
    natural: int | None = None
    hit_breakdown: dict[str, Any] | None = None
    if combat.hit_mode == "roll_vs_defense":
        if attack_roll is not None:
            natural = attack_roll
            hit_dice = [natural]
        else:
            hit_dice, natural = roll_mode_values(combat.check_die, rng, plus=plus, mode=roll_mode)
        if spec is not None and reactions.has_trigger(pc, spec, None, "attack"):
            natural = max(natural, roll_total(combat.check_die, rng, plus=plus))
        hit_parts = list(prepared.bonuses.to_hit_parts)
        if hit_modifier:
            hit_parts.append(("situational", hit_modifier))
        if prepared.range_penalty:
            hit_parts.append(("range", prepared.range_penalty))
        if prepared.dual_penalty:
            hit_parts.append(("dual wielding", prepared.dual_penalty))
        condition_modifier = _condition_attack_modifier(pc, conditions)
        if condition_modifier:
            hit_parts.append(("conditions", condition_modifier))
        to_hit = natural + sum(value for _, value in hit_parts)
        hit_breakdown = roll_breakdown(
            combat.check_die,
            hit_dice,
            hit_parts,
            to_hit,
            target=struck.armor,
            target_label="AC",
        )
    critical = bool(
        combat.critical_dice
        and natural is not None
        and natural >= _critical_threshold(spec, pc, combat)
    )
    if critical:
        extra_values, _ = roll_values(attack_dice, rng, plus=plus)
        raw += sum(extra_values)
        damage_rolls += extra_values
        damage_breakdown = roll_breakdown(
            attack_dice, damage_rolls, damage_parts, raw, note="critical hit"
        )
    result = _strike(
        db,
        campaign_id,
        combat,
        rng,
        struck,
        pc.name,
        prepared.weapon,
        raw,
        modifier=modifier_name(modifier),
        plus=plus,
        to_hit=to_hit,
        natural=natural,
        shock=shock,
        shock_ac=shock_ac,
        nonlethal=nonlethal,
        damage_type=weapon_feature.damage_type if weapon_feature is not None else "",
        spec=spec,
        hit_breakdown=hit_breakdown,
        damage_breakdown=damage_breakdown,
        shock_breakdown=shock_breakdown,
        roll_mode=roll_mode,
        critical=critical,
        mastery=mastery,
        graze_ability=prepared.bonuses.attribute,
        mastery_dc=8 + int(pc.stats.get("attack_bonus", 2)) + prepared.bonuses.attribute,
    )
    if state.encounter is not None:
        _mark_attack(
            db,
            campaign_id,
            pc.name,
            weapon=weapon_feature.id if weapon_feature is not None else name_key(prepared.weapon),
            target=struck.name,
            extra=prepared.extra,
            bonus_action=prepared.extra_bonus_action,
            hit=result.get("hit", True) is not False,
        )
    placement = {"range": prepared.distance.as_dict()} if prepared.distance.cells else {}
    return {**result, **placement}


def player_attack_check(
    db: Session,
    campaign_id: int,
    target: str,
    weapon: str,
    combat: CombatRules,
    modifier: str = "",
    *,
    spec: CharacterSpec | None = None,
    weapon_feature: ItemTemplate | None = None,
    range_band: str = "",
    dual_wield: bool = False,
    force: bool = False,
    character: str = "",
    cell_feet: int | None = None,
    mastery: str = "",
    extra: str = "",
    extra_from: str = "",
) -> None:
    """Refuse a player attack the same way `player_attack` would, without rolling or recording
    anything. The engine calls it before showing the player's roll prompt, so an attack outside
    the character's turn, at a spent budget, out of reach, or with a weapon they cannot use is
    refused to the GM instead of being offered to the player as a roll that would fail."""
    _prepare_player_attack(
        db,
        campaign_id,
        target,
        weapon,
        combat,
        modifier,
        spec=spec,
        weapon_feature=weapon_feature,
        range_band=range_band,
        dual_wield=dual_wield,
        force=force,
        character=character,
        cell_feet=cell_feet,
        mastery=mastery,
        extra=extra,
        extra_from=extra_from,
    )


def require_attack(
    state: CampaignState,
    actor: Character,
    combat: CombatRules,
    force: bool = False,
    *,
    player_control: bool = False,
) -> None:
    """Refuse an attack action the way the attack tools do, without rolling: outside the
    actor's turn, or after the turn's attack actions are spent."""
    _require_attack(state, actor, combat, force, player_control=player_control)


@dataclass(frozen=True)
class _PreparedAttack:
    """A player attack that passed every check: who strikes whom with what, before any dice."""

    state: CampaignState
    pc: Character
    struck: Character
    weapon: str
    dice: str
    bonuses: WeaponBonuses
    distance: ranges.AttackDistance
    range_penalty: int
    dual_penalty: int
    dual_damage: int
    mastery: str
    extra: str
    extra_bonus_action: bool


def _prepare_player_attack(
    db: Session,
    campaign_id: int,
    target: str,
    weapon: str,
    combat: CombatRules,
    modifier: str,
    *,
    spec: CharacterSpec | None,
    weapon_feature: ItemTemplate | None,
    damage_roll: int | None = None,
    range_band: str = "",
    dual_wield: bool = False,
    force: bool = False,
    character: str = "",
    cell_feet: int | None = None,
    two_handed: bool = False,
    mastery: str = "",
    extra: str = "",
    extra_from: str = "",
    gm_played: bool = False,
) -> _PreparedAttack:
    """Every check `player_attack` makes before it rolls: the attacker may act now, carries the
    weapon, and can reach the target. Both the attack and its prompt check share it, so the
    player is only asked to roll for an attack that can happen."""
    state = load_state(db, campaign_id)
    _no_initiative_pending(state)
    pc = _player_character(state, character)
    encounter = state.encounter
    if (
        not gm_played
        and pc.hireling
        and encounter is not None
        and encounter.gm_played
        and name_key(encounter.acting or "") == name_key(pc.name)
    ):
        raise InvalidInput(f"the GM plays {pc.name}'s turn; use hireling_attack for their attack")
    override = mastery.strip().casefold()
    if override and override not in MASTERY_PROPERTIES:
        raise InvalidInput(f"there is no weapon mastery property named {mastery!r}")
    if pc.save_due is not None:
        # The engine waits for the player's own die when harm overflowed, so the fight pauses
        # until the GM asks for it rather than letting the character act while reeling.
        raise InvalidInput(f"{pc.name} must make their {pc.save_due} save first; call player_save")
    gm_hireling = bool(pc.hireling and gm_plays_hirelings(db, campaign_id))
    _require_attack(
        state, pc, combat, force, extra=extra, player_control=not (gm_played or gm_hireling)
    )
    if pc.grappled_by and name_key(weapon) not in combat.unarmed_names:
        raise InvalidInput(f"{pc.name} is grappled and can only make unarmed attacks")
    if pc.surprised_turns > 0:
        raise InvalidInput(f"{pc.name} is surprised and cannot act this turn")
    _living(pc, combat)
    if damage_roll is not None and damage_roll < 1:
        raise InvalidInput("the player's damage roll must be at least 1")
    struck = _participant(state, target)
    _living(struck, combat)
    name = _weapon_name(weapon)
    dice = _attack_dice(pc, name, modifier, combat, two_handed=two_handed, spec=spec)
    mastered = _mastered_property(spec, pc, weapon_feature, override=override)
    extra_kind = extra.strip().casefold()
    extra_bonus_action = (
        _check_extra_attack(
            state,
            pc,
            struck,
            name,
            weapon_feature,
            spec,
            mastered,
            extra_kind,
            extra_from,
            cell_feet,
            combat,
        )
        if extra_kind
        else False
    )
    if spec is not None and weapon_feature is not None:
        ranged = bool(weapon_feature.ranges)
        kind = "ranged" if ranged else "melee"
        for feature_id in pc.features:
            held = spec.features.get(feature_id)
            if held is None or not held.damage_dice:
                continue
            if held.weapon_kind and held.weapon_kind != kind:
                continue
            dice = f"{dice}+{held.damage_dice}"
    _require_impaired(struck, modifier_name(modifier), combat)
    bonuses = _player_bonuses(
        combat,
        pc,
        name,
        spec,
        weapon_feature,
        omit_attribute_damage=bool(extra_kind),
    )
    distance = ranges.check_attack(
        state,
        combat,
        cell_feet=cell_feet,
        attacker=pc,
        target=struck,
        profile=ranges.AttackRange.for_feature(weapon_feature, combat),
        band=range_band,
    )
    range_penalty = distance.penalty
    if not distance.cells and range_band:
        # Without placed positions there is no distance to check, so a named band keeps its old
        # meaning: its penalty applies to the hit roll.
        band = range_band.strip().casefold()
        if band not in combat.range_penalties:
            options = ", ".join(sorted(combat.range_penalties)) or "none"
            raise InvalidInput(f"there is no range band named {range_band!r}; choose {options}")
        range_penalty = combat.range_penalties[band]
    dual_penalty = 0
    dual_damage = 0
    if dual_wield:
        if not (combat.dual_wield_hit_penalty or combat.dual_wield_damage_bonus):
            raise InvalidInput("this rule set has no dual-wielding action")
        dual_penalty = combat.dual_wield_hit_penalty
        dual_damage = combat.dual_wield_damage_bonus
    return _PreparedAttack(
        state=state,
        pc=pc,
        struck=struck,
        weapon=name,
        dice=dice,
        bonuses=bonuses,
        distance=distance,
        range_penalty=range_penalty,
        dual_penalty=dual_penalty,
        dual_damage=dual_damage,
        mastery=mastered,
        extra=extra_kind,
        extra_bonus_action=extra_bonus_action,
    )


def _attack_roll_mode(
    source: Character,
    target: Character,
    conditions: Mapping[str, ConditionRules] | None,
    requested: str,
) -> str:
    """Choose the attack's roll mode from any explicit request and the conditions on both
    sides. An explicit advantage or disadvantage wins; otherwise the conditions decide."""
    if requested != "normal":
        return requested
    modes = [
        condition_roll_mode(source, conditions, "attack"),
        condition_roll_mode(target, conditions, "attack_against"),
    ]
    if source.marks.get(f"vex:{target.name}"):
        modes.append("advantage")
    if "advantage" in modes and "disadvantage" in modes:
        return "normal"
    return (
        "advantage"
        if "advantage" in modes
        else "disadvantage"
        if "disadvantage" in modes
        else "normal"
    )


def _altitude_attack_mode(
    target: Character, conditions: Mapping[str, ConditionRules] | None, requested: str
) -> str:
    """The target-side condition mode for a combined resolution where each attacker may differ."""
    if requested != "normal":
        return requested
    return condition_roll_mode(target, conditions, "attack_against")


def _condition_attack_modifier(
    character: Character, conditions: Mapping[str, ConditionRules] | None
) -> int:
    """What the character's conditions add to an attack roll, using their `attack` and wildcard
    modifiers."""
    if not conditions:
        return 0
    return sum(
        rules.modifies.get("attack", 0) + rules.modifies.get(ALL_STATS, 0)
        for rules in active_conditions(character, conditions)
    )


def _shield_round(db: Session, campaign_id: int, target: Character) -> int:
    """The round a shield can turn Shock aside, or 0 when the target has no shield or already
    used it this round."""
    encounter = load_state(db, campaign_id).encounter
    if encounter is None:
        return 0
    if target.shock_blocked_round == encounter.round:
        return 0
    if not target.equipped_shield or not any(
        "shield" in item.flags and item.ready and same_item_name(item.name, target.equipped_shield)
        for item in target.items
    ):
        return 0
    return encounter.round


def _record_shield_bash(db: Session, campaign_id: int, attacker: str, weapon: str) -> None:
    """A shield bash suspends the attacker's shield AC until the attacker's next turn."""
    name = name_key(weapon)
    if name.startswith(("shield bash", "shield_bash")):
        record(db, campaign_id, ShieldBashed(name=attacker))


def _player_character(state: CampaignState, name: str = "") -> Character:
    """The player character or player-controlled hireling making an attack. A campaign can have
    several player characters, so an attack may name the one acting; otherwise the campaign's
    primary player character is used."""
    if name:
        pc = state.characters.get(name_key(name))
        if pc is None:
            raise InvalidInput(f"there is no character named {name!r}")
        if pc.kind != PC_KIND:
            raise InvalidInput(f"{pc.name} is an NPC; use attack instead")
        return pc
    pc = state.pc()
    if pc is None:
        raise InvalidInput("there is no player character in this campaign")
    return pc


def _mark_attack(
    db: Session,
    campaign_id: int,
    attacker: str,
    *,
    weapon: str = "",
    target: str = "",
    extra: str = "",
    bonus_action: bool = False,
    hit: bool = True,
) -> None:
    """Record one attack action for the acting character. The event is hidden and feeds the
    turn's remaining-attack count; an NPC's multiattack still makes one attack action. An extra
    attack records its tag instead, and the weapon it used."""
    record(
        db,
        campaign_id,
        EncounterAttacked(
            name=attacker,
            weapon=weapon,
            target=target,
            hit=hit,
            extra=extra,
            bonus_action=bonus_action,
        ),
    )
    db.commit()


def _mark_attacks(db: Session, campaign_id: int, attackers: Sequence[str]) -> None:
    """Record one attack action for each distinct attacker in a combined resolution."""
    for attacker in dict.fromkeys(attackers):
        record(db, campaign_id, EncounterAttacked(name=attacker))
    db.commit()


def _require_attack(
    state: CampaignState,
    actor: Character,
    combat: CombatRules,
    force: bool,
    *,
    extra: str = "",
    player_control: bool = False,
) -> None:
    """Refuse an attack outside the actor's turn, or after the turn's attack actions are spent.
    The limit comes from the rule set's combat, so a rule set that grants more attack actions
    per turn works without engine changes. `force` is the story override the GM may use when
    the player insists. An extra attack is checked against its own once-per-turn rule instead
    of the attack-action budget. `player_control` says the player takes this character's turns,
    which lets the next-up character act in the reply that begins the encounter."""
    if combat.mode == "moves":
        return
    _require_turn(state, actor, force, player_control=player_control)
    if force or extra:
        return
    encounter = state.encounter
    if (
        encounter is not None
        and combat.actions_per_turn
        and encounter.attacked == 0
        and encounter.actions_used >= combat.actions_per_turn
    ):
        raise InvalidInput(
            f"{actor.name} has no action left this turn; end the turn before attacking again"
        )
    limit = combat.attacks_per_turn_for(actor)
    if encounter is None or encounter.attacked < limit:
        return
    if limit == 1:
        spent = f"{actor.name} has already attacked this turn"
    else:
        spent = f"{actor.name} has used {encounter.attacked} of {limit} attack actions this turn"
    plural = "" if limit == 1 else "s"
    raise InvalidInput(
        f"{spent}, and the rules allow {limit} attack action{plural} per turn; wait for the next "
        "turn, or pass force true when the player insists for the story"
    )


def _require_turn(
    state: CampaignState, actor: Character, force: bool, *, player_control: bool = False
) -> None:
    """Refuse an attack the attacker has no turn for. An encounter runs on turns: a player
    character's turn starts automatically, an NPC's or a GM-played hireling's starts when the
    player presses Start, and a turn ends when the player ends it or, for an NPC, when the GM's
    reply finishes. That makes a turn the only time an actor may strike, so nobody can cut in
    ahead of their place in the order. `force` lets the GM resolve an action the story needs
    outside the order, such as an ambush that takes the party unawares."""
    if force:
        return
    error = acting_error(state, actor.name, player_control=player_control, action="attack")
    if error is not None:
        raise InvalidInput(error)


def _weapon_name(weapon: str) -> str:
    name = weapon.strip()
    if not name:
        raise InvalidInput("weapon must name the attack")
    return name


def _attack_dice(
    pc: Character,
    weapon: str,
    modifier: str,
    combat: CombatRules,
    *,
    two_handed: bool = False,
    spec: CharacterSpec | None = None,
) -> str:
    held = next((item for item in pc.items if same_item_name(item.name, weapon)), None)
    if held is not None:
        if not held.damage:
            raise InvalidInput(f"{held.name} is not a weapon: it has no damage dice")
        if not held.ready:
            raise InvalidInput(f"{held.name} is stowed; ready it with ready_item before attacking")
        weapon_dice = held.damage
        if two_handed and spec is not None:
            weapon = next(
                (
                    entry
                    for entry in spec.items.values()
                    if same_item_name(entry.name, held.name) and entry.versatile_damage
                ),
                None,
            )
            if weapon is not None:
                weapon_dice = weapon.versatile_damage
    elif name_key(weapon) in combat.unarmed_names:
        weapon_dice = combat.unarmed_damage
    else:
        raise InvalidInput(
            f"{pc.name} carries no weapon named {weapon!r}; give it with give_item first, "
            "naming its damage if it is not in the catalogue"
        )
    return modifier_dice(combat.modifiers, modifier) or weapon_dice
