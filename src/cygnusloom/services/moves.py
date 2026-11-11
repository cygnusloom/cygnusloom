"""Makes a rule set's moves. A move rolls its dice, adds the stat's modifier and any situational
modifier, and runs the effects of the band the total reaches. Nothing here names a particular game:
the dice, stats, bands, and effects all come from the rule set's JSON."""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence

from sqlmodel import Session

from cygnusloom.content.character import CharacterSpec, Feature
from cygnusloom.content.library import (
    ALL_STATS,
    HIGHEST,
    CombatRules,
    ConditionRules,
    CounterRules,
    MagicRules,
    MoveBand,
    MoveRules,
)
from cygnusloom.content.lookup import AmbiguousId, resolve_id
from cygnusloom.rules.dice import roll_total
from cygnusloom.services import reactions
from cygnusloom.services.common import InvalidInput, whole_number
from cygnusloom.services.conditions import active_conditions
from cygnusloom.services.events import (
    Character,
    MoveChoiceMade,
    MoveMade,
    SpellCast,
    decode,
    events_since,
    load_state,
    modifier_value,
    name_key,
    record,
)
from cygnusloom.services.injuries import EffectContext, run_effects


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


def canonical_stat(character: Character, spec: CharacterSpec | None, stat: str) -> str:
    """The stat id the character knows, accepting a display name or different capitalization."""
    if stat in character.stats:
        return stat
    table = spec.stats if spec is not None else character.stats
    names = (lambda rules: (rules.name,)) if spec is not None else None
    try:
        found = resolve_id(table, stat, kind="stat", names=names)
    except AmbiguousId as exc:
        raise InvalidInput(str(exc)) from exc
    except LookupError:
        found = ""
    if found not in character.stats:
        raise InvalidInput(
            f"there is no stat named {stat!r}; choose one of {sorted(character.stats)}"
        )
    return found


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
    features: Mapping[str, Feature] | None = None,
    roll: int | None = None,
    magic: MagicRules | None = None,
    spell: str = "",
) -> dict[str, object]:
    """Roll `move` for the character, record the result, and run the band's effects. The effects
    carry their own events, so the outcome shows up in the campaign's history either way. A move
    with no stat of its own takes the `stat` the player names; a move with one ignores any other.

    A rule set with modifier bands and skills passes its `spec`: the stat's contribution is its
    modifier rather than its score, and a move with a `skill` adds that skill's level. Without a
    spec, the stat's score is added, as a rule set with no modifier bands expects.

    `roll` is the dice total a player reported. When it is None the engine rolls the move's dice."""
    state = load_state(db, campaign_id)
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    if stat is not None and not stat.strip():
        stat = None
    if stat is not None and move.stat != HIGHEST:
        try:
            stat = canonical_stat(character, spec, stat)
        except InvalidInput:
            if move.stat is None and move.stat_choice:
                raise InvalidInput(
                    f"{move.label} rolls a stat the player chooses: "
                    f"pick one of {list(move.stat_choice)}"
                ) from None
            raise
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
        best_stat(character, move.stat_choice, spec) if move.stat == HIGHEST else move.stat or stat
    )
    if rolled_stat is not None:
        rolled_stat = canonical_stat(character, spec, rolled_stat)
    if roll is None:
        roll = roll_total(move.dice, rng, plus=plus)
    else:
        roll = whole_number(roll, "roll", 0, 1000)
    stat_bonus = 0
    if rolled_stat is not None:
        if spec is not None:
            stat_bonus = character.modifier(spec, rolled_stat)
        else:
            stat_bonus = modifier_value(character, rolled_stat)
        stat_bonus += condition_bonus(character, conditions, rolled_stat)
    if move.skill:
        if spec is None:
            raise InvalidInput(f"{move.label} needs the rule set's skill rules")
        skill_bonus = spec.skill_level(character.skills, move.skill)
        if move.skill_floor is not None:
            skill_bonus = max(move.skill_floor, skill_bonus)
        stat_bonus += skill_bonus
    bonus = stat_bonus + modifier
    total = roll + bonus
    band = band_for(move, total)
    triggered = None
    if spec is not None:
        triggered = reactions.match_move(
            character, spec, features, move, band, total, rolled_stat or ""
        )
    applied = False
    if triggered is not None:
        effect = triggered.reaction.effect
        if effect["op"] == "raise_band":
            target = next((b for b in move.bands if b.label == effect["to"]), None)
            if target is not None and move.bands.index(target) < move.bands.index(band):
                band = target
                applied = True
        elif effect["op"] == "add_total":
            total += int(effect["amount"])
            band = band_for(move, total)
            applied = True
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
    if triggered is not None and applied:
        reactions.use(db, campaign_id, character, triggered)
    else:
        triggered = None
    if (
        magic is not None
        and magic.model == "move"
        and magic.move is not None
        and move.id == magic.move.move
    ):
        record(
            db,
            campaign_id,
            SpellCast(
                name=character.name,
                spell=spell or move.label,
                source=move.label,
                model="move",
            ),
        )
    db.commit()
    run_effects(
        db,
        campaign_id,
        name,
        band.effects,
        EffectContext(rng=rng, combat=combat, counters=counters, conditions=conditions, spec=spec),
        cause or move.label,
        plus=plus,
    )
    result: dict[str, object] = {"move": move.label}
    if move.stat == HIGHEST:
        result["stat"] = rolled_stat
    result.update(roll=roll, bonus=bonus, total=total, band=band.label, text=band.text)
    if band.choices:
        # The player picks one before the reply goes on. Their effects run in choose_option, not
        # here, so the roll alone never spends the cost of the band.
        result["choices"] = [
            {"id": choice.id, "label": choice.label, "text": choice.text} for choice in band.choices
        ]
    if triggered is not None:
        result["reaction"] = triggered.text
    return result


def choose_option(
    db: Session,
    campaign_id: int,
    moves: Mapping[str, MoveRules],
    move: str,
    band: str,
    choice: str,
    rng: random.Random,
    *,
    plus: str,
    combat: CombatRules | None,
    conditions: Mapping[str, ConditionRules],
    counters: Mapping[str, CounterRules],
    spec: CharacterSpec | None = None,
) -> dict[str, object]:
    """Pick one of the choices a move band offered, and run its effects. `move` is the move's id
    or label, as `make_move` returns it. The choice must belong to a band the character's roll
    reached in the open turn, and a band takes only one choice: asking again returns the choice
    already made without recording or applying it twice."""
    rules = _find_move(moves, move)
    chosen_band = _find_band(rules, band)
    if chosen_band is None or not chosen_band.choices:
        raise InvalidInput(f"{rules.label} has no choices in the band {band!r}")
    option = next((item for item in chosen_band.choices if item.id == choice), None)
    if option is None:
        option = next(
            (
                item
                for item in chosen_band.choices
                if item.label.casefold() == choice.strip().casefold()
            ),
            None,
        )
    if option is None:
        offered = ", ".join(item.id for item in chosen_band.choices)
        raise InvalidInput(f"{rules.label} has no choice named {choice!r}; choose one of {offered}")
    state = load_state(db, campaign_id)
    turn = state.turn
    if turn is None:
        raise InvalidInput("there is no reply in progress to choose for")
    made: MoveMade | None = None
    picked: MoveChoiceMade | None = None
    for row in events_since(db, campaign_id, turn.opener):
        event = decode(row)
        if not isinstance(event, MoveChoiceMade | MoveMade):
            continue
        if name_key(event.move) != name_key(rules.label) or event.band != chosen_band.label:
            continue
        if isinstance(event, MoveChoiceMade):
            picked = event
        elif isinstance(event, MoveMade):
            made = event
    if picked is not None:
        # A resent answer must not record or apply the choice again.
        return _choice_result(rules, chosen_band, made, picked.choice, picked.label, picked.text)
    if made is None:
        raise InvalidInput(
            f"roll {rules.label} into the {chosen_band.label} band before choosing from it"
        )
    record(
        db,
        campaign_id,
        MoveChoiceMade(
            name=made.name,
            move=rules.label,
            band=chosen_band.label,
            choice=option.id,
            label=option.label,
            text=option.text,
        ),
    )
    db.commit()
    run_effects(
        db,
        campaign_id,
        made.name,
        option.effects,
        EffectContext(rng=rng, combat=combat, counters=counters, conditions=conditions, spec=spec),
        f"{rules.label}: {option.label}",
        plus=plus,
    )
    return _choice_result(rules, chosen_band, made, option.id, option.label, option.text)


def _find_move(moves: Mapping[str, MoveRules], move: str) -> MoveRules:
    """The move named by id, or else by label, as the roll result reports it."""
    found = moves.get(move)
    if found is not None:
        return found
    wanted = move.strip().casefold()
    found = next((item for item in moves.values() if item.label.casefold() == wanted), None)
    if found is None:
        raise InvalidInput(f"there is no move named {move!r}")
    return found


def _find_band(move: MoveRules, band: str) -> MoveBand | None:
    """The move's band with the given label, ignoring case."""
    wanted = band.strip().casefold()
    return next((item for item in move.bands if item.label.casefold() == wanted), None)


def _choice_result(
    move: MoveRules,
    band: MoveBand,
    made: MoveMade | None,
    choice: str,
    label: str,
    text: str,
) -> dict[str, object]:
    """What the model is told after the player picks a choice: the roll it belongs to and the
    pick, so it can narrate the cost."""
    result: dict[str, object] = {"move": move.label}
    if made is not None:
        result.update(
            roll=made.roll, bonus=made.bonus, total=made.total, band=band.label, text=made.text
        )
    result.update(choice=choice, choice_label=label, choice_text=text)
    return result


def best_stat(
    character: Character, choice: Sequence[str], spec: CharacterSpec | None = None
) -> str:
    """The stat in `choice` with the highest modifier, the first one listed on a tie. When the
    rule set has modifier bands, compare the same modifier the roll will add, not the raw score."""
    if spec is not None:
        return max(choice, key=lambda stat: character.modifier(spec, stat))
    return max(choice, key=lambda stat: modifier_value(character, stat))
