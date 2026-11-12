"""Damage, dying, defeat, and recovery: the harm a hit leaves behind."""

import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlmodel import Session

from cygnusloom.content.character import CharacterSpec
from cygnusloom.content.library import (
    CombatRules,
    DamageStep,
)
from cygnusloom.services import death
from cygnusloom.services.combat.constants import (
    MAX_CAUSE_TEXT,
    MAX_DAMAGE,
    MAX_RECOVERY_TEXT,
)
from cygnusloom.services.common import InvalidInput, bounded_text, whole_number
from cygnusloom.services.events import (
    NPC_KIND,
    PC_KIND,
    Character,
    CharacterDied,
    CharacterRecovered,
    CharacterSurrendered,
    EncounterEnded,
    Harmed,
    Recovered,
    current,
    defeat_status,
    load_state,
    name_key,
    out_of_fight,
    record,
)
from cygnusloom.services.injuries import EffectContext, gain_injury
from cygnusloom.services.recovery import recover


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


def _harm_stat(combat: CombatRules) -> str | None:
    """The first stat a hit harms, which is the one shock and defeat are read from."""
    return combat.hit_pool


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
    restored = recover(character, stat, amount)
    record(
        db,
        campaign_id,
        Recovered(name=character.name, stat=stat, amount=restored, cause=cause),
    )
    db.commit()
    return {
        "character": character.name,
        "stat": stat,
        "restored": restored,
        "harm": character.harm.get(stat, 0),
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
