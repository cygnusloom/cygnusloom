"""Effect ops a rule set's JSON declares. An op is a small, named step such as raising a stat's
maximum or adding a condition; this module runs them in order and records what they did. The ops
and their words live in the rule set's JSON, so nothing here names a particular game's stats."""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session

from cygnusforge.content.character import CharacterSpec
from cygnusforge.content.library import (
    ALL_CONDITIONS,
    PICKED,
    CombatRules,
    ConditionRules,
    CounterRules,
)
from cygnusforge.rules.dice import roll_total
from cygnusforge.services.death import begin_dying
from cygnusforge.services.events import (
    CharacterRetired,
    CharacterSaved,
    ConditionAdded,
    CounterChanged,
    EffectNoted,
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
    # The rule set's combat, or None when it has none. Only saves and dying conditions need it.
    combat: CombatRules | None
    picked: str | None = None
    # The rule set's counters and stored conditions by id. Only moves, and the effects of counters
    # and conditions they reach, can use them.
    counters: Mapping[str, CounterRules] = field(default_factory=dict)
    conditions: Mapping[str, ConditionRules] = field(default_factory=dict)
    spec: CharacterSpec | None = None


def _refresh_defense(db: Session, campaign_id: int, name: str, cx: EffectContext) -> None:
    if cx.spec is None or cx.combat is None or not cx.combat.defense_base:
        return
    character = load_state(db, campaign_id).characters[name_key(name)]
    if character.defense_tracked:
        # Combat imports this module, so this import must happen after the modules load.
        from cygnusforge.services.combat import recalculate_defense

        recalculate_defense(db, campaign_id, character.name, cx.spec, cx.combat)


def _run_note(
    db: Session,
    campaign_id: int,
    name: str,
    op: Mapping[str, Any],
    cx: EffectContext,
    cause: str,
    plus: str,
) -> None:
    record(db, campaign_id, EffectNoted(name=name, text=op["text"]))


def _run_set_max(
    db: Session,
    campaign_id: int,
    name: str,
    op: Mapping[str, Any],
    cx: EffectContext,
    cause: str,
    plus: str,
) -> None:
    _set_max(db, campaign_id, name, op, cx, cause, plus=plus)


def _run_condition(
    db: Session,
    campaign_id: int,
    name: str,
    op: Mapping[str, Any],
    cx: EffectContext,
    cause: str,
    plus: str,
) -> None:
    gain_condition(db, campaign_id, name, op["id"], cause, cx.combat)


def _run_save(
    db: Session,
    campaign_id: int,
    name: str,
    op: Mapping[str, Any],
    cx: EffectContext,
    cause: str,
    plus: str,
) -> None:
    _save(db, campaign_id, name, op, cx, cause, plus=plus)


def _run_pick_stat(
    db: Session,
    campaign_id: int,
    name: str,
    op: Mapping[str, Any],
    cx: EffectContext,
    cause: str,
    plus: str,
) -> None:
    _pick_stat(db, campaign_id, name, op, cx, cause, plus=plus)


def _run_raise_stat(
    db: Session,
    campaign_id: int,
    name: str,
    op: Mapping[str, Any],
    cx: EffectContext,
    cause: str,
    plus: str,
) -> None:
    _raise_stat(db, campaign_id, name, op, cx, cause)


def _run_counter(
    db: Session,
    campaign_id: int,
    name: str,
    op: Mapping[str, Any],
    cx: EffectContext,
    cause: str,
    plus: str,
) -> None:
    rules = cx.counters[op["id"]]
    if "reset" in op:
        amount, absolute = rules.initial, True
    else:
        amount, absolute = op.get("delta", op.get("set")), "set" in op
    change_counter(
        db,
        campaign_id,
        name,
        rules,
        amount,
        absolute=absolute,
        cause=cause,
        cx=cx,
        plus=plus,
    )


def _run_end_condition(
    db: Session,
    campaign_id: int,
    name: str,
    op: Mapping[str, Any],
    cx: EffectContext,
    cause: str,
    plus: str,
) -> None:
    if op["id"] == ALL_CONDITIONS:
        _end_all_conditions(db, campaign_id, name, cx, cause, plus=plus)
    else:
        _end_condition(db, campaign_id, name, op["id"], cx, cause, plus=plus)


def _run_retire(
    db: Session,
    campaign_id: int,
    name: str,
    op: Mapping[str, Any],
    cx: EffectContext,
    cause: str,
    plus: str,
) -> None:
    record(db, campaign_id, CharacterRetired(name=name, cause=cause))


@dataclass(frozen=True)
class EffectExecutor:
    """One effect op's runtime: the function that runs it, and whether the character's defense
    must be recalculated after it."""

    run: Callable[[Session, int, str, Mapping[str, Any], EffectContext, str, str], None]
    refresh_defense: bool = False


# The runtime for every effect op the loader accepts. Adding an op means registering its check in
# content.library's EFFECT_RULES and its executor here.
EFFECT_EXECUTORS: Mapping[str, EffectExecutor] = {
    "note": EffectExecutor(_run_note),
    "set_max": EffectExecutor(_run_set_max, refresh_defense=True),
    "condition": EffectExecutor(_run_condition),
    "save": EffectExecutor(_run_save),
    "pick_stat": EffectExecutor(_run_pick_stat),
    "counter": EffectExecutor(_run_counter),
    "end_condition": EffectExecutor(_run_end_condition),
    "retire": EffectExecutor(_run_retire),
    "raise_stat": EffectExecutor(_run_raise_stat, refresh_defense=True),
}


def run_effects(
    db: Session,
    campaign_id: int,
    name: str,
    ops: Sequence[Mapping[str, Any]],
    cx: EffectContext,
    cause: str,
    *,
    plus: str,
) -> None:
    """Run a list of effect ops in order, recording what each one does. The rule set's loader has
    already checked the ops, so this trusts their shape."""
    for op in ops:
        executor = EFFECT_EXECUTORS[op["op"]]
        executor.run(db, campaign_id, name, op, cx, cause, plus)
        if executor.refresh_defense:
            _refresh_defense(db, campaign_id, name, cx)
        db.commit()


def change_counter(
    db: Session,
    campaign_id: int,
    name: str,
    rules: CounterRules,
    amount: int,
    *,
    absolute: bool,
    cause: str,
    cx: EffectContext,
    plus: str,
) -> None:
    """Move a character's counter by `amount`, or set it to `amount` when `absolute`, keeping it
    within the counter's bounds. Reaching the maximum runs the counter's `full` effects."""
    character = load_state(db, campaign_id).characters[name_key(name)]
    before = character.counters.get(rules.id, rules.initial)
    after = max(rules.minimum, amount if absolute else before + amount)
    if rules.maximum is not None:
        after = min(rules.maximum, after)
    if after == before:
        record(
            db,
            campaign_id,
            EffectNoted(
                name=character.name,
                text=f"{character.name}'s {rules.label} stays {before} ({cause}).",
            ),
        )
    else:
        record(
            db,
            campaign_id,
            CounterChanged(
                name=character.name,
                counter=rules.id,
                label=rules.label,
                before=before,
                after=after,
                cause=cause,
            ),
        )
        _refresh_defense(db, campaign_id, name, cx)
    db.commit()
    if rules.full and rules.maximum is not None and before < rules.maximum <= after:
        run_effects(db, campaign_id, name, rules.full, cx, f"{rules.label} filled", plus=plus)


def _end_all_conditions(
    db: Session,
    campaign_id: int,
    name: str,
    cx: EffectContext,
    cause: str,
    *,
    plus: str,
) -> None:
    """End every condition the character holds, each with its own on-end effects. A character with
    none is noted, not an error."""
    character = load_state(db, campaign_id).characters[name_key(name)]
    if not character.conditions:
        record(
            db,
            campaign_id,
            EffectNoted(
                name=character.name, text=f"{character.name} has no conditions to end ({cause})."
            ),
        )
        return
    for condition in list(character.conditions):
        _end_condition(db, campaign_id, name, condition, cx, cause, plus=plus)


def _end_condition(
    db: Session,
    campaign_id: int,
    name: str,
    condition: str,
    cx: EffectContext,
    cause: str,
    *,
    plus: str,
) -> None:
    """End a stored condition the character has, then run its on-end effects. A character who does
    not have it is noted, not an error, since an effect can name a condition that has already ended."""
    character = load_state(db, campaign_id).characters[name_key(name)]
    rules = cx.conditions[condition]
    if condition not in character.conditions:
        record(
            db,
            campaign_id,
            EffectNoted(
                name=character.name,
                text=f"{character.name} does not have {rules.label}, so it does not end ({cause}).",
            ),
        )
        return
    # Conditions imports this module, so the import belongs here rather than at module load.
    from cygnusforge.services.conditions import end_condition as end_stored_condition

    end_stored_condition(
        db,
        campaign_id,
        character.name,
        condition,
        cause,
        cx.combat,
        cx.conditions,
        cx.rng,
        plus=plus,
        spec=cx.spec,
        force=True,
    )


def _raise_stat(
    db: Session,
    campaign_id: int,
    name: str,
    op: Mapping[str, Any],
    cx: EffectContext,
    cause: str,
) -> None:
    """Raise a stat's maximum by `delta`, but no higher than `cap`."""
    stat = cx.picked if op["stat"] == PICKED else op["stat"]
    assert stat is not None
    character = load_state(db, campaign_id).characters[name_key(name)]
    before = character.stats.get(stat, 0)
    after = min(op["cap"], before + op["delta"])
    if after <= before:
        record(
            db,
            campaign_id,
            EffectNoted(name=name, text=f"{name}'s {stat.upper()} stays {before} ({cause})."),
        )
    else:
        record(
            db,
            campaign_id,
            MaxChanged(name=name, stat=stat, before=before, after=after, cause=cause, label=""),
        )


def _set_max(
    db: Session,
    campaign_id: int,
    name: str,
    op: Mapping[str, Any],
    cx: EffectContext,
    cause: str,
    *,
    plus: str,
) -> None:
    stat = cx.picked if op["stat"] == PICKED else op["stat"]
    assert stat is not None
    roll = roll_total(op["dice"], cx.rng, plus=plus)
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


def gain_condition(
    db: Session,
    campaign_id: int,
    name: str,
    condition: str,
    cause: str,
    combat: CombatRules | None,
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
    if combat is not None and condition in combat.dying_conditions:
        begin_dying(db, campaign_id, combat, name, cause=f"from {cause}")


def _save(
    db: Session,
    campaign_id: int,
    name: str,
    op: Mapping[str, Any],
    cx: EffectContext,
    cause: str,
    *,
    plus: str,
) -> None:
    stat = op["stat"]
    combat = cx.combat
    assert combat is not None, "the loader only allows a save in a rule set with combat"
    character = load_state(db, campaign_id).characters[name_key(name)]
    # Combat imports this module, so this import must happen after the modules load.
    from cygnusforge.services.combat import resolve_save

    result = resolve_save(combat, current(character, stat), rng=cx.rng, plus=plus)
    record(
        db,
        campaign_id,
        CharacterSaved(
            name=character.name,
            stat=stat,
            roll=result.roll,
            score=result.score,
            passed=result.passed,
            cause=cause,
        ),
    )
    db.commit()
    run_effects(
        db,
        campaign_id,
        name,
        op.get("pass" if result.passed else "fail", []),
        cx,
        cause,
        plus=plus,
    )


def _pick_stat(
    db: Session,
    campaign_id: int,
    name: str,
    op: Mapping[str, Any],
    cx: EffectContext,
    cause: str,
    *,
    plus: str,
) -> None:
    """Roll the die and split its faces evenly across the options, in order."""
    options: list[str] = op["options"]
    sides = int(op["dice"][2:])
    roll = roll_total(op["dice"], cx.rng, plus=plus)
    stat = options[(roll - 1) * len(options) // sides]
    cx.picked = stat
    record(
        db,
        campaign_id,
        EffectNoted(name=name, text=f"{cause}: rolled {roll}, so the stat is {stat.upper()}."),
    )
