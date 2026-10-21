"""Combat: hits, armor, the harm they cause, and the saves they force. The rule set's combat module
decides what a save means and how initiative is ordered. This service checks each step against the
encounter and records what happened.

Like the rest of the world service, each function loads the campaign's state, validates the
change, records its events, and commits."""

from __future__ import annotations

import random
from typing import Any

from sqlmodel import Session

from cygnusforge.content.library import CombatRules
from cygnusforge.rules.dice import roll_total
from cygnusforge.services.common import InvalidInput, whole_number
from cygnusforge.services.encounters import (
    _participant,
    awaiting_initiative,
    initiative_open,
    settle_initiative,
)
from cygnusforge.services.events import (
    NPC_KIND,
    ArmorSet,
    CampaignState,
    Character,
    InitiativeRolled,
    SaveRolled,
    Struck,
    load_state,
    name_key,
    record,
)

D20 = "1d20"
MAX_DAMAGE = 1000


def current(character: Character, stat: str) -> int:
    """A stat's score less the harm it has taken, never below 0."""
    return max(0, character.stats.get(stat, 0) - character.harm.get(stat, 0))


def _living(character: Character) -> Character:
    if character.defeated:
        raise InvalidInput(f"{character.name} is defeated")
    return character


def _strike(
    db: Session,
    campaign_id: int,
    combat: CombatRules,
    rng: random.Random,
    target: Character,
    source: str,
    raw: int,
) -> dict[str, Any]:
    """Apply a hit. Armor absorbs first, then hit points take harm, then the overflow comes off
    the overflow stat and the target must save against it. An NPC saves at once; a player
    character saves when the player rolls, so a character who still owes a save takes no more hits."""
    if target.save_due is not None:
        raise InvalidInput(f"{target.name} must make their {target.save_due} save first")
    absorbed = min(target.armor, raw)
    dealt = raw - absorbed
    on_hit_points = min(dealt, current(target, combat.hit_points))
    overflow = dealt - on_hit_points
    harm: dict[str, int] = {}
    if on_hit_points:
        harm[combat.hit_points] = on_hit_points
    if overflow:
        harm[combat.overflow] = overflow
    save = combat.overflow if overflow else None
    record(
        db,
        campaign_id,
        Struck(name=target.name, source=source, raw=raw, armor=absorbed, harm=harm, save=save),
    )
    db.commit()
    result: dict[str, Any] = {
        "target": target.name,
        "raw": raw,
        "armor": absorbed,
        "harm": harm,
        "save_due": save,
    }
    if save is not None and target.kind == NPC_KIND:
        state = load_state(db, campaign_id)
        character = state.characters[name_key(target.name)]
        result["save"] = _save(db, campaign_id, combat, rng, character, save)
    result["defeated"] = load_state(db, campaign_id).characters[name_key(target.name)].defeated
    return result


def _save(
    db: Session,
    campaign_id: int,
    combat: CombatRules,
    rng: random.Random,
    character: Character,
    stat: str,
) -> dict[str, Any]:
    roll = roll_total(D20, rng)
    score = current(character, stat)
    passed = combat.module.save_succeeds(roll, score)
    record(
        db,
        campaign_id,
        SaveRolled(name=character.name, stat=stat, roll=roll, score=score, passed=passed),
    )
    db.commit()
    return {"stat": stat, "roll": roll, "score": score, "passed": passed}


def _no_initiative_pending(state: CampaignState) -> None:
    if initiative_open(state):
        waiting = ", ".join(awaiting_initiative(state))
        raise InvalidInput(f"initiative is not set yet; {waiting} must roll for it first")


def attack(
    db: Session,
    campaign_id: int,
    attacker: str,
    attack_name: str,
    target: str,
    combat: CombatRules,
    rng: random.Random,
) -> dict[str, Any]:
    """An NPC's attack. Attacks always hit, so the attack's dice are the damage. Who acts when is
    the turn order's job; this only checks the attacker has the attack and both are living."""
    state = load_state(db, campaign_id)
    _no_initiative_pending(state)
    source = _participant(state, attacker)
    if source.kind != NPC_KIND:
        raise InvalidInput(f"{source.name} is a player character; use player_attack instead")
    _living(source)
    struck = _participant(state, target)
    _living(struck)
    wanted = attack_name.strip().casefold()
    matches = [name for name in source.attacks if name.casefold() == wanted]
    if not matches:
        options = ", ".join(source.attacks) or "none"
        raise InvalidInput(f"{source.name} has no attack named {attack_name!r}; attacks: {options}")
    name = matches[0]
    raw = roll_total(source.attacks[name], rng)
    return _strike(db, campaign_id, combat, rng, struck, f"{source.name}'s {name}", raw)


def player_attack(
    db: Session,
    campaign_id: int,
    target: str,
    weapon: str,
    damage: int,
    combat: CombatRules,
    rng: random.Random,
) -> dict[str, Any]:
    """A player character's hit. The player rolls the damage; the attack always hits."""
    state = load_state(db, campaign_id)
    _no_initiative_pending(state)
    pc = state.pc()
    if pc is None:
        raise InvalidInput("there is no player character in this campaign")
    _living(pc)
    struck = _participant(state, target)
    _living(struck)
    raw = whole_number(damage, "damage", 0, MAX_DAMAGE)
    name = weapon.strip()
    if not name:
        raise InvalidInput("weapon must name the attack")
    return _strike(db, campaign_id, combat, rng, struck, f"{pc.name}'s {name}", raw)


def player_save(db: Session, campaign_id: int, roll: int, combat: CombatRules) -> dict[str, Any]:
    """The save a player character owes after harm overflowed, rolled by the player."""
    state = load_state(db, campaign_id)
    pc = state.pc()
    if pc is None or pc.save_due is None:
        raise InvalidInput("no player character owes a save")
    roll = whole_number(roll, "roll", 1, 20)
    stat = pc.save_due
    score = current(pc, stat)
    passed = combat.module.save_succeeds(roll, score)
    record(
        db,
        campaign_id,
        SaveRolled(name=pc.name, stat=stat, roll=roll, score=score, passed=passed),
    )
    db.commit()
    return {"character": pc.name, "stat": stat, "roll": roll, "score": score, "passed": passed}


def player_initiative(
    db: Session, campaign_id: int, roll: int, combat: CombatRules
) -> dict[str, Any]:
    """A player character's DEX save for initiative. Once every player character has rolled, the
    order of round one is set."""
    state = load_state(db, campaign_id)
    waiting = awaiting_initiative(state)
    if not waiting:
        raise InvalidInput("no one is rolling for initiative right now")
    character = state.characters[name_key(waiting[0])]
    roll = whole_number(roll, "roll", 1, 20)
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


def set_armor(db: Session, campaign_id: int, name: str, armor: int) -> dict[str, Any]:
    """Set what each hit against a character absorbs."""
    state = load_state(db, campaign_id)
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    value = whole_number(armor, "armor", 0, MAX_DAMAGE)
    record(db, campaign_id, ArmorSet(name=character.name, armor=value))
    db.commit()
    return {"character": character.name, "armor": value}
