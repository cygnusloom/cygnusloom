"""Makes a rule set's moves. A move rolls its dice, adds the stat's modifier and any situational
modifier, and runs the effects of the band the total reaches. Nothing here names a particular game:
the dice, stats, bands, and effects all come from the rule set's JSON."""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence

from sqlmodel import Session

from cygnusforge.content.character import CharacterSpec
from cygnusforge.content.library import (
    ALL_STATS,
    HIGHEST,
    CombatRules,
    ConditionRules,
    CounterRules,
    MoveBand,
    MoveRules,
)
from cygnusforge.rules.dice import roll_total
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.conditions import active_conditions
from cygnusforge.services.events import (
    Character,
    MoveMade,
    load_state,
    modifier_value,
    name_key,
    record,
)
from cygnusforge.services.injuries import EffectContext, run_effects


def band_for(move: MoveRules, total: int) -> MoveBand:
    """The first band the total reaches. The loader makes sure the last band takes what is left."""
    return next(band for band in move.bands if band.min is None or total >= band.min)


def condition_bonus(
    character: Character, conditions: Mapping[str, ConditionRules], stat: str
) -> int:
    """What the character's conditions add to a roll on `stat`: each held condition's amount for
    that stat, plus its amount for every stat."""
    return sum(
        rules.modifies.get(stat, 0) + rules.modifies.get(ALL_STATS, 0)
        for rules in active_conditions(character, conditions)
    )


def make_move(
    db: Session,
    campaign_id: int,
    name: str,
    move: MoveRules,
    rng: random.Random,
    *,
    plus: str,
    combat: CombatRules | None,
    conditions: Mapping[str, ConditionRules],
    counters: Mapping[str, CounterRules],
    modifier: int = 0,
    stat: str | None = None,
    cause: str = "",
    spec: CharacterSpec | None = None,
) -> dict[str, object]:
    """Roll `move` for the character, record the result, and run the band's effects. The effects
    carry their own events, so the outcome shows up in the campaign's history either way. A move
    with no stat of its own takes the `stat` the player names; a move with one ignores any other.

    A rule set with modifier bands and skills passes its `spec`: the stat's contribution is its
    modifier rather than its score, and a move with a `skill` adds that skill's level. Without a
    spec, the stat's score is added, as a rule set with no modifier bands expects."""
    state = load_state(db, campaign_id)
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    if move.stat == HIGHEST:
        if stat is not None:
            raise InvalidInput(
                f"{move.label} rolls the best of {list(move.stat_choice)}, not {stat!r}"
            )
    elif move.stat is not None and stat not in (None, move.stat):
        raise InvalidInput(f"{move.label} always rolls {move.stat}, not {stat!r}")
    if move.stat is None and move.stat_choice and stat not in move.stat_choice:
        raise InvalidInput(
            f"{move.label} rolls a stat the player chooses: pick one of {list(move.stat_choice)}"
        )
    if move.needs_all_conditions:
        missing = [
            rules.label
            for cid, rules in conditions.items()
            if cid not in character.conditions and rules.derived is None
        ]
        if missing:
            raise InvalidInput(
                f"{move.label} is only open once every condition is marked; "
                f"{character.name} still lacks {', '.join(missing)}"
            )
    rolled_stat = (
        best_stat(character, move.stat_choice) if move.stat == HIGHEST else move.stat or stat
    )
    if rolled_stat is not None and rolled_stat not in character.stats:
        raise InvalidInput(
            f"there is no stat named {rolled_stat!r}; choose one of {sorted(character.stats)}"
        )
    roll = roll_total(move.dice, rng, plus=plus)
    stat_bonus = 0
    if rolled_stat is not None:
        if spec is not None:
            stat_bonus = spec.modifier(character.stats, rolled_stat)
        else:
            stat_bonus = modifier_value(character, rolled_stat)
        stat_bonus += condition_bonus(character, conditions, rolled_stat)
    if spec is not None and move.skill:
        stat_bonus += max(0, spec.skill_level(character.skills, move.skill))
    bonus = stat_bonus + modifier
    total = roll + bonus
    band = band_for(move, total)
    record(
        db,
        campaign_id,
        MoveMade(
            name=character.name,
            move=move.label,
            roll=roll,
            bonus=bonus,
            total=total,
            band=band.label,
            text=band.text,
            cause=cause,
            stat=rolled_stat or "",
        ),
    )
    db.commit()
    run_effects(
        db,
        campaign_id,
        name,
        band.effects,
        EffectContext(rng=rng, combat=combat, counters=counters, conditions=conditions),
        cause or move.label,
        plus=plus,
    )
    result: dict[str, object] = {"move": move.label}
    if move.stat == HIGHEST:
        result["stat"] = rolled_stat
    result.update(roll=roll, bonus=bonus, total=total, band=band.label, text=band.text)
    return result


def best_stat(character: Character, choice: Sequence[str]) -> str:
    """The stat in `choice` with the highest modifier, the first one listed on a tie."""
    return max(choice, key=lambda stat: modifier_value(character, stat))
