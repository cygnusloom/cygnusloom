"""Effect ops a rule set's JSON declares. An op is a small, named step such as raising a stat's
maximum or adding a condition; this module runs them in order and records what they did. The ops
and their words live in the rule set's JSON, so nothing here names a particular game's stats."""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session

from cygnusloom.content.character import CharacterSpec, ItemTemplate
from cygnusloom.content.library import (
    ALL_CONDITIONS,
    PICKED,
    CampaignClockDefinition,
    CombatRules,
    ConditionRules,
    CounterRules,
)
from cygnusloom.rules.dice import roll_total
from cygnusloom.services.death import begin_dying
from cygnusloom.services.events import (
    CharacterRetired,
    CharacterSaved,
    ConditionAdded,
    CounterChanged,
    EffectNoted,
    ItemAdded,
    ItemAmmoChanged,
    ItemConditionChanged,
    ItemRemoved,
    MaxChanged,
    SkillPracticed,
    VehicleUpdated,
    current,
    load_state,
    name_key,
    record,
)
from cygnusloom.services.state import alias_key


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
    # The target, weapon, and skill a move band named, resolved for `$target`, `$weapon`,
    # and `$skill`.
    target: str = ""
    weapon: str = ""
    skill: str = ""
    # A stable key for the current scene, so practice ticks land once per scene.
    scene: str = ""
    # Campaign clock definitions by id, for `noise` and `clock` effects.
    clock_definitions: Mapping[str, CampaignClockDefinition] = field(default_factory=dict)
    # Item size to inventory slots, for granting items from effects.
    item_sizes: Mapping[str, int] = field(default_factory=dict)


def _refresh_defense(db: Session, campaign_id: int, name: str, cx: EffectContext) -> None:
    if cx.spec is None or cx.combat is None or not cx.combat.defense_base:
        return
    character = load_state(db, campaign_id).characters[name_key(name)]
    if character.defense_tracked:
        # Combat imports this module, so this import must happen after the modules load.
        from cygnusloom.services.combat import recalculate_defense

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


def _find_item_template(cx: EffectContext, key: str) -> ItemTemplate | None:
    if cx.spec is None:
        return None
    template = cx.spec.items.get(key)
    if template is not None:
        return template
    wanted = name_key(key)
    return next(
        (item for item in cx.spec.items.values() if name_key(item.name) == wanted),
        None,
    )


def _carried_item(db: Session, campaign_id: int, name: str, key: str):
    character = load_state(db, campaign_id).characters[name_key(name)]
    wanted = name_key(key)
    return next((item for item in character.items if name_key(item.name) == wanted), None)


def _run_harm(
    db: Session,
    campaign_id: int,
    name: str,
    op: Mapping[str, Any],
    cx: EffectContext,
    cause: str,
    plus: str,
) -> None:
    target = op.get("target", "$target")
    if target == "$target":
        target = cx.target
    if target == "self":
        target = name
    if not target:
        raise ValueError("this harm effect needs a target")
    amount = int(op.get("amount", 0))
    piercing = bool(op.get("piercing", False))
    weapon = op.get("weapon", "")
    if weapon == "$weapon":
        weapon = cx.weapon
    if weapon:
        template = _find_item_template(cx, weapon)
        if template is not None:
            amount = template.harm or amount
            piercing = piercing or template.piercing
    from cygnusloom.services.harm import apply_harm

    apply_harm(
        db,
        campaign_id,
        target,
        amount,
        cause=cause,
        cx=cx,
        plus=plus,
        piercing=piercing,
    )


def _run_ammo(
    db: Session,
    campaign_id: int,
    name: str,
    op: Mapping[str, Any],
    cx: EffectContext,
    cause: str,
    plus: str,
) -> None:
    weapon = op.get("weapon", "$weapon")
    if weapon == "$weapon":
        weapon = cx.weapon
    if not weapon:
        raise ValueError("this ammo effect needs a weapon")
    held = _carried_item(db, campaign_id, name, weapon)
    if held is None:
        raise ValueError(f"{name} is not carrying {weapon!r}")
    rounds = int(op.get("rounds", 1))
    loaded = max(0, held.loaded - rounds)
    record(
        db,
        campaign_id,
        ItemAmmoChanged(
            name=name,
            item=held.name,
            loaded=loaded,
            capacity=held.capacity,
            cause=cause,
        ),
    )


def _run_noise(
    db: Session,
    campaign_id: int,
    name: str,
    op: Mapping[str, Any],
    cx: EffectContext,
    cause: str,
    plus: str,
) -> None:
    clock = str(op.get("clock", "noise"))
    amount = int(op.get("delta", 1))
    from cygnusloom.services.clocks import advance_clock

    advance_clock(
        db,
        campaign_id,
        clock,
        amount,
        cause=f"noise: {cause}",
        definitions=cx.clock_definitions,
        cx=cx,
        plus=plus,
    )


def _run_clock(
    db: Session,
    campaign_id: int,
    name: str,
    op: Mapping[str, Any],
    cx: EffectContext,
    cause: str,
    plus: str,
) -> None:
    from cygnusloom.services.clocks import advance_clock

    advance_clock(
        db,
        campaign_id,
        str(op["id"]),
        int(op.get("delta", 0)),
        cause=cause,
        definitions=cx.clock_definitions,
        cx=cx,
        plus=plus,
        set_to=op.get("set"),
    )


def _run_practice(
    db: Session,
    campaign_id: int,
    name: str,
    op: Mapping[str, Any],
    cx: EffectContext,
    cause: str,
    plus: str,
) -> None:
    if cx.spec is None or cx.spec.skill_progression is None:
        return
    skill = op.get("skill", "$skill")
    if skill == "$skill":
        skill = cx.skill
    if not skill:
        return
    progression = cx.spec.skill_progression
    if not progression.practice_thresholds:
        return
    character = load_state(db, campaign_id).characters[name_key(name)]
    before = character.practice.get(skill, 0)
    delta = int(op.get("delta", 1))
    top = progression.practice_thresholds[-1]
    after = min(top, before + delta)
    rank_before = character.skills.get(skill, progression.untrained)
    rank_after = rank_before
    for index, threshold in enumerate(progression.practice_thresholds):
        if after >= threshold:
            rank_after = max(rank_after, progression.first_level + index)
    rank_after = min(progression.maximum, rank_after)
    if rank_after != rank_before:
        after = 0
    record(
        db,
        campaign_id,
        SkillPracticed(
            name=character.name,
            skill=skill,
            skill_name=cx.spec.skills[skill].name,
            ticks=after,
            rank_before=rank_before,
            rank_after=rank_after,
            scene=cx.scene,
            cause=cause,
        ),
    )


def _run_item(
    db: Session,
    campaign_id: int,
    name: str,
    op: Mapping[str, Any],
    cx: EffectContext,
    cause: str,
    plus: str,
) -> None:
    if cx.spec is None:
        return
    template = cx.spec.items[op["item"]]
    mode = op.get("mode", "grant")
    if mode == "grant":
        record(
            db,
            campaign_id,
            ItemAdded(
                name=name,
                item=template.name,
                size=template.size,
                slots=cx.item_sizes.get(template.size, 1),
                kind_of_item=template.tag,
                cause=cause,
                text=template.description,
                uses=template.uses,
                max_uses=template.uses,
                damage=template.damage,
                rules=list(template.rules),
                flags=list(template.flags),
                loaded=template.capacity,
                capacity=template.capacity,
                caliber=template.caliber,
                condition=template.condition,
            ),
        )
    elif mode == "consume":
        record(db, campaign_id, ItemRemoved(name=name, item=template.name))
    else:
        record(
            db,
            campaign_id,
            ItemConditionChanged(
                name=name,
                item=template.name,
                condition=op.get("condition", "worn"),
                cause=cause,
            ),
        )


def _run_vehicle(
    db: Session,
    campaign_id: int,
    name: str,
    op: Mapping[str, Any],
    cx: EffectContext,
    cause: str,
    plus: str,
) -> None:
    state = load_state(db, campaign_id)
    key = alias_key(op["vehicle"])
    vehicle = state.vehicles.get(key)
    if vehicle is None:
        vehicle = next(
            (v for v in state.vehicles.values() if alias_key(v.name) == key), None
        )
    if vehicle is None:
        raise ValueError(f"there is no vehicle named {op['vehicle']!r}")
    record(
        db,
        campaign_id,
        VehicleUpdated(
            id=vehicle.id,
            name=vehicle.name,
            condition=op.get("condition"),
            fuel=op.get("fuel"),
            note=cause,
        ),
    )


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
    "harm": EffectExecutor(_run_harm),
    "ammo": EffectExecutor(_run_ammo),
    "noise": EffectExecutor(_run_noise),
    "clock": EffectExecutor(_run_clock),
    "practice": EffectExecutor(_run_practice),
    "item": EffectExecutor(_run_item),
    "vehicle": EffectExecutor(_run_vehicle),
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
    for threshold in sorted(rules.thresholds):
        if before < threshold <= after:
            run_effects(
                db,
                campaign_id,
                name,
                rules.thresholds[threshold],
                cx,
                f"{rules.label} reaches {threshold}",
                plus=plus,
            )
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
    from cygnusloom.services.conditions import end_condition as end_stored_condition

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
    cap = op["cap"]
    if isinstance(cap, Mapping):
        counter = str(cap["counter"])
        bands = cap["bands"]
        cap = int(bands.get(str(character.counters.get(counter, 0)), 0)) or None
    if cap is None:
        return
    after = min(int(cap), before + op["delta"])
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
    if combat is not None:
        hook = getattr(combat.module, "condition_gained", None) if combat.module else None
        if callable(hook):
            hook(db, campaign_id, name, condition, cause)
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
    from cygnusloom.services.combat import resolve_save

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
