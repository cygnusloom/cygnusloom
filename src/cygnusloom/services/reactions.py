"""Feature reactions: small, rule-set-defined changes to a roll while it is being resolved.

A feature lists its reactions in the rule set's JSON. Each reaction watches one kind of roll
(a move, a check, or a reaction roll), narrows the rolls it applies to with `when`, and changes
the result with `effect`. A reaction with `uses` above zero is spent until an event carrying its
`reset` label clears it, such as a new day or the end of a school week.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from sqlmodel import Session

from cygnusloom.content.character import (
    CharacterSpec,
    Feature,
    Reaction,
    reaction_effect_text,
)
from cygnusloom.content.library import CheckRules, MoveBand, MoveRules
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
    return f"{reaction.reset}:{feature.id}:{reaction.id}"


def _available(character: Character, feature: Feature, reaction: Reaction) -> bool:
    if reaction.uses <= 0:
        return True
    return character.reaction_uses.get(_key(feature, reaction), 0) < reaction.uses


def _within_total(reaction: Reaction, total: int) -> bool:
    low = reaction.when.get("min_total")
    high = reaction.when.get("max_total")
    return (low is None or total >= low) and (high is None or total <= high)


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


def match_check(
    character: Character,
    spec: CharacterSpec,
    extra: Mapping[str, Feature] | None,
    check: CheckRules,
    skill: str,
    stat: str,
    total: int,
    success: bool,
) -> Triggered | None:
    """The reaction a check triggers, the most specific first."""
    candidates: list[tuple[int, int, Triggered]] = []
    for order, feature in enumerate(_features(character, spec, extra)):
        for reaction in feature.reactions:
            if reaction.trigger != "check" or not _available(character, feature, reaction):
                continue
            when = reaction.when
            if not _within_total(reaction, total):
                continue
            if when.get("checks") and check.id not in when["checks"]:
                continue
            if when.get("skills") and skill not in when["skills"]:
                continue
            if when.get("stats") and stat not in when["stats"]:
                continue
            if "success" in when and success is not when["success"]:
                continue
            specificity = 2 if when.get("checks") else 1 if when.get("skills") else 0
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
            reaction=triggered.reaction.id,
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
    "match_check",
    "match_move",
    "match_reaction_roll",
    "reset",
    "use",
]
