"""Attacks: the roll, the hit, the blast, and a party attacking together."""

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlmodel import Session

from cygnusloom.content.character import CharacterSpec, Feature
from cygnusloom.content.library import (
    ALL_STATS,
    CombatRules,
    ConditionRules,
    GroupRules,
)
from cygnusloom.rules.dice import roll_total
from cygnusloom.services import death
from cygnusloom.services.attacks import (
    AttackFlags,
    highest_index,
    modifier_dice,
    modifier_name,
    rolls_text,
)
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
)
from cygnusloom.services.combat.initiative import _no_initiative_pending
from cygnusloom.services.combat.save import _save
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.conditions import active_conditions
from cygnusloom.services.encounters import (
    _participant,
)
from cygnusloom.services.events import (
    NPC_KIND,
    PC_KIND,
    CampaignState,
    Character,
    ShieldBashed,
    Struck,
    current,
    load_state,
    name_key,
    out_of_fight,
    record,
    same_item_name,
)


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
            _record_shield_bash(db, campaign_id, attacker, weapon)
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
    _record_shield_bash(db, campaign_id, attacker, weapon)
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
        from cygnusloom.services.morale import check_morale

        checks = check_morale(db, campaign_id, combat, rng, target.name, plus=plus)
        if checks:
            result["morale"] = checks
    result["defeated"] = load_state(db, campaign_id).characters[name_key(target.name)].defeated
    _end_if_one_side_left(db, campaign_id)
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
    conditions: Mapping[str, ConditionRules] | None = None,
    spec: CharacterSpec | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """An NPC's attack. Under the auto hit mode it always hits, so the attack's damage is the roll.
    A modifier rolls its own dice instead of the attack's. An attack with Blast hits every target in
    its area, which is `targets` (it must include `target`), and each target rolls separately. An
    attacker may only strike on their own turn unless `force` overrides that; the turn order's
    Start and End buttons are what hand out turns."""
    state = load_state(db, campaign_id)
    _no_initiative_pending(state)
    source = _participant(state, attacker)
    _require_turn(state, source, force)
    if source.grappled_by and name_key(attack_name) not in combat.unarmed_names:
        raise InvalidInput(f"{source.name} is grappled and can only make unarmed attacks")
    if source.surprised_turns > 0:
        raise InvalidInput(f"{source.name} is surprised and cannot act this turn")
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
    flags = AttackFlags.of(source.attack_flags.get(name, ()))
    if flags.hits_all_targets:
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
        ignores_armor=flags.ignores_armor,
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
    ignores_armor = AttackFlags.of(source.attack_flags.get(name, ())).ignores_armor
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
                ignores_armor=ignores_armor,
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
    ignores_armor = AttackFlags.of(source.attack_flags.get(name, ())).ignores_armor
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
                ignores_armor=ignores_armor,
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
    force: bool = False,
) -> dict[str, Any]:
    """Several NPC attacks on one character, such as two NPCs of the foe side or one NPC's two
    weapons. Every attack rolls its own dice. Under the rule set's "highest" multi-attack the
    highest roll is the one hit and the rest are recorded; under "each" every attack hits in turn.
    Each attacker must be a living NPC on the foe side, every attack is checked before any die is
    rolled, and each attacker needs their own turn unless `force` overrides that."""
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
        _require_turn(state, source, force)
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
        planned.append(
            PlannedAttack(
                attacker=source.name,
                weapon=name,
                modifier=modifier_name(entry.get("modifier")),
                dice=dice or source.attacks[name],
                ignores_armor=flags.ignores_armor,
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
    range_band: str = "",
    dual_wield: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """A player character's hit. The engine rolls the damage unless the player's roll is passed as
    `damage_roll`. The hit mode decides whether it lands. A group takes only impaired attacks.
    When the player has already rolled the d20, pass it as `attack_roll`; the engine uses that
    natural roll. The character strikes on their own turn unless `force` overrides that.

    A rule set with modifier bands and skills passes its `spec` and the weapon's `feature`. The
    hit roll then adds the rule set's hit bonus stat, the weapon skill (or its unskilled penalty),
    and the weapon's best attribute modifier; the damage adds the rule set's damage bonus stat,
    the attribute modifier when the rule set uses attribute damage, and the unarmed skill for an
    unarmed strike; and shock comes from the weapon plus the attribute and damage bonuses."""
    state = load_state(db, campaign_id)
    _no_initiative_pending(state)
    pc = _player_character(state)
    _require_turn(state, pc, force)
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
    dice = _attack_dice(pc, name, modifier, combat)
    _require_impaired(struck, modifier_name(modifier), combat)
    raw = damage_roll if damage_roll is not None else roll_total(dice, rng, plus=plus)
    bonuses = _player_bonuses(combat, pc, name, spec, weapon_feature)
    range_penalty = 0
    if range_band:
        band = range_band.strip().casefold()
        if band not in combat.range_penalties:
            options = ", ".join(sorted(combat.range_penalties)) or "none"
            raise InvalidInput(f"there is no range band named {range_band!r}; choose {options}")
        range_penalty = combat.range_penalties[band]
    dual_penalty = 0
    if dual_wield:
        if not (combat.dual_wield_hit_penalty or combat.dual_wield_damage_bonus):
            raise InvalidInput("this rule set has no dual-wielding action")
        dual_penalty = combat.dual_wield_hit_penalty
        raw += combat.dual_wield_damage_bonus
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
            natural
            + bonuses.to_hit
            + hit_modifier
            + range_penalty
            + dual_penalty
            + _condition_attack_modifier(pc, conditions)
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


def _record_shield_bash(db: Session, campaign_id: int, attacker: str, weapon: str) -> None:
    """A shield bash suspends the attacker's shield AC until the attacker's next turn."""
    name = name_key(weapon)
    if name.startswith(("shield bash", "shield_bash")):
        record(db, campaign_id, ShieldBashed(name=attacker))


def _player_character(state: CampaignState) -> Character:
    pc = state.pc()
    if pc is None:
        raise InvalidInput("there is no player character in this campaign")
    return pc


def _require_turn(state: CampaignState, actor: Character, force: bool) -> None:
    """Refuse an attack the attacker has no turn for. An encounter runs on turns: a player
    character's turn starts automatically, an NPC's starts when the player presses Start, and a
    turn ends when the player ends it or, for an NPC, when the GM's reply finishes. That makes a
    turn the only time an actor may strike, so an NPC cannot cut in ahead of its place in the
    order. `force` lets the GM resolve an action the story needs outside the order, such as an
    ambush that takes the party unawares."""
    encounter = state.encounter
    if force or encounter is None:
        return
    if encounter.acting is None:
        # A player character whose turn is next may act before that turn is opened. Side
        # initiative settles the order with nobody acting, and the player should not have to
        # press Start for their own character.
        upcoming = encounter.order[encounter.up] if encounter.up < len(encounter.order) else ""
        if actor.kind == PC_KIND and upcoming and name_key(upcoming) == name_key(actor.name):
            return
        raise InvalidInput(
            f"no one is taking a turn, so {actor.name} cannot attack: begin the next turn first, "
            "or pass force true when the story needs an action outside the turn order"
        )
    if name_key(encounter.acting) != name_key(actor.name):
        raise InvalidInput(
            f"it is {encounter.acting}'s turn, not {actor.name}'s: press End your turn first, or "
            "pass force true when the story needs an action outside the turn order"
        )


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
