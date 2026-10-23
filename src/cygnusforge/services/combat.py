"""Combat: hits, armor, the harm they cause, and the saves they force. The rule set's combat module
decides what a save means and how initiative is ordered. This service checks each step against the
encounter and records what happened.

Like the rest of the world service, each function loads the campaign's state, validates the
change, records its events, and commits."""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlmodel import Session

from cygnusforge.content.library import CombatRules, DamageStep
from cygnusforge.rules.dice import roll_total
from cygnusforge.services import death
from cygnusforge.services.attacks import (
    highest_index,
    modifier_dice,
    modifier_name,
    rolls_text,
)
from cygnusforge.services.common import InvalidInput, bounded_text, whole_number
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
    CharacterRecovered,
    CharacterSurrendered,
    DebugCurrencyAdded,
    DebugStatSet,
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
)
from cygnusforge.services.injuries import EffectContext, gain_injury

MAX_DAMAGE = 1000
# Sanity bound on armor when the rule set sets no cap (armor_max of 0).
UNCAPPED_ARMOR = 1000
MAX_RECOVERY_TEXT = 600
MAX_CAUSE_TEXT = 200


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
    if character.defeated:
        raise InvalidInput(f"{character.name} is {defeat_status(character, combat.defeat)}")
    if character.fled:
        raise InvalidInput(f"{character.name} has fled the fight")
    if character.surrendered:
        raise InvalidInput(f"{character.name} has surrendered")
    if character.critical_due:
        raise InvalidInput(
            f"{character.name} is broken and awaits the Warden's decision; call resolve_group_break"
        )
    return character


def _require_impaired(target: Character, modifier: str) -> None:
    """A group is hit only by impaired attacks."""
    if target.large_group and modifier != "impaired":
        raise InvalidInput(f"{target.name} is a group; attacks on it must be impaired")


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
) -> dict[str, Any]:
    """Apply a hit through the rule set's damage steps. A target that owes a save takes no more
    hits. An NPC saves at once; a player character saves when the player rolls. A hit on an NPC
    may also bring its side's morale check."""
    if target.save_due is not None:
        raise InvalidInput(f"{target.name} must make their {target.save_due} save first")
    damage = apply_damage(combat.damage, target.armor, lambda stat: current(target, stat), raw)
    if target.invincible:
        # The whole hit is absorbed, so the log reads as a blow turned aside rather than a miss.
        damage = Damage(absorbed=raw, harm={}, save=None)
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
        ),
    )
    db.commit()
    result: dict[str, Any] = {
        "target": target.name,
        "raw": raw,
        "armor": damage.absorbed,
        "harm": damage.harm,
        "save_due": damage.save,
    }
    injury = _injury_from_hit(db, campaign_id, combat, rng, target.name, damage)
    if injury is not None:
        result["injury"] = injury
    if damage.save is not None and target.kind == NPC_KIND:
        state = load_state(db, campaign_id)
        character = state.characters[name_key(target.name)]
        result["save"] = _save(db, campaign_id, combat, rng, character, damage.save)
    if target.kind == NPC_KIND and not target.large_group and combat.morale is not None:
        # morale imports this module for the shared helpers, so it is imported here to avoid a cycle.
        from cygnusforge.services.morale import check_morale

        checks = check_morale(db, campaign_id, combat, rng, target.name)
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
) -> str | None:
    """A player character hit to exactly 0 on the injury stat gains an injury. The injury is the table
    entry numbered by the harm this hit did, capped at the table's last entry. Damage that
    overflows onto another stat is separate: it owes a save, and the injury still applies."""
    table = combat.injury_table
    if table is None:
        return None
    lost = damage.harm.get(table.stat, 0)
    if lost == 0:
        return None
    character = load_state(db, campaign_id).characters[name_key(name)]
    if character.kind != PC_KIND or current(character, table.stat) != 0:
        return None
    roll = min(lost, len(table.entries))
    entry = gain_injury(db, campaign_id, name, table, roll, EffectContext(rng=rng, combat=combat))
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


def _save(
    db: Session,
    campaign_id: int,
    combat: CombatRules,
    rng: random.Random,
    character: Character,
    stat: str,
) -> dict[str, Any]:
    roll = roll_total(combat.check_die, rng)
    score = current(character, stat)
    passed = combat.module.save_succeeds(roll, score)
    critical = character.large_group
    record(
        db,
        campaign_id,
        SaveRolled(
            name=character.name,
            stat=stat,
            roll=roll,
            score=score,
            passed=passed,
            status="" if critical else defeat_status(character, combat.defeat),
            critical=critical,
        ),
    )
    db.commit()
    if not passed and not critical and character.kind == PC_KIND:
        death.on_critical_save_failed(db, campaign_id, combat, character.name)
        db.commit()
    return {"stat": stat, "roll": roll, "score": score, "passed": passed}


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
) -> dict[str, Any]:
    """An NPC's attack. Attacks always hit, so the attack's damage is the roll. A modifier rolls
    its own dice instead of the attack's. Who acts when is the turn order's job; this only checks
    the attacker has the attack and both are living."""
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
        return _blast(db, campaign_id, combat, rng, state, source, struck, name, modifier)
    _require_impaired(struck, modifier_name(modifier))
    dice = modifier_dice(combat.modifiers, modifier)
    raw = roll_total(dice or source.attacks[name], rng)
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
    )


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
) -> dict[str, Any]:
    """A group's attack, which has Blast: it rolls enhanced dice separately for each living
    individual on the other side. The named target must be one of them, but every one is hit."""
    encounter = state.encounter
    assert encounter is not None
    own_side = encounter.sides.get(name_key(source.name), "")
    if encounter.sides.get(name_key(struck.name), "") == own_side:
        raise InvalidInput(f"{source.name} is on {struck.name}'s side and cannot attack them")
    if struck.large_group:
        raise InvalidInput(
            f"{source.name} is a group and cannot attack {struck.name}, another group"
        )
    if modifier_name(modifier) not in ("", "enhanced"):
        raise InvalidInput(
            f"{source.name} is a group; its attacks are enhanced, so leave the modifier out"
        )
    targets = [
        state.characters[name_key(display)]
        for display in encounter.order
        if encounter.sides.get(name_key(display), "") != own_side
        and not state.characters[name_key(display)].large_group
        and not out_of_fight(state.characters[name_key(display)])
    ]
    if not targets:
        raise InvalidInput(
            f"there is no living individual on the other side for {source.name} to hit"
        )
    owed = [target.name for target in targets if target.save_due is not None]
    if owed:
        raise InvalidInput(f"{', '.join(owed)} must make their save first")
    dice = modifier_dice(combat.modifiers, "enhanced") or source.attacks[name]
    results = []
    for target in targets:
        # An earlier hit in the blast can break another target's side, so each is checked as it comes up.
        current_target = load_state(db, campaign_id).characters[name_key(target.name)]
        if out_of_fight(current_target):
            continue
        raw = roll_total(dice, rng)
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
                modifier="enhanced",
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
) -> dict[str, Any]:
    """Several NPC attacks on one character, such as two NPCs of the foe side or one NPC's two
    weapons. Every attack rolls its own dice, the highest roll is the one hit, and the rest are
    recorded. Each attacker must be a living NPC on the foe side, and every attack is checked
    before any die is rolled."""
    state = load_state(db, campaign_id)
    _no_initiative_pending(state)
    struck = _living(_participant(state, target), combat)
    if not attacks:
        raise InvalidInput("list at least one attack")
    encounter = state.encounter
    assert encounter is not None
    foe_side = encounter.sides.get(name_key(struck.name), "")
    planned: list[tuple[str, str, str, str]] = []
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
        _require_impaired(struck, modifier_name(entry.get("modifier")))
        name = _attack_named(source, attack_name)
        dice = modifier_dice(combat.modifiers, entry.get("modifier"))
        planned.append(
            (source.name, name, modifier_name(entry.get("modifier")), dice or source.attacks[name])
        )
    rolls = [roll_total(dice, rng) for *_, dice in planned]
    best = highest_index(rolls)
    attacker, weapon, modifier, _ = planned[best]
    rolled = ""
    if len(planned) > 1:
        rolled = rolls_text(
            [(who, what, mod, roll) for (who, what, mod, _), roll in zip(planned, rolls)]
        )
    return _strike(
        db,
        campaign_id,
        combat,
        rng,
        struck,
        attacker,
        weapon,
        rolls[best],
        modifier=modifier,
        rolled=rolled,
    )


def player_attack(
    db: Session,
    campaign_id: int,
    target: str,
    weapon: str,
    damage: int,
    combat: CombatRules,
    rng: random.Random,
    modifier: str = "",
) -> dict[str, Any]:
    """A player character's hit. The player rolls the damage, using the modifier's dice when there
    is one; the attack always hits. A group takes only impaired attacks."""
    state = load_state(db, campaign_id)
    _no_initiative_pending(state)
    pc = state.pc()
    if pc is None:
        raise InvalidInput("there is no player character in this campaign")
    _living(pc, combat)
    struck = _participant(state, target)
    _living(struck, combat)
    raw = whole_number(damage, "damage", 0, MAX_DAMAGE)
    name = weapon.strip()
    if not name:
        raise InvalidInput("weapon must name the attack")
    # The modifier's dice are rolled by the player, so this only checks the modifier is known.
    modifier_dice(combat.modifiers, modifier)
    _require_impaired(struck, modifier_name(modifier))
    return _strike(
        db, campaign_id, combat, rng, struck, pc.name, name, raw, modifier=modifier_name(modifier)
    )


def resolve_group_break(
    db: Session, campaign_id: int, name: str, outcome: str, combat: CombatRules
) -> dict[str, Any]:
    """The Warden's decision after a group's Critical Damage save failed: "defeat" breaks and
    defeats it, "flee" breaks it up so its members run from the fight. Until this is called, the
    group cannot attack or be attacked."""
    state = load_state(db, campaign_id)
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    if not character.large_group:
        raise InvalidInput(f"{character.name} is not a group")
    if not character.critical_due:
        raise InvalidInput(f"{character.name} has no failed critical damage save to resolve")
    if outcome not in ("defeat", "flee"):
        raise InvalidInput("outcome must be 'defeat' or 'flee'")
    status = defeat_status(character, combat.defeat) if outcome == "defeat" else ""
    record(
        db,
        campaign_id,
        GroupBreakResolved(name=character.name, outcome=outcome, status=status),
    )
    db.commit()
    _end_if_one_side_left(db, campaign_id)
    return {"character": character.name, "outcome": outcome}


def player_save(db: Session, campaign_id: int, roll: int, combat: CombatRules) -> dict[str, Any]:
    """The save a player character owes after harm overflowed, rolled by the player."""
    state = load_state(db, campaign_id)
    pc = state.pc()
    if pc is None or pc.save_due is None:
        raise InvalidInput("no player character owes a save")
    roll = whole_number(roll, "roll", 1, combat.check_sides)
    stat = pc.save_due
    score = current(pc, stat)
    passed = combat.module.save_succeeds(roll, score)
    record(
        db,
        campaign_id,
        SaveRolled(
            name=pc.name,
            stat=stat,
            roll=roll,
            score=score,
            passed=passed,
            status=defeat_status(pc, combat.defeat),
        ),
    )
    db.commit()
    if not passed:
        death.on_critical_save_failed(db, campaign_id, combat, pc.name)
    _end_if_one_side_left(db, campaign_id)
    return {"character": pc.name, "stat": stat, "roll": roll, "score": score, "passed": passed}


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
    score = current(character, combat.initiative_stat)
    passed = combat.module.save_succeeds(roll, score)
    record(db, campaign_id, InitiativeRolled(name=character.name, passed=passed))
    db.commit()
    settle_initiative(db, campaign_id, combat.module.initiative_order)
    return {
        "character": character.name,
        "roll": roll,
        "score": score,
        "passed": passed,
        "still_waiting": awaiting_initiative(load_state(db, campaign_id)),
    }


def set_armor(
    db: Session, campaign_id: int, name: str, armor: int, armor_max: int = 0
) -> dict[str, Any]:
    """Set what each hit against a character absorbs, up to the rule set's armor cap (0 means
    no cap)."""
    state = load_state(db, campaign_id)
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    value = whole_number(armor, "armor", 0, armor_max or UNCAPPED_ARMOR)
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
    if character.dying_hours is not None:
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
    defeats = stat in overflow and taken == before
    record(
        db,
        campaign_id,
        Harmed(
            name=character.name,
            stat=stat,
            taken=taken,
            cause=bounded_text(cause, "cause", MAX_CAUSE_TEXT),
            defeats=defeats,
            status=defeat_status(character, combat.defeat, killed=True) if defeats else "",
        ),
    )
    if defeats and character.kind == PC_KIND:
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
