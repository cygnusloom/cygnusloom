"""Combat: hits, armor, the harm they cause, and the saves they force. The rule set's combat module
decides what a save means and how initiative is ordered. This service checks each step against the
encounter and records what happened.

Like the rest of the world service, each function loads the campaign's state, validates the
change, records its events, and commits."""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from sqlmodel import Session

from cygnusforge.content.character import CharacterSpec, Feature
from cygnusforge.content.library import (
    ALL_STATS,
    CombatRules,
    ConditionRules,
    DamageStep,
    GroupRules,
)
from cygnusforge.rules.dice import roll_total
from cygnusforge.services import death, resolution
from cygnusforge.services.attacks import (
    BLAST,
    IGNORES_ARMOR,
    highest_index,
    modifier_dice,
    modifier_name,
    rolls_text,
)
from cygnusforge.services.common import InvalidInput, bounded_text, whole_number
from cygnusforge.services.conditions import active_conditions, end_condition
from cygnusforge.services.encounters import (
    _participant,
    awaiting_initiative,
    initiative_open,
    settle_initiative,
)
from cygnusforge.services.events import (
    NPC_KIND,
    PC_KIND,
    ArmorSet,
    CampaignState,
    Character,
    CharacterDied,
    CharacterHealed,
    CharacterRecovered,
    CharacterSurrendered,
    DebugCurrencyAdded,
    DebugStatSet,
    DefenseEquipmentSet,
    EncounterEnded,
    GroupBreakResolved,
    Harmed,
    InitiativeRolled,
    InvincibilitySet,
    SaveRolled,
    Struck,
    current,
    defeat_status,
    load_state,
    name_key,
    out_of_fight,
    record,
    same_item_name,
)
from cygnusforge.services.injuries import EffectContext, gain_injury

MAX_DAMAGE = 1000
# Sanity bound on armor when the rule set sets no cap (armor_max of 0).
UNCAPPED_ARMOR = 1000
MAX_RECOVERY_TEXT = 600
MAX_CAUSE_TEXT = 200
# A weapon feature flag: the weapon can be used without the unskilled attack penalty, as the free
# Worlds Without Number rules say of crossbows.
NO_UNSKILLED_PENALTY = "no_unskilled_penalty"


def armor_after(current: int, listed: int, mode: str, defense: int = 0) -> int:
    """Combine an armor value with the current defense according to its configured mode."""
    if mode == "set":
        return defense + listed
    if mode == "max":
        return max(current, defense + listed)
    if mode == "shield":
        if current <= defense:
            return defense + listed
        return max(current + 1, defense + listed)
    return current + listed


def equipped_defense(character: Character, spec: CharacterSpec, base: int, modifier: str) -> int:
    """Calculate defense from passive floors, worn armor, a wielded shield, and adjustments."""
    bonus = spec.modifier(character.stats, modifier) if modifier else 0
    unarmored = base + bonus
    floor = max(base, character.innate_defense)
    for feature_id, count in character.features.items():
        feature = spec.features.get(feature_id)
        if count <= 0 or feature is None or feature.defense_floor is None:
            continue
        rule = feature.defense_floor
        if rule.requires_no_armor and (character.equipped_armor or character.equipped_armor_value):
            continue
        if rule.requires_no_shield and (
            character.equipped_shield or character.equipped_shield_value
        ):
            continue
        value = rule.base
        if rule.counter:
            counter = spec.counters.get(rule.counter)
            if counter is None:
                raise InvalidInput(f"defense floor names no counter {rule.counter!r}")
            amount = character.counters.get(rule.counter, counter.initial)
            value += -(-amount // rule.divisor) if rule.round_up else amount // rule.divisor
        floor = max(floor, value)
    armor = max(unarmored + character.equipped_armor_value, floor + bonus)
    if character.equipped_shield_value:
        armor = armor_after(armor, character.equipped_shield_value, "shield", unarmored)
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
) -> int:
    """Change either equipped defense slot or its adjustment, then record the resulting AC."""
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
        bonus = (
            spec.modifier(character.stats, rules.defense_modifier) if rules.defense_modifier else 0
        )
        innate = max(0, character.armor - bonus)
    record(
        db,
        campaign_id,
        DefenseEquipmentSet(
            name=character.name,
            equipped_armor=body[0],
            equipped_armor_value=body[1],
            equipped_shield=held_shield[0],
            equipped_shield_value=held_shield[1],
            innate_defense=innate,
            defense_adjustment=(character.defense_adjustment if adjustment is None else adjustment),
        ),
    )
    return recalculate_defense(db, campaign_id, character.name, spec, rules)


@dataclass(frozen=True)
class WeaponBonuses:
    """What a player character's weapon adds to an attack: `to_hit` to the hit roll, `damage` to
    the hit's own damage, and the `shock` a miss does through armor of `shock_ac` or less."""

    to_hit: int = 0
    damage: int = 0
    shock: int = 0
    shock_ac: int = 0


@dataclass(frozen=True)
class Damage:
    """What a hit did: the damage armor absorbed, the harm by stat, and the stat a save is owed on."""

    absorbed: int
    harm: dict[str, int]
    save: str | None


def apply_damage(
    steps: Sequence[DamageStep],
    armor: int,
    current_of: Callable[[str], int],
    raw: int,
) -> Damage:
    """Run a hit's damage through the rule set's steps in order. `absorb` takes up to `armor` off
    the hit. `harm` takes up to the stat's current score. `overflow` takes the rest and owes a save
    on its stat. Damage left after the last step has no effect."""
    absorbed = 0
    harm: dict[str, int] = {}
    save: str | None = None
    dealt = raw
    for step in steps:
        if step.kind == "absorb":
            absorbed = min(armor, dealt)
            dealt -= absorbed
        elif step.kind == "harm":
            assert step.stat is not None
            taken = min(dealt, current_of(step.stat))
            if taken:
                harm[step.stat] = taken
            dealt -= taken
        else:
            assert step.stat is not None
            if dealt:
                harm[step.stat] = dealt
                save = step.stat
            dealt = 0
    return Damage(absorbed=absorbed, harm=harm, save=save)


def _living(character: Character, combat: CombatRules) -> Character:
    if character.story_only:
        raise InvalidInput(
            f"{character.name} has no stats yet; give them stats with update_npc first"
        )
    if character.defeated:
        raise InvalidInput(f"{character.name} is {defeat_status(character, combat.defeat)}")
    if character.fled:
        raise InvalidInput(f"{character.name} has fled the fight")
    if character.surrendered:
        raise InvalidInput(f"{character.name} has surrendered")
    if character.critical_due:
        raise InvalidInput(
            f"{character.name} is broken and awaits the GM's decision; call resolve_group_break"
        )
    return character


def _require_impaired(target: Character, modifier: str, combat: CombatRules) -> None:
    """A group is hit only by the rule set's incoming modifier. A rule set with no groups block
    places no such limit."""
    if combat.groups is None or not target.large_group:
        return
    required = combat.groups.incoming_modifier
    if modifier != required:
        raise InvalidInput(f"{target.name} is a group; attacks on it must be {required}")


def _stat_bonus(combat: CombatRules, character: Character, stat: str) -> int:
    """What one of the rule set's attack stats adds, such as a hit or damage bonus. A rule set
    that names no stat adds nothing."""
    return current(character, stat) if stat else 0


def _attribute_modifier(
    spec: CharacterSpec | None, character: Character, attributes: Sequence[str]
) -> int:
    """The best of the weapon's attribute modifiers, or 0 when the rule set has no attributes."""
    if spec is None:
        return 0
    scores = [
        spec.modifier(character.stats, stat) for stat in attributes if stat in character.stats
    ]
    return max(scores) if scores else 0


def _weapon_skill(combat: CombatRules, weapon: str, feature: Feature | None) -> str:
    """The skill an attack with `weapon` rolls: the weapon's own when it has one, the unarmed
    skill for an unarmed strike, and no skill for a rule set with neither."""
    if feature is not None and feature.skill:
        return feature.skill
    if name_key(weapon) in combat.unarmed_names:
        return combat.unarmed_skill
    return ""


def _player_bonuses(
    combat: CombatRules,
    character: Character,
    weapon: str,
    spec: CharacterSpec | None,
    feature: Feature | None,
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
    attribute = _attribute_modifier(spec, character, attributes)
    damage_bonus = _stat_bonus(combat, character, combat.damage_bonus_stat)
    to_hit = _stat_bonus(combat, character, combat.hit_bonus_stat) + attribute
    damage = damage_bonus
    class_bonus = _level_damage_bonus(combat, character, spec)
    damage += class_bonus
    skill = _weapon_skill(combat, weapon, feature)
    if skill and skill in spec.skills:
        level = spec.skill_level(character.skills, skill)
        if spec.skill_trained(character.skills, skill):
            to_hit += level
            if unarmed:
                damage += level
        elif not (feature is not None and NO_UNSKILLED_PENALTY in feature.flags):
            to_hit -= combat.unskilled_attack_penalty
    if combat.attribute_damage and attributes:
        damage += attribute
    shock = 0
    shock_ac = 0
    if feature is not None and feature.shock:
        shock = feature.shock + attribute + damage_bonus + class_bonus
        shock_ac = feature.shock_ac
    return WeaponBonuses(to_hit=to_hit, damage=damage, shock=shock, shock_ac=shock_ac)


def _level_damage_bonus(combat: CombatRules, character: Character, spec: CharacterSpec) -> int:
    """The damage the configured template's counter adds, rounded up by its divisor."""
    rules = combat.level_damage
    if rules is None:
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


def _harm_stat(combat: CombatRules) -> str | None:
    """The first stat a hit harms, which is the one shock and defeat are read from."""
    return next((step.stat for step in combat.damage if step.kind == "harm"), None)


def _defeat_after_harm(
    combat: CombatRules, target: Character, harm: Mapping[str, int]
) -> tuple[bool, str]:
    """Whether this harm took a stat the rule set leaves a character defeated at 0 down to 0, and
    where that defeat leaves the character."""
    if target.invincible:
        return False, ""
    for stat in combat.defeat_at_zero:
        if current(target, stat) - harm.get(stat, 0) <= 0:
            return True, defeat_status(target, combat.defeat)
    return False, ""


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
    spec: CharacterSpec | None = None,
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
    if target.save_due is not None:
        raise InvalidInput(f"{target.name} must make their {target.save_due} save first")
    defense = 0 if ignores_armor else target.armor
    shock_applies = bool(shock) and defense <= shock_ac
    if combat.hit_mode == "roll_vs_defense":
        if to_hit is None:
            natural = roll_total(combat.check_die, rng, plus=plus)
            to_hit = natural + hit_bonus
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
            defeats, status = _defeat_after_harm(combat, target, harm)
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
                ),
            )
            db.commit()
            if defeats:
                if target.kind == PC_KIND or target.important:
                    death.begin_dying(db, campaign_id, combat, target.name)
                _end_if_one_side_left(db, campaign_id)
            return {
                "target": target.name,
                "raw": raw,
                "armor": 0,
                "harm": harm,
                "save_due": None,
                "hit": False,
                "to_hit": to_hit,
                "defeated": load_state(db, campaign_id).characters[name_key(target.name)].defeated,
            }
    armor = 0 if ignores_armor else target.armor
    if shock_applies and combat.minimum_shock_damage:
        raw = max(raw, shock)
    damage = apply_damage(combat.damage, armor, lambda stat: current(target, stat), raw)
    if target.invincible:
        # The whole hit is absorbed, so the log reads as a blow turned aside rather than a miss.
        damage = Damage(absorbed=raw, harm={}, save=None)
    defeats, status = _defeat_after_harm(combat, target, damage.harm)
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
        ),
    )
    db.commit()
    if defeats and (target.kind == PC_KIND or target.important):
        death.begin_dying(db, campaign_id, combat, target.name)
    result: dict[str, Any] = {
        "target": target.name,
        "raw": raw,
        "armor": damage.absorbed,
        "harm": damage.harm,
        "save_due": damage.save,
    }
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
        from cygnusforge.services.morale import check_morale

        checks = check_morale(db, campaign_id, combat, rng, target.name, plus=plus)
        if checks:
            result["morale"] = checks
    result["defeated"] = load_state(db, campaign_id).characters[name_key(target.name)].defeated
    _end_if_one_side_left(db, campaign_id)
    return result


def _injury_from_hit(
    db: Session,
    campaign_id: int,
    combat: CombatRules,
    rng: random.Random,
    name: str,
    damage: Damage,
    *,
    plus: str,
    spec: CharacterSpec | None = None,
) -> str | None:
    """Apply the configured injury trigger to eligible targets, using harm as the table index."""
    table = combat.injury_table
    if table is None:
        return None
    lost = damage.harm.get(table.stat, 0)
    if lost == 0:
        return None
    character = load_state(db, campaign_id).characters[name_key(name)]
    if character.kind not in table.target_kinds:
        return None
    if table.trigger == "depleted" and current(character, table.stat) != 0:
        return None
    roll = min(lost, len(table.entries))
    entry = gain_injury(
        db,
        campaign_id,
        name,
        table,
        roll,
        EffectContext(rng=rng, combat=combat, spec=spec),
        plus=plus,
    )
    return entry.name


def _end_if_one_side_left(db: Session, campaign_id: int) -> None:
    """End the encounter once only one side still has a character who can fight. The check
    needs at least two sides in the encounter, so a fight among one side is left alone."""
    state = load_state(db, campaign_id)
    encounter = state.encounter
    if encounter is None:
        return
    sides: dict[str, list[Character]] = {}
    for display in encounter.order:
        character = state.characters[name_key(display)]
        side = encounter.sides.get(name_key(character.name), "")
        sides.setdefault(side, []).append(character)
    if len(sides) < 2:
        return
    standing = [
        side for side, members in sides.items() if any(not out_of_fight(m) for m in members)
    ]
    if len(standing) > 1:
        return
    if standing:
        reason = f"Only the {standing[0]} side can still fight."
    else:
        reason = "Nobody is left able to fight."
    record(db, campaign_id, EncounterEnded(reason=reason))
    db.commit()


def surrender(db: Session, campaign_id: int, name: str, combat: CombatRules) -> dict[str, Any]:
    """A character gives up the fight. The encounter ends once one side has nobody who can fight."""
    state = load_state(db, campaign_id)
    if state.encounter is None:
        raise InvalidInput("there is no encounter to surrender in")
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    if name_key(character.name) not in state.encounter.sides:
        raise InvalidInput(f"{character.name} is not in the encounter")
    _living(character, combat)
    record(db, campaign_id, CharacterSurrendered(name=character.name))
    db.commit()
    _end_if_one_side_left(db, campaign_id)
    return {"character": character.name, "surrendered": True}


def resolve_save(
    combat: CombatRules,
    score: int,
    *,
    rng: random.Random | None = None,
    roll: int | None = None,
    plus: str = "sum",
) -> resolution.RollResult:
    """Resolve a save against `score` through one rules-aware path.

    A rule set whose Python module supplies `save_succeeds` owns the comparison; every other rule
    set uses the shared resolution primitive with its declarative save mode and automatic rolls.
    `roll` is a roll the player reported, used as it is; when it is None the engine rolls the rule
    set's check die with `rng`."""
    module = combat.module
    if module is not None and callable(getattr(module, "save_succeeds", None)):
        made = (
            roll
            if roll is not None
            else resolution.roll_die(_required_rng(rng), combat.check_die, plus=plus)
        )
        return resolution.RollResult(
            roll=made, score=score, passed=bool(module.save_succeeds(made, score))
        )
    if roll is not None:
        return resolution.resolve_roll(
            roll,
            score,
            mode=combat.save.mode,
            auto_success=combat.save.auto_success,
            auto_fail=combat.save.auto_fail,
        )
    return resolution.roll_score(
        _required_rng(rng),
        combat.check_die,
        score,
        mode=combat.save.mode,
        auto_success=combat.save.auto_success,
        auto_fail=combat.save.auto_fail,
        plus=plus,
    )


def _required_rng(rng: random.Random | None) -> random.Random:
    """The caller's random source. A caller that supplied the player's roll needs none."""
    if rng is None:
        raise ValueError("resolving a save needs either the player's roll or a random source")
    return rng


def _save(
    db: Session,
    campaign_id: int,
    combat: CombatRules,
    rng: random.Random,
    character: Character,
    stat: str,
    *,
    plus: str,
) -> dict[str, Any]:
    result = resolve_save(combat, current(character, stat), rng=rng, plus=plus)
    critical = character.large_group
    record(
        db,
        campaign_id,
        SaveRolled(
            name=character.name,
            stat=stat,
            roll=result.roll,
            score=result.score,
            passed=result.passed,
            status="" if critical else defeat_status(character, combat.defeat),
            critical=critical,
        ),
    )
    db.commit()
    if not result.passed and not critical and character.kind == PC_KIND:
        death.on_critical_save_failed(db, campaign_id, combat, character.name)
        db.commit()
    return {
        "stat": stat,
        "roll": result.roll,
        "score": result.score,
        "passed": result.passed,
    }


def _no_initiative_pending(state: CampaignState) -> None:
    if initiative_open(state):
        waiting = ", ".join(awaiting_initiative(state))
        raise InvalidInput(f"initiative is not set yet; {waiting} must roll for it first")


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
    conditions: Mapping[str, ConditionRules] | None = None,
    spec: CharacterSpec | None = None,
) -> dict[str, Any]:
    """An NPC's attack. Under the auto hit mode it always hits, so the attack's damage is the roll.
    A modifier rolls its own dice instead of the attack's. An attack with Blast hits every target in
    its area, which is `targets` (it must include `target`), and each target rolls separately. Who
    acts when is the turn order's job; this only checks the attacker has the attack and both are
    living."""
    state = load_state(db, campaign_id)
    _no_initiative_pending(state)
    source = _participant(state, attacker)
    if source.kind != NPC_KIND:
        raise InvalidInput(f"{source.name} is a player character; use player_attack instead")
    _living(source, combat)
    struck = _participant(state, target)
    _living(struck, combat)
    name = _attack_named(source, attack_name)
    if source.large_group:
        if targets:
            raise InvalidInput(
                f"{source.name} is a group; its Blast hits every living individual on the other "
                "side, so leave targets out"
            )
        return _blast(
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
        )
    flags = source.attack_flags.get(name, [])
    if BLAST in flags:
        return _blast_attack(
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
        )
    if targets:
        raise InvalidInput(f"{name} has no Blast, so it hits one target; leave targets out")
    _require_impaired(struck, modifier_name(modifier), combat)
    dice = modifier_dice(combat.modifiers, modifier)
    raw = roll_total(dice or source.attacks[name], rng, plus=plus)
    raw += _stat_bonus(combat, source, combat.damage_bonus_stat)
    shock, shock_ac = _attack_shock(source, name)
    return _strike(
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
        ignores_armor=IGNORES_ARMOR in flags,
        hit_bonus=_stat_bonus(combat, source, combat.hit_bonus_stat)
        + hit_modifier
        + _condition_attack_modifier(source, conditions),
        shock=shock,
        shock_ac=shock_ac,
        spec=spec,
    )


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
    hit_bonus = _stat_bonus(combat, source, combat.hit_bonus_stat)
    shock, shock_ac = _attack_shock(source, name)
    results = []
    for target in chosen:
        # An earlier hit in the blast can break a later target's side, so each is checked as it comes up.
        current_target = load_state(db, campaign_id).characters[name_key(target.name)]
        if out_of_fight(current_target):
            continue
        raw = roll_total(dice, rng, plus=plus) + damage_bonus
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
                ignores_armor=IGNORES_ARMOR in source.attack_flags.get(name, []),
                hit_bonus=hit_bonus,
                shock=shock,
                shock_ac=shock_ac,
                spec=spec,
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
    hit_bonus = _stat_bonus(combat, source, combat.hit_bonus_stat)
    shock, shock_ac = _attack_shock(source, name)
    results = []
    for target in targets:
        # An earlier hit in the blast can break another target's side, so each is checked as it comes up.
        current_target = load_state(db, campaign_id).characters[name_key(target.name)]
        if out_of_fight(current_target):
            continue
        raw = roll_total(dice, rng, plus=plus) + damage_bonus
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
                ignores_armor=IGNORES_ARMOR in source.attack_flags.get(name, []),
                hit_bonus=hit_bonus,
                shock=shock,
                shock_ac=shock_ac,
                spec=spec,
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
) -> dict[str, Any]:
    """Several NPC attacks on one character, such as two NPCs of the foe side or one NPC's two
    weapons. Every attack rolls its own dice. Under the rule set's "highest" multi-attack the
    highest roll is the one hit and the rest are recorded; under "each" every attack hits in turn.
    Each attacker must be a living NPC on the foe side, and every attack is checked before any die
    is rolled."""
    state = load_state(db, campaign_id)
    _no_initiative_pending(state)
    struck = _living(_participant(state, target), combat)
    if not attacks:
        raise InvalidInput("list at least one attack")
    encounter = state.encounter
    assert encounter is not None
    foe_side = encounter.sides.get(name_key(struck.name), "")
    # Each planned attack is the attacker, the attack, its modifier, its dice, and whether it ignores armor.
    planned: list[PlannedAttack] = []
    for entry in attacks:
        attacker_name = entry.get("attacker")
        attack_name = entry.get("attack")
        if not isinstance(attacker_name, str) or not isinstance(attack_name, str):
            raise InvalidInput("each attack needs an attacker and an attack name")
        source = _living(_participant(state, attacker_name), combat)
        if source.kind != NPC_KIND:
            raise InvalidInput(f"{source.name} is a player character; use player_attack instead")
        if source.large_group:
            raise InvalidInput(f"{source.name} is a group; use attack, which has its Blast")
        if encounter.sides.get(name_key(source.name), "") == foe_side:
            raise InvalidInput(f"{source.name} is on {struck.name}'s side and cannot attack them")
        _require_impaired(struck, modifier_name(entry.get("modifier")), combat)
        name = _attack_named(source, attack_name)
        flags = source.attack_flags.get(name, [])
        if BLAST in flags:
            raise InvalidInput(
                f"{source.name}'s {name} has Blast, which hits each target in its area; use "
                "attack with targets instead"
            )
        dice = modifier_dice(combat.modifiers, entry.get("modifier"))
        shock, shock_ac = _attack_shock(source, name)
        planned.append(
            PlannedAttack(
                attacker=source.name,
                weapon=name,
                modifier=modifier_name(entry.get("modifier")),
                dice=dice or source.attacks[name],
                ignores_armor=IGNORES_ARMOR in flags,
                hit_bonus=_stat_bonus(combat, source, combat.hit_bonus_stat)
                + int(entry.get("hit_modifier", 0))
                + _condition_attack_modifier(source, conditions),
                damage_bonus=_stat_bonus(combat, source, combat.damage_bonus_stat),
                shock=shock,
                shock_ac=shock_ac,
            )
        )
    rolls = [roll_total(attack.dice, rng, plus=plus) + attack.damage_bonus for attack in planned]
    if combat.multi_attack == "each":
        return _strike_each(
            db, campaign_id, combat, rng, struck.name, planned, rolls, plus=plus, spec=spec
        )
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
    return _strike(
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
        spec=spec,
    )


@dataclass(frozen=True)
class PlannedAttack:
    """One of several NPC attacks on the same target, before any of its dice are rolled."""

    attacker: str
    weapon: str
    modifier: str
    dice: str
    ignores_armor: bool = False
    hit_bonus: int = 0
    damage_bonus: int = 0
    shock: int = 0
    shock_ac: int = 0


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
                spec=spec,
            )
        )
    return {**results[-1], "attacks": results}


def attack_dice(
    db: Session, campaign_id: int, weapon: str, combat: CombatRules, modifier: str = ""
) -> str:
    """The dice a player character's attack with `weapon` rolls: the modifier's dice when one is
    named, else the weapon's own. The weapon must be carried, or be unarmed."""
    state = load_state(db, campaign_id)
    return _attack_dice(_player_character(state), _weapon_name(weapon), modifier, combat)


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
    weapon_feature: Feature | None = None,
    attack_roll: int | None = None,
    damage_roll: int | None = None,
    hit_modifier: int = 0,
    conditions: Mapping[str, ConditionRules] | None = None,
) -> dict[str, Any]:
    """A player character's hit. The engine rolls the damage unless the player's roll is passed as
    `damage_roll`. The hit mode decides whether it lands. A group takes only impaired attacks.
    When the player has already rolled the d20, pass it as `attack_roll`; the engine uses that
    natural roll.

    A rule set with modifier bands and skills passes its `spec` and the weapon's `feature`. The
    hit roll then adds the rule set's hit bonus stat, the weapon skill (or its unskilled penalty),
    and the weapon's best attribute modifier; the damage adds the rule set's damage bonus stat,
    the attribute modifier when the rule set uses attribute damage, and the unarmed skill for an
    unarmed strike; and shock comes from the weapon plus the attribute and damage bonuses."""
    state = load_state(db, campaign_id)
    _no_initiative_pending(state)
    pc = _player_character(state)
    _living(pc, combat)
    if damage_roll is not None and damage_roll < 1:
        raise InvalidInput("the player's damage roll must be at least 1")
    struck = _participant(state, target)
    _living(struck, combat)
    name = _weapon_name(weapon)
    dice = _attack_dice(pc, name, modifier, combat)
    _require_impaired(struck, modifier_name(modifier), combat)
    raw = damage_roll if damage_roll is not None else roll_total(dice, rng, plus=plus)
    bonuses = _player_bonuses(combat, pc, name, spec, weapon_feature)
    raw += bonuses.damage
    # The hit is rolled after the damage, so a roll_vs_defense rule set draws its dice in the same
    # order it always has.
    to_hit: int | None = None
    natural: int | None = None
    if combat.hit_mode == "roll_vs_defense":
        natural = (
            attack_roll if attack_roll is not None else roll_total(combat.check_die, rng, plus=plus)
        )
        to_hit = (
            natural + bonuses.to_hit + hit_modifier + _condition_attack_modifier(pc, conditions)
        )
    return _strike(
        db,
        campaign_id,
        combat,
        rng,
        struck,
        pc.name,
        name,
        raw,
        modifier=modifier_name(modifier),
        plus=plus,
        to_hit=to_hit,
        natural=natural,
        shock=bonuses.shock,
        shock_ac=bonuses.shock_ac,
        spec=spec,
    )


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


def _player_character(state: CampaignState) -> Character:
    pc = state.pc()
    if pc is None:
        raise InvalidInput("there is no player character in this campaign")
    return pc


def _weapon_name(weapon: str) -> str:
    name = weapon.strip()
    if not name:
        raise InvalidInput("weapon must name the attack")
    return name


def _attack_dice(pc: Character, weapon: str, modifier: str, combat: CombatRules) -> str:
    held = next((item for item in pc.items if same_item_name(item.name, weapon)), None)
    if held is not None:
        if not held.damage:
            raise InvalidInput(f"{held.name} is not a weapon: it has no damage dice")
        if not held.ready:
            raise InvalidInput(f"{held.name} is stowed; ready it with ready_item before attacking")
        weapon_dice = held.damage
    elif name_key(weapon) in combat.unarmed_names:
        weapon_dice = combat.unarmed_damage
    else:
        raise InvalidInput(
            f"{pc.name} carries no weapon named {weapon!r}; give it with give_item first, "
            "naming its damage if it is not in the catalogue"
        )
    return modifier_dice(combat.modifiers, modifier) or weapon_dice


def resolve_group_break(
    db: Session, campaign_id: int, name: str, outcome: str, combat: CombatRules
) -> dict[str, Any]:
    """The GM's decision after a group's Critical Damage save failed. "defeat" breaks and defeats it,
    "flee" breaks it up so its members run from the fight. Until this is called, the group cannot
    attack or be attacked. The rule set's groups list the decisions a break may take."""
    state = load_state(db, campaign_id)
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    if not character.large_group:
        raise InvalidInput(f"{character.name} is not a group")
    if not character.critical_due:
        raise InvalidInput(f"{character.name} has no failed critical damage save to resolve")
    outcomes = (combat.groups or GroupRules()).break_outcomes
    if outcome not in outcomes:
        raise InvalidInput(f"outcome must be {' or '.join(repr(o) for o in outcomes)}")
    status = defeat_status(character, combat.defeat) if outcome == "defeat" else ""
    record(
        db,
        campaign_id,
        GroupBreakResolved(name=character.name, outcome=outcome, status=status),
    )
    db.commit()
    _end_if_one_side_left(db, campaign_id)
    return {"character": character.name, "outcome": outcome}


def player_save(
    db: Session,
    campaign_id: int,
    roll: int,
    combat: CombatRules,
    *,
    conditions: Mapping[str, ConditionRules] | None = None,
    rng: random.Random | None = None,
    plus: str = "sum",
    spec: CharacterSpec | None = None,
) -> dict[str, Any]:
    """The save a player character owes after harm overflowed, rolled by the player. Passing it
    ends any fatal condition the character has, such as Doomed, when the rule set defines it."""
    state = load_state(db, campaign_id)
    pc = state.pc()
    if pc is None or pc.save_due is None:
        raise InvalidInput("no player character owes a save")
    roll = whole_number(roll, "roll", 1, combat.check_sides)
    stat = pc.save_due
    result = resolve_save(combat, current(pc, stat), rng=rng, roll=roll, plus=plus)
    record(
        db,
        campaign_id,
        SaveRolled(
            name=pc.name,
            stat=stat,
            roll=result.roll,
            score=result.score,
            passed=result.passed,
            status=defeat_status(pc, combat.defeat),
        ),
    )
    db.commit()
    if not result.passed:
        death.on_critical_save_failed(db, campaign_id, combat, pc.name)
    elif conditions and rng is not None:
        for cid in list(pc.conditions):
            if cid in combat.fatal_conditions and cid in conditions:
                end_condition(
                    db,
                    campaign_id,
                    pc.name,
                    cid,
                    "gm",
                    combat,
                    conditions,
                    rng,
                    plus=plus,
                    spec=spec,
                )
    _end_if_one_side_left(db, campaign_id)
    return {
        "character": pc.name,
        "stat": stat,
        "roll": result.roll,
        "score": result.score,
        "passed": result.passed,
    }


def player_initiative(
    db: Session, campaign_id: int, roll: int, combat: CombatRules
) -> dict[str, Any]:
    """A player character's initiative save, on the rule set's initiative stat. Once every player
    character has rolled, the order of round one is set."""
    state = load_state(db, campaign_id)
    waiting = awaiting_initiative(state)
    if not waiting:
        raise InvalidInput("no one is rolling for initiative right now")
    character = state.characters[name_key(waiting[0])]
    roll = whole_number(roll, "roll", 1, combat.check_sides)
    result = resolve_save(combat, current(character, combat.initiative_stat), roll=roll)
    record(db, campaign_id, InitiativeRolled(name=character.name, passed=result.passed))
    db.commit()
    settle_initiative(db, campaign_id, combat.initiative_order)
    return {
        "character": character.name,
        "roll": result.roll,
        "score": result.score,
        "passed": result.passed,
        "still_waiting": awaiting_initiative(load_state(db, campaign_id)),
    }


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


def set_invincibility(db: Session, campaign_id: int, name: str, active: bool) -> dict[str, Any]:
    """Turn the debug invincibility buff on or off for a player character."""
    state = load_state(db, campaign_id)
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    if character.kind != PC_KIND:
        raise InvalidInput(f"{character.name} is not a player character")
    record(db, campaign_id, InvincibilitySet(name=character.name, active=active))
    db.commit()
    return {"character": character.name, "invincible": active}


def set_debug_stat(
    db: Session, campaign_id: int, name: str, stat: str, remaining: int
) -> dict[str, Any]:
    """Set a player character's current score for one stat, for testing damage rules."""
    state = load_state(db, campaign_id)
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    if character.kind != PC_KIND:
        raise InvalidInput(f"{character.name} is not a player character")
    if stat not in character.stats:
        raise InvalidInput(f"{character.name} has no stat {stat!r}")
    value = whole_number(remaining, "remaining", 0, character.stats[stat])
    record(db, campaign_id, DebugStatSet(name=character.name, stat=stat, remaining=value))
    db.commit()
    return {"character": character.name, "stat": stat, "remaining": value}


def add_debug_currency(
    db: Session, campaign_id: int, name: str, feature: str, amount: int
) -> dict[str, Any]:
    """Give a player character some of the rule set's currency, for testing purchases."""
    state = load_state(db, campaign_id)
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    if character.kind != PC_KIND:
        raise InvalidInput(f"{character.name} is not a player character")
    value = whole_number(amount, "amount", 1, 10_000)
    record(db, campaign_id, DebugCurrencyAdded(name=character.name, feature=feature, amount=value))
    db.commit()
    return {"character": character.name, "feature": feature, "gained": value}


def recover_character(
    db: Session,
    campaign_id: int,
    name: str,
    explanation: str,
    consequence: str,
    combat: CombatRules,
) -> dict[str, Any]:
    """Bring a player character back from critical damage. The GM says how they survived and what
    it cost them; the harm they took stays as it was."""
    state = load_state(db, campaign_id)
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    if character.kind != PC_KIND:
        raise InvalidInput(f"{character.name} is not a player character and cannot be recovered")
    if character.dead:
        raise InvalidInput(f"{character.name} is dead and cannot be recovered")
    if character.dying_hours is not None or character.dying_rounds is not None:
        raise InvalidInput(f"{character.name} is still dying; stabilize them first")
    if not character.defeated:
        raise InvalidInput(f"{character.name} is not {combat.defeat['player_character']}")
    record(
        db,
        campaign_id,
        CharacterRecovered(
            name=character.name,
            explanation=bounded_text(explanation, "explanation", MAX_RECOVERY_TEXT),
            consequence=bounded_text(consequence, "consequence", MAX_RECOVERY_TEXT),
        ),
    )
    db.commit()
    return {"character": character.name, "recovered": True}


def heal_character(
    db: Session,
    campaign_id: int,
    name: str,
    stat: str,
    amount: int,
    cause: str,
) -> dict[str, Any]:
    """Restore harm in one stat, never below zero. Healing is separate from recovering a
    mortally wounded character: it reduces harm, while `recover_character` clears the dying
    state."""
    state = load_state(db, campaign_id)
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    if stat not in character.stats:
        options = ", ".join(sorted(character.stats)) or "none"
        raise InvalidInput(f"{character.name} has no stat {stat!r}; the stats are: {options}")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < 1:
        raise InvalidInput("healing must restore a whole number of points, 1 or more")
    taken = character.harm.get(stat, 0)
    restored = min(amount, taken)
    record(
        db,
        campaign_id,
        CharacterHealed(
            name=character.name,
            stat=stat,
            amount=amount,
            restored=restored,
            cause=cause,
        ),
    )
    db.commit()
    return {
        "character": character.name,
        "stat": stat,
        "restored": restored,
        "harm": max(0, taken - restored),
    }


def harm_character(
    db: Session,
    campaign_id: int,
    name: str,
    stat: str,
    amount: int,
    cause: str,
    combat: CombatRules,
) -> dict[str, Any]:
    """Harm a player character outside combat, such as from a fall or poison. Only the rule set's
    outside-harm stats can take it. Harm that takes the overflow stat to 0 defeats the character."""
    state = load_state(db, campaign_id)
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    _living(character, combat)
    if stat not in combat.outside_harm:
        options = ", ".join(combat.outside_harm) or "none"
        raise InvalidInput(f"harm outside combat can only take {options}, not {stat!r}")
    value = whole_number(amount, "amount", 1, MAX_DAMAGE)
    before = current(character, stat)
    if character.invincible:
        return {
            "character": character.name,
            "stat": stat,
            "taken": 0,
            "current": before,
            "defeated": False,
        }
    if before == 0:
        raise InvalidInput(f"{character.name} has no {stat.upper()} left to lose")
    taken = min(value, before)
    overflow = [step.stat for step in combat.damage if step.kind == "overflow"]
    at_zero = stat in combat.defeat_at_zero and taken == before
    defeats = (stat in overflow and taken == before) or at_zero
    record(
        db,
        campaign_id,
        Harmed(
            name=character.name,
            stat=stat,
            taken=taken,
            cause=bounded_text(cause, "cause", MAX_CAUSE_TEXT),
            defeats=defeats,
            status=defeat_status(
                character, combat.defeat, killed=True if not at_zero else character.kind == NPC_KIND
            )
            if defeats
            else "",
        ),
    )
    if defeats and (character.kind == PC_KIND or character.important):
        if at_zero:
            death.begin_dying(
                db, campaign_id, combat, character.name, cause=f"outside combat, with {stat} at 0"
            )
        else:
            record(
                db,
                campaign_id,
                CharacterDied(name=character.name, cause=f"outside combat, with {stat} at 0"),
            )
    db.commit()
    return {
        "character": character.name,
        "stat": stat,
        "taken": taken,
        "current": before - taken,
        "defeated": defeats,
    }
