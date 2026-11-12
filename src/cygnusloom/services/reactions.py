"""Feature reactions: small, rule-set-defined changes to a roll while it is being resolved.

A feature lists its reactions in the rule set's JSON. Each reaction watches one kind of roll
(a move, a check, or a reaction roll), narrows the rolls it applies to with `when`, and changes
the result with `effect`. A reaction with `uses` above zero is spent until an event carrying its
`reset` label clears it, such as a new day or the end of a school week.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from sqlmodel import Session

from cygnusloom.content.character import (
    CharacterSpec,
    Feature,
    Reaction,
    reaction_effect_text,
)
from cygnusloom.content.library import CheckRules, MoveBand, MoveRules
from cygnusloom.services import resolution
from cygnusloom.services.events import (
    Character,
    ReactionsReset,
    ReactionUsed,
    load_state,
    name_key,
    record,
)


@dataclass(frozen=True)
class Triggered:
    """A reaction that applies, and the feature that owns it."""

    feature: Feature
    reaction: Reaction
    text: str


def _features(
    character: Character,
    spec: CharacterSpec,
    extra: Mapping[str, Feature] | None,
) -> Iterable[Feature]:
    """The features the character holds, in content order, then any extra world features."""
    seen: set[str] = set()
    for feature in getattr(spec, "features", {}).values():
        if feature.id in character.features:
            seen.add(feature.id)
            yield feature
    for feature in (extra or {}).values():
        if feature.id in character.features and feature.id not in seen:
            yield feature


def _key(feature: Feature, reaction: Reaction) -> str:
    return f"{reaction.reset}:{feature.id}:{reaction.shared or reaction.id}"


def _available(character: Character, feature: Feature, reaction: Reaction) -> bool:
    if reaction.uses <= 0:
        return True
    return character.reaction_uses.get(_key(feature, reaction), 0) < reaction.uses


def _within_total(reaction: Reaction, total: int) -> bool:
    low = reaction.when.get("min_total")
    high = reaction.when.get("max_total")
    return (low is None or total >= low) and (high is None or total <= high)


def has_trigger(
    character: Character,
    spec: CharacterSpec,
    extra: Mapping[str, Feature] | None,
    trigger: str,
) -> bool:
    """Whether the character has an available reaction for this kind of roll."""
    return any(
        reaction.trigger == trigger and _available(character, feature, reaction)
        for feature in _features(character, spec, extra)
        for reaction in feature.reactions
    )


def match_attack(
    character: Character,
    spec: CharacterSpec,
    extra: Mapping[str, Feature] | None,
    total: int,
) -> Triggered | None:
    """The reaction an attack roll triggers, such as Lucky rolling it twice."""
    return _match_unfiltered(character, spec, extra, "attack", total)


def match_damage(
    character: Character,
    spec: CharacterSpec,
    extra: Mapping[str, Feature] | None,
    total: int,
) -> Triggered | None:
    """The reaction a damage roll triggers, such as Lucky rolling it twice."""
    return _match_unfiltered(character, spec, extra, "damage", total)


def _match_unfiltered(
    character: Character,
    spec: CharacterSpec,
    extra: Mapping[str, Feature] | None,
    trigger: str,
    total: int,
) -> Triggered | None:
    for feature in _features(character, spec, extra):
        for reaction in feature.reactions:
            if reaction.trigger != trigger or not _available(character, feature, reaction):
                continue
            if not _within_total(reaction, total):
                continue
            return Triggered(
                feature=feature,
                reaction=reaction,
                text=reaction_effect_text(reaction, feature),
            )
    return None


def _cause_matches(reaction: Reaction, cause: str) -> bool:
    """Whether the cause names one of the fragments a reaction watches for."""
    wanted = reaction.when.get("cause_contains")
    if not wanted:
        return True
    haystack = cause.casefold()
    return any(fragment.casefold() in haystack for fragment in wanted)


def _chosen_skill(character: Character, key: str, skill: str) -> bool:
    """Whether the character chose `skill` for the feature pick named by `key`, such as
    `focus_elf:skill`."""
    return skill in character.feature_choices.get(key, [])


def check_best(
    character: Character,
    spec: CharacterSpec,
    extra: Mapping[str, Feature] | None,
    skill: str,
) -> tuple[int, int] | None:
    """The best-of dice a check with `skill` rolls, as `(dice, keep)`, or None. The strongest
    matching reaction wins, so a rank-2 Focus replaces its rank-1 version."""
    best: tuple[int, int] | None = None
    for feature in _features(character, spec, extra):
        for reaction in feature.reactions:
            if reaction.trigger != "check" or reaction.effect.get("op") != "best_of":
                continue
            when = reaction.when
            choice = when.get("skill_choice")
            if choice and not _chosen_skill(character, choice, skill):
                continue
            if when.get("skills") and skill not in when["skills"]:
                continue
            dice = int(reaction.effect.get("dice", 0))
            keep = int(reaction.effect.get("keep", 0))
            if dice < 2 or keep < 1 or keep >= dice:
                continue
            if best is None or (keep, dice) > (best[1], best[0]):
                best = (dice, keep)
    return best


def match_move(
    character: Character,
    spec: CharacterSpec,
    extra: Mapping[str, Feature] | None,
    move: MoveRules,
    band: MoveBand,
    total: int,
    stat: str,
) -> Triggered | None:
    """The reaction a move triggers, the most specific first."""
    candidates: list[tuple[int, int, Triggered]] = []
    for order, feature in enumerate(_features(character, spec, extra)):
        for reaction in feature.reactions:
            if reaction.trigger != "move" or not _available(character, feature, reaction):
                continue
            when = reaction.when
            if not _within_total(reaction, total):
                continue
            if when.get("moves") and move.id not in when["moves"]:
                continue
            if when.get("move_tags") and not set(when["move_tags"]) & set(move.tags):
                continue
            if when.get("bands") and band.label not in when["bands"]:
                continue
            if when.get("stats") and stat not in when["stats"]:
                continue
            specificity = 2 if when.get("moves") else 1 if when.get("move_tags") else 0
            candidates.append(
                (
                    -specificity,
                    order,
                    Triggered(
                        feature=feature,
                        reaction=reaction,
                        text=reaction_effect_text(reaction, feature),
                    ),
                )
            )
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item[0], item[1]))[2]


def offered_check(
    character: Character,
    spec: CharacterSpec,
    extra: Mapping[str, Feature] | None,
    check: CheckRules,
    skill: str,
    stat: str,
    cause: str = "",
) -> tuple[str, ...]:
    """The optional check reactions that could apply, as feature names, so the roll prompt
    can ask the player whether to use one. Success and total filters wait for the roll."""
    names: list[str] = []
    for feature in _features(character, spec, extra):
        for reaction in feature.reactions:
            if (
                reaction.trigger != "check"
                or not reaction.optional
                or not _available(character, feature, reaction)
                or reaction.effect.get("op") == "best_of"
            ):
                continue
            when = reaction.when
            if not _cause_matches(reaction, cause):
                continue
            if when.get("checks") and check.id not in when["checks"]:
                continue
            if when.get("skills") and skill not in when["skills"]:
                continue
            if when.get("skill_choice") and not _chosen_skill(
                character, when["skill_choice"], skill
            ):
                continue
            if when.get("stats") and stat not in when["stats"]:
                continue
            if feature.name not in names:
                names.append(feature.name)
    return tuple(names)


def match_check(
    character: Character,
    spec: CharacterSpec,
    extra: Mapping[str, Feature] | None,
    check: CheckRules,
    skill: str,
    stat: str,
    total: int,
    success: bool,
    cause: str = "",
    use_optional: bool = True,
) -> Triggered | None:
    """The reaction a check triggers, the most specific first. An optional reaction fires
    only when the player allowed it in the roll prompt."""
    candidates: list[tuple[int, int, Triggered]] = []
    for order, feature in enumerate(_features(character, spec, extra)):
        for reaction in feature.reactions:
            if reaction.trigger != "check" or not _available(character, feature, reaction):
                continue
            if reaction.optional and not use_optional:
                continue
            if reaction.effect.get("op") == "best_of":
                continue
            when = reaction.when
            if not _within_total(reaction, total) or not _cause_matches(reaction, cause):
                continue
            if when.get("checks") and check.id not in when["checks"]:
                continue
            if when.get("skills") and skill not in when["skills"]:
                continue
            if when.get("skill_choice") and not _chosen_skill(
                character, when["skill_choice"], skill
            ):
                continue
            if when.get("stats") and stat not in when["stats"]:
                continue
            if "success" in when and success is not when["success"]:
                continue
            specificity = 0
            if when.get("checks"):
                specificity = 3
            elif when.get("skills") or when.get("skill_choice"):
                specificity = 2
            elif when.get("cause_contains"):
                specificity = 1
            candidates.append(
                (
                    -specificity,
                    order,
                    Triggered(
                        feature=feature,
                        reaction=reaction,
                        text=reaction_effect_text(reaction, feature),
                    ),
                )
            )
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item[0], item[1]))[2]


def offered_save(
    character: Character,
    spec: CharacterSpec,
    extra: Mapping[str, Feature] | None,
    stat: str,
    cause: str = "",
) -> tuple[str, ...]:
    """The optional save reactions that could apply, as feature names."""
    names: list[str] = []
    for feature in _features(character, spec, extra):
        for reaction in feature.reactions:
            if (
                reaction.trigger != "save"
                or not reaction.optional
                or not _available(character, feature, reaction)
            ):
                continue
            when = reaction.when
            if not _cause_matches(reaction, cause):
                continue
            if when.get("stats") and stat not in when["stats"]:
                continue
            if feature.name not in names:
                names.append(feature.name)
    return tuple(names)


def match_save(
    character: Character,
    spec: CharacterSpec,
    extra: Mapping[str, Feature] | None,
    stat: str,
    total: int,
    success: bool,
    cause: str = "",
    use_optional: bool = True,
) -> Triggered | None:
    """The reaction a saving throw triggers, the most specific first. An optional reaction
    fires only when the player allowed it in the roll prompt."""
    candidates: list[tuple[int, int, Triggered]] = []
    for order, feature in enumerate(_features(character, spec, extra)):
        for reaction in feature.reactions:
            if reaction.trigger != "save" or not _available(character, feature, reaction):
                continue
            if reaction.optional and not use_optional:
                continue
            when = reaction.when
            if not _within_total(reaction, total) or not _cause_matches(reaction, cause):
                continue
            if when.get("stats") and stat not in when["stats"]:
                continue
            if "success" in when and success is not when["success"]:
                continue
            specificity = 2 if when.get("stats") else 1 if when.get("cause_contains") else 0
            candidates.append(
                (
                    -specificity,
                    order,
                    Triggered(
                        feature=feature,
                        reaction=reaction,
                        text=reaction_effect_text(reaction, feature),
                    ),
                )
            )
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item[0], item[1]))[2]


def apply_save_effect(
    triggered: Triggered,
    result: resolution.RollResult,
    *,
    reroll: Callable[[], resolution.RollResult],
    resolve_total: Callable[[int], resolution.RollResult],
) -> resolution.RollResult:
    """Apply a save reaction to a save already resolved, returning the result to record."""
    effect = triggered.reaction.effect
    if effect["op"] == "reroll":
        rerolled = reroll()
        keep = "better" if effect.get("keep", "better") == "better" else "worse"
        better = (rerolled.passed and not result.passed) or (
            rerolled.passed == result.passed and rerolled.roll > result.roll
        )
        worse = (result.passed and not rerolled.passed) or (
            rerolled.passed == result.passed and rerolled.roll < result.roll
        )
        if (keep == "better" and better) or (keep == "worse" and worse):
            return rerolled
    elif effect["op"] == "add_total":
        return resolve_total(result.roll + int(effect["amount"]))
    return result


def match_reaction_roll(
    character: Character,
    spec: CharacterSpec,
    extra: Mapping[str, Feature] | None,
    total: int,
) -> list[Triggered]:
    """Every reaction a reaction roll can use, in content order. The caller applies them in
    order: unlimited modifiers first, then the first limited result change."""
    found: list[Triggered] = []
    for feature in _features(character, spec, extra):
        for reaction in feature.reactions:
            if reaction.trigger != "reaction" or not _available(character, feature, reaction):
                continue
            if not _within_total(reaction, total):
                continue
            found.append(
                Triggered(
                    feature=feature,
                    reaction=reaction,
                    text=reaction_effect_text(reaction, feature),
                )
            )
    return found


def use(db: Session, campaign_id: int, character: Character, triggered: Triggered) -> None:
    """Record a limited reaction's use. An unlimited reaction leaves no event."""
    if triggered.reaction.uses <= 0:
        return
    record(
        db,
        campaign_id,
        ReactionUsed(
            name=character.name,
            feature=triggered.feature.id,
            feature_name=triggered.feature.name,
            reaction=triggered.reaction.shared or triggered.reaction.id,
            reset=triggered.reaction.reset,
            text=triggered.text,
        ),
    )


def reset(db: Session, campaign_id: int, reset: str, label: str = "", name: str = "") -> None:
    """Clear the uses carrying `reset`, for every character or, when `name` is given, for that
    character alone. Characters with nothing to clear leave no event."""
    prefix = f"{reset}:"
    wanted = name_key(name) if name else ""
    for character in load_state(db, campaign_id).characters.values():
        if wanted and name_key(character.name) != wanted:
            continue
        if any(key.startswith(prefix) for key in character.reaction_uses):
            record(
                db,
                campaign_id,
                ReactionsReset(name=character.name, reset=reset, label=label),
            )


__all__ = [
    "Triggered",
    "apply_save_effect",
    "check_best",
    "has_trigger",
    "match_attack",
    "match_check",
    "match_damage",
    "match_move",
    "match_reaction_roll",
    "match_save",
    "reset",
    "use",
]
