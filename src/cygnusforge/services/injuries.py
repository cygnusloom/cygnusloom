"""Injuries and the rule effects they carry. A rule set's effects are small ops, such as raising a
stat's maximum or adding a condition; this module runs them and records what they did. The ops and
their words live in the rule set's JSON, so nothing here names a particular game's stats."""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlmodel import Session

from cygnusforge.content.library import PICKED, CombatRules, InjuryEntry, InjuryTable
from cygnusforge.rules.dice import roll_total
from cygnusforge.services.death import begin_dying
from cygnusforge.services.events import (
    CharacterSaved,
    ConditionAdded,
    EffectNoted,
    InjuryGained,
    MaxChanged,
    current,
    load_state,
    name_key,
    record,
)


@dataclass
class EffectContext:
    """What effect ops need: dice, the rule set's combat rules for saves, and the stat the latest
    `pick_stat` chose, which `$picked` refers to."""

    rng: random.Random
    combat: CombatRules
    picked: str | None = None


def gain_injury(
    db: Session,
    campaign_id: int,
    name: str,
    table: InjuryTable,
    roll: int,
    cx: EffectContext,
) -> InjuryEntry:
    """Give a character the injury at `roll` on the table, then run the injury's effects."""
    entry = table.entries[roll - 1]
    record(
        db,
        campaign_id,
        InjuryGained(name=name, injury=entry.name, roll=entry.roll, text=entry.text),
    )
    db.commit()
    run_effects(db, campaign_id, name, entry.effects, cx, f"injury {entry.name}")
    return entry


def run_effects(
    db: Session,
    campaign_id: int,
    name: str,
    ops: Sequence[dict[str, Any]],
    cx: EffectContext,
    cause: str,
) -> None:
    """Run a list of effect ops in order, recording what each one does. The rule set's loader has
    already checked the ops, so this trusts their shape."""
    for op in ops:
        kind = op["op"]
        if kind == "note":
            record(db, campaign_id, EffectNoted(name=name, text=op["text"]))
        elif kind == "set_max":
            _set_max(db, campaign_id, name, op, cx, cause)
        elif kind == "condition":
            _add_condition(db, campaign_id, name, op["id"], cause, cx.combat)
        elif kind == "save":
            _save(db, campaign_id, name, op, cx, cause)
        elif kind == "pick_stat":
            _pick_stat(db, campaign_id, name, op, cx, cause)
        db.commit()


def _set_max(
    db: Session,
    campaign_id: int,
    name: str,
    op: dict[str, Any],
    cx: EffectContext,
    cause: str,
) -> None:
    stat = cx.picked if op["stat"] == PICKED else op["stat"]
    assert stat is not None
    roll = roll_total(op["dice"], cx.rng)
    character = load_state(db, campaign_id).characters[name_key(name)]
    before = character.stats.get(stat, 0)
    mode = op["mode"]
    if mode == "if_higher":
        after = max(before, roll)
    elif mode == "if_above_current":
        after = roll if roll > current(character, stat) else before
    elif mode == "add":
        after = before + roll
    else:
        after = roll
    reason = f"{cause}, rolled {roll}"
    if after == before:
        record(
            db,
            campaign_id,
            EffectNoted(
                name=name, text=f"{name}'s maximum {stat.upper()} stays {before} ({reason})."
            ),
        )
    else:
        record(
            db,
            campaign_id,
            MaxChanged(name=name, stat=stat, before=before, after=after, cause=reason),
        )


def _add_condition(
    db: Session, campaign_id: int, name: str, condition: str, cause: str, combat: CombatRules
) -> None:
    character = load_state(db, campaign_id).characters[name_key(name)]
    if condition in character.conditions:
        record(
            db,
            campaign_id,
            EffectNoted(name=name, text=f"already has the condition {condition}."),
        )
        return
    record(db, campaign_id, ConditionAdded(name=name, condition=condition, cause=cause))
    if condition in combat.dying_conditions:
        begin_dying(db, campaign_id, combat, name, cause="from a mortal wound")


def _save(
    db: Session,
    campaign_id: int,
    name: str,
    op: dict[str, Any],
    cx: EffectContext,
    cause: str,
) -> None:
    stat = op["stat"]
    character = load_state(db, campaign_id).characters[name_key(name)]
    roll = roll_total(cx.combat.check_die, cx.rng)
    score = current(character, stat)
    passed = cx.combat.module.save_succeeds(roll, score)
    record(
        db,
        campaign_id,
        CharacterSaved(
            name=character.name,
            stat=stat,
            roll=roll,
            score=score,
            passed=passed,
            cause=cause,
        ),
    )
    db.commit()
    run_effects(db, campaign_id, name, op.get("pass" if passed else "fail", []), cx, cause)


def _pick_stat(
    db: Session,
    campaign_id: int,
    name: str,
    op: dict[str, Any],
    cx: EffectContext,
    cause: str,
) -> None:
    """Roll the die and split its faces evenly across the options, in order."""
    options: list[str] = op["options"]
    sides = int(op["dice"][2:])
    roll = roll_total(op["dice"], cx.rng)
    stat = options[(roll - 1) * len(options) // sides]
    cx.picked = stat
    record(
        db,
        campaign_id,
        EffectNoted(name=name, text=f"{cause}: rolled {roll}, so the stat is {stat.upper()}."),
    )
