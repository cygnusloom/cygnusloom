"""Saves: what a save means and how a character rolls one."""

import random
from collections.abc import Mapping
from typing import Any

from sqlmodel import Session

from cygnusloom.content.character import CharacterSpec
from cygnusloom.content.library import (
    CombatRules,
    ConditionRules,
)
from cygnusloom.services import death, resolution
from cygnusloom.services.combat.damage import _end_if_one_side_left
from cygnusloom.services.common import InvalidInput, whole_number
from cygnusloom.services.conditions import end_condition
from cygnusloom.services.events import (
    PC_KIND,
    Character,
    CharacterSaved,
    SaveRolled,
    current,
    defeat_status,
    load_state,
    record,
)


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
    stat: str = "",
    cause: str = "",
) -> dict[str, Any]:
    """The save a player character owes after harm overflowed, rolled by the player. Passing it
    ends any fatal condition the character has, such as Doomed, when the rule set defines it.

    A `stat` names a hazard save instead, such as one a trap or a spell calls for. It is
    recorded as a CharacterSaved with its `cause`; a failure only records the result and never
    defeats the character."""
    state = load_state(db, campaign_id)
    pc = state.pc()
    if pc is None:
        raise InvalidInput("no player character owes a save; name the stat for a hazard save")
    if pc.save_due is None:
        if not stat:
            raise InvalidInput("no player character owes a save; name the stat for a hazard save")
        known = spec.stats if spec is not None else pc.stats
        if stat not in known:
            raise InvalidInput(f"there is no stat named {stat!r}")
        roll = whole_number(roll, "roll", 1, combat.check_sides)
        result = resolve_save(combat, current(pc, stat), rng=rng, roll=roll, plus=plus)
        record(
            db,
            campaign_id,
            CharacterSaved(
                name=pc.name,
                stat=stat,
                roll=result.roll,
                score=result.score,
                passed=result.passed,
                cause=cause,
            ),
        )
        db.commit()
        return {
            "character": pc.name,
            "stat": stat,
            "roll": result.roll,
            "score": result.score,
            "passed": result.passed,
        }
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
