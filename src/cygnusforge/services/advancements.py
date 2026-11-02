"""Spending an Advancement. The rule set's JSON says what each option costs and does, so this module
only checks the request, spends the point, and runs the option's effects."""

from __future__ import annotations

import random
from collections.abc import Mapping

from sqlmodel import Session

from cygnusforge.content.character import (
    CharacterError,
    CharacterSpec,
    Feature,
    check_ordered_feature_rank,
)
from cygnusforge.content.library import (
    AdvancementRules,
    CombatRules,
    ConditionRules,
    CounterRules,
    LearnRules,
)
from cygnusforge.services.common import InvalidInput
from cygnusforge.services.events import (
    AdvancementTaken,
    Character,
    FeatureLearned,
    load_state,
    name_key,
    record,
)
from cygnusforge.services.injuries import EffectContext, change_counter, run_effects


def take_advancement(
    db: Session,
    campaign_id: int,
    name: str,
    option_id: str,
    *,
    advancements: AdvancementRules | None,
    counters: Mapping[str, CounterRules],
    conditions: Mapping[str, ConditionRules],
    combat: CombatRules | None,
    rng: random.Random,
    plus: str,
    stat: str | None = None,
    feature: str | None = None,
    features: Mapping[str, Feature] | None = None,
    spec: CharacterSpec | None = None,
) -> dict[str, object]:
    """Spend one point of the Advancement counter on `option_id`, and run the option's effects. `stat`
    names the stat an option that picks one raises; other options take none. `feature` names the
    feature an option that teaches one gives the character."""
    if advancements is None:
        raise InvalidInput("this rule set has no Advancements")
    character = load_state(db, campaign_id).characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    option = advancements.options.get(option_id)
    if option is None:
        raise InvalidInput(
            f"there is no Advancement {option_id!r}; choose one of {sorted(advancements.options)}"
        )
    if option.limit is not None and character.advancements_taken.get(option_id, 0) >= option.limit:
        raise InvalidInput(f"{character.name} has already taken {option.label} as often as allowed")
    if option.picks and stat not in character.stats:
        raise InvalidInput(f"{option.label} needs a stat; choose one of {sorted(character.stats)}")
    if not option.picks and stat is not None:
        raise InvalidInput(f"{option.label} does not take a stat")
    counter = counters[advancements.counter]
    points = character.counters.get(advancements.counter, counter.initial)
    if points < 1:
        raise InvalidInput(f"{character.name} has no {advancements.label} to spend")
    learned = _learnable(option.label, option.learn, character, feature, features or {})
    if learned is not None and learned.defense_floor is not None:
        if combat is None or not combat.defense_base or spec is None:
            raise InvalidInput("a defense feature requires equipped defense rules")
        if not character.defense_tracked:
            raise InvalidInput(f"{character.name}'s equipped defense has not been recorded")
    if learned is not None and spec is not None:
        held = {**character.features, learned.id: character.features.get(learned.id, 0) + 1}
        try:
            check_ordered_feature_rank(spec, learned, held)
        except CharacterError as exc:
            raise InvalidInput(str(exc)) from exc

    record(
        db,
        campaign_id,
        AdvancementTaken(
            name=character.name, option=option_id, label=option.label, stat=stat or ""
        ),
    )
    if learned is not None:
        record(
            db,
            campaign_id,
            FeatureLearned(
                name=character.name,
                feature=learned.id,
                feature_name=learned.name,
                cause=f"{character.name} takes {option.label}",
                repeat=learned.repeatable,
            ),
        )
    cx = EffectContext(
        rng=rng, combat=combat, picked=stat, counters=counters, conditions=conditions, spec=spec
    )
    change_counter(
        db,
        campaign_id,
        character.name,
        counter,
        -1,
        absolute=False,
        cause=f"{character.name} takes {option.label}",
        cx=cx,
        plus=plus,
    )
    run_effects(
        db,
        campaign_id,
        character.name,
        option.effects,
        cx,
        cause=f"{character.name} takes {option.label}",
        plus=plus,
    )
    value = (
        load_state(db, campaign_id)
        .characters[name_key(name)]
        .counters.get(advancements.counter, counter.initial)
    )
    db.commit()
    return {
        "character": character.name,
        "advancement": option.label,
        "stat": stat,
        "feature": learned.name if learned is not None else None,
        "remaining": value,
    }


def _learnable(
    label: str,
    learn: LearnRules | None,
    character: Character,
    feature: str | None,
    features: Mapping[str, Feature],
) -> Feature | None:
    """The feature an option teaches, checked against the character: it must be of the option's
    type and carry the tag of the character's field; nonrepeatable features must be new."""
    if learn is None:
        if feature is not None:
            raise InvalidInput(f"{label} does not take a feature")
        return None
    tag = character.fields.get(learn.tag_field, "")
    candidates = sorted(
        f.name
        for f in features.values()
        if f.feature_type == learn.feature_type
        and f.tag == tag
        and (f.repeatable or f.id not in character.features)
    )
    chosen = features.get(feature or "")
    if chosen is None or chosen.feature_type != learn.feature_type or chosen.tag != tag:
        raise InvalidInput(
            f"{label} needs a {learn.feature_type} from {tag}; choose one of {candidates}"
        )
    if chosen.id in character.features and not chosen.repeatable:
        raise InvalidInput(f"{character.name} already has {chosen.name}")
    return chosen
