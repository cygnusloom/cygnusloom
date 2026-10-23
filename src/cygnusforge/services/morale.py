"""Morale and retreat. A side of NPCs makes a morale save at its first casualty and again at half
its number (when the rule set's morale block enables them); a lone NPC makes one when it reaches 0
on its lone stat. A failed save makes every living member of the side flee. Any character can try
to run, which takes a save on the retreat stat and a safe way out. Both checks come from the rule
set's morale block and never touch player characters' morale.

Like the rest of the world service, each function loads the campaign's state, validates the
change, records its events, and commits."""

from __future__ import annotations

import random
from typing import Any

from sqlmodel import Session

from cygnusforge.content.library import CombatRules, MoraleRules
from cygnusforge.rules.dice import roll_total
from cygnusforge.services.combat import (
    _end_if_one_side_left,
    _living,
    _no_initiative_pending,
    current,
)
from cygnusforge.services.common import InvalidInput, whole_number
from cygnusforge.services.encounters import _participant
from cygnusforge.services.events import (
    NPC_KIND,
    PC_KIND,
    CampaignState,
    Character,
    CharacterFled,
    MoraleChecked,
    RetreatRolled,
    load_state,
    name_key,
    out_of_fight,
    record,
)


def check_morale(
    db: Session,
    campaign_id: int,
    combat: CombatRules,
    rng: random.Random,
    target: str,
    *,
    plus: str,
) -> list[dict[str, Any]]:
    """Make whatever morale checks the target's side now owes after a hit on the target. Each tag
    is checked once per encounter, and each has its own save. Returns the checks made, in order."""
    morale = combat.morale
    if morale is None:
        return []
    checks: list[dict[str, Any]] = []
    while True:
        state = load_state(db, campaign_id)
        encounter = state.encounter
        if encounter is None:
            break
        character = state.characters.get(name_key(target))
        if character is None or character.kind != NPC_KIND or character.large_group:
            break
        side = encounter.sides.get(name_key(character.name), "")
        tag = _due_tag(state, side, morale)
        if tag is None:
            break
        group = _side_npcs(state, side)
        living = [member for member in group if not out_of_fight(member)]
        leader = living[0]
        roll = roll_total(combat.check_die, rng, plus=plus)
        score = current(leader, morale.save_stat)
        passed = combat.module.save_succeeds(roll, score)
        record(
            db,
            campaign_id,
            MoraleChecked(
                side=side,
                tag=tag,
                leader=leader.name,
                stat=morale.save_stat,
                roll=roll,
                score=score,
                passed=passed,
            ),
        )
        db.commit()
        if not passed:
            for member in living:
                record(db, campaign_id, CharacterFled(name=member.name, reason="morale"))
            db.commit()
        checks.append(
            {
                "side": side,
                "tag": tag,
                "leader": leader.name,
                "stat": morale.save_stat,
                "roll": roll,
                "score": score,
                "passed": passed,
                "fled": [member.name for member in living] if not passed else [],
            }
        )
    if checks:
        _end_if_one_side_left(db, campaign_id)
    return checks


def _due_tag(state: CampaignState, side: str, morale: MoraleRules) -> str | None:
    """The morale tag the side owes now, or None. "first" is checked before "half", and each gets
    its own save. A side with no living members owes nothing."""
    encounter = state.encounter
    assert encounter is not None
    group = _side_npcs(state, side)
    if not group or all(out_of_fight(member) for member in group):
        return None
    done = encounter.morale.get(side, [])
    if len(group) == 1:
        lone = group[0]
        if "first" not in done and not lone.defeated and current(lone, morale.lone_at) == 0:
            return "first"
        return None
    casualties = sum(1 for member in group if member.defeated)
    if casualties == 0:
        return None
    if morale.first_casualty and "first" not in done:
        return "first"
    if morale.half and "half" not in done and casualties * 2 >= len(group):
        return "half"
    return None


def _side_npcs(state: CampaignState, side: str) -> list[Character]:
    """The NPCs on a side, in turn order."""
    encounter = state.encounter
    assert encounter is not None
    members = []
    for display in encounter.order:
        character = state.characters[name_key(display)]
        if (
            character.kind == NPC_KIND
            and not character.large_group
            and encounter.sides.get(name_key(display), "") == side
        ):
            members.append(character)
    return members


def retreat(
    db: Session,
    campaign_id: int,
    name: str,
    safe: bool,
    combat: CombatRules,
    rng: random.Random,
    *,
    plus: str,
) -> dict[str, Any]:
    """An NPC's attempt to run from the fight: a save on the retreat stat, rolled by the engine."""
    state = load_state(db, campaign_id)
    character = _retreat_ready(state, name, safe, combat)
    if character.kind == PC_KIND:
        raise InvalidInput(f"{character.name} is a player character; use player_retreat instead")
    return _run_retreat(
        db, campaign_id, character, combat, roll_total(combat.check_die, rng, plus=plus)
    )


def player_retreat(
    db: Session,
    campaign_id: int,
    safe: bool,
    roll: int,
    combat: CombatRules,
) -> dict[str, Any]:
    """The player character's attempt to run from the fight. The player rolls the retreat save."""
    state = load_state(db, campaign_id)
    pc = state.pc()
    if pc is None:
        raise InvalidInput("there is no player character in this campaign")
    character = _retreat_ready(state, pc.name, safe, combat)
    roll = whole_number(roll, "roll", 1, combat.check_sides)
    return _run_retreat(db, campaign_id, character, combat, roll)


def _retreat_ready(state: CampaignState, name: str, safe: bool, combat: CombatRules) -> Character:
    """Check a character may try to run now: in the encounter, living, owing no save, with a safe
    way out. Only then is a roll made."""
    if combat.morale is None:
        raise InvalidInput("this rule set has no retreat")
    _no_initiative_pending(state)
    character = _participant(state, name)
    _living(character, combat)
    if character.save_due is not None:
        raise InvalidInput(f"{character.name} must make their {character.save_due} save first")
    if not safe:
        raise InvalidInput(f"{character.name} has no safe way out of the fight")
    return character


def _run_retreat(
    db: Session,
    campaign_id: int,
    character: Character,
    combat: CombatRules,
    roll: int,
) -> dict[str, Any]:
    morale = combat.morale
    assert morale is not None
    stat = morale.retreat_stat
    score = current(character, stat)
    passed = combat.module.save_succeeds(roll, score)
    record(
        db,
        campaign_id,
        RetreatRolled(name=character.name, stat=stat, roll=roll, score=score, passed=passed),
    )
    if passed:
        record(db, campaign_id, CharacterFled(name=character.name, reason="retreat"))
    db.commit()
    _end_if_one_side_left(db, campaign_id)
    return {
        "character": character.name,
        "stat": stat,
        "roll": roll,
        "score": score,
        "passed": passed,
    }
