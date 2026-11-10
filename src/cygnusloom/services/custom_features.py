"""Features invented during play, such as researched spells, potions, and enchantments.

A custom feature lives on its character rather than in the loaded rule set, so it can be created
by the GM or player without editing content. Everything that reads a rule set feature reads these
too: the sheet, feature lookups, and the compendium.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from sqlmodel import Session

from cygnusloom.content.character import Feature, FeatureTypeRules
from cygnusloom.services.common import InvalidInput, bounded_text, whole_number
from cygnusloom.services.events import (
    CustomFeatureCreated,
    CustomFeatureRemoved,
    CustomFeatureUpdated,
    load_state,
    name_key,
    record,
)
from cygnusloom.services.state import Character, CustomFeature

MAX_NAME = 120
MAX_TEXT = 2000
MAX_MODS = 12
CUSTOM_TYPES = ("spell", "potion", "enchantment", "item", "trait", "feat")
_SLUG = re.compile(r"[^a-z0-9]+")


def _slug(value: str) -> str:
    return _SLUG.sub("-", value.casefold()).strip("-") or "feature"


def _character(state, name: str) -> Character:
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    return character


def _optional(value: object, label: str, limit: int = MAX_TEXT) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    return bounded_text(value, label, limit)


def _mods(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise InvalidInput("mods must be a list of short text tags")
    mods = [bounded_text(item, "mod", MAX_NAME) for item in value]
    if len(mods) > MAX_MODS:
        raise InvalidInput(f"a custom feature takes at most {MAX_MODS} mods")
    return mods


def _unique_id(character: Character, name: str) -> str:
    base = f"custom-{_slug(name)}"
    candidate = base
    index = 2
    while candidate in character.custom_features:
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def create_custom_feature(
    db: Session,
    campaign_id: int,
    character_name: str,
    name: str,
    feature_type: str,
    description: str,
    *,
    tag: str = "",
    rank: int = 0,
    subject: str = "",
    base: str = "",
    mods: object = None,
    stress: int = 0,
    cause: str = "",
    feature_types: Mapping[str, FeatureTypeRules] | None = None,
) -> dict[str, Any]:
    """Create and learn a feature invented during play."""
    state = load_state(db, campaign_id)
    character = _character(state, character_name)
    feature_name = bounded_text(name, "name", MAX_NAME)
    if any(
        existing.name.casefold() == feature_name.casefold()
        for existing in character.custom_features.values()
    ):
        raise InvalidInput(f"this character already has a custom feature named {feature_name!r}")
    allowed_types = tuple(feature_types) if feature_types else CUSTOM_TYPES
    kind = bounded_text(feature_type, "feature type", MAX_NAME).casefold()
    if kind not in allowed_types:
        raise InvalidInput(f"feature type must be one of {', '.join(allowed_types)}")
    text = bounded_text(description, "description", MAX_TEXT)
    feature_id = _unique_id(character, feature_name)
    record(
        db,
        campaign_id,
        CustomFeatureCreated(
            name=character.name,
            feature_id=feature_id,
            feature_name=feature_name,
            feature_type=kind,
            description=text,
            tag=_optional(tag, "tag", MAX_NAME),
            rank=whole_number(rank, "rank", 0, 20),
            subject=_optional(subject, "subject", MAX_NAME),
            base=_optional(base, "base", MAX_TEXT),
            mods=_mods(mods),
            stress=whole_number(stress, "stress", 0, 20),
            cause=_optional(cause, "cause", MAX_NAME),
        ),
    )
    db.commit()
    state = load_state(db, campaign_id)
    return feature_view(state.characters[name_key(character_name)].custom_features[feature_id])


def update_custom_feature(
    db: Session,
    campaign_id: int,
    character_name: str,
    feature: str,
    *,
    name: str | None = None,
    description: str | None = None,
    tag: str | None = None,
    rank: int | None = None,
    subject: str | None = None,
    base: str | None = None,
    mods: object = None,
    stress: int | None = None,
) -> dict[str, Any]:
    """Change one of a character's invented features."""
    state = load_state(db, campaign_id)
    character = _character(state, character_name)
    found = find_custom_feature(character, feature)
    if found is None:
        raise InvalidInput(f"there is no custom feature named {feature!r}")
    record(
        db,
        campaign_id,
        CustomFeatureUpdated(
            name=character.name,
            feature_id=found.id,
            feature_name=bounded_text(name, "name", MAX_NAME) if name is not None else None,
            description=(
                bounded_text(description, "description", MAX_TEXT)
                if description is not None
                else None
            ),
            tag=_optional(tag, "tag", MAX_NAME) if tag is not None else None,
            rank=whole_number(rank, "rank", 0, 20) if rank is not None else None,
            subject=(_optional(subject, "subject", MAX_NAME) if subject is not None else None),
            base=_optional(base, "base", MAX_TEXT) if base is not None else None,
            mods=_mods(mods) if mods is not None else None,
            stress=whole_number(stress, "stress", 0, 20) if stress is not None else None,
        ),
    )
    db.commit()
    state = load_state(db, campaign_id)
    return feature_view(state.characters[name_key(character_name)].custom_features[found.id])


def remove_custom_feature(
    db: Session, campaign_id: int, character_name: str, feature: str
) -> dict[str, Any]:
    """Give up one of a character's invented features."""
    state = load_state(db, campaign_id)
    character = _character(state, character_name)
    found = find_custom_feature(character, feature)
    if found is None:
        raise InvalidInput(f"there is no custom feature named {feature!r}")
    record(
        db,
        campaign_id,
        CustomFeatureRemoved(
            name=character.name,
            feature_id=found.id,
            feature_name=found.name,
        ),
    )
    db.commit()
    return {"character": character.name, "feature": found.name}


def list_custom_features(
    db: Session, campaign_id: int, character_name: str
) -> list[dict[str, Any]]:
    state = load_state(db, campaign_id)
    character = _character(state, character_name)
    return [feature_view(feature) for feature in character.custom_features.values()]


def find_custom_feature(character: Character, feature: str) -> CustomFeature | None:
    """Find one of a character's invented features by id or name."""
    wanted = feature.strip()
    found = character.custom_features.get(wanted)
    if found is not None:
        return found
    return next(
        (
            candidate
            for candidate in character.custom_features.values()
            if candidate.name.casefold() == wanted.casefold()
        ),
        None,
    )


def find_custom_feature_in_state(state, feature: str) -> tuple[Character, CustomFeature] | None:
    """Find an invented feature across the campaign's characters."""
    for character in state.characters.values():
        found = find_custom_feature(character, feature)
        if found is not None:
            return character, found
    return None


def feature_view(feature: CustomFeature) -> dict[str, Any]:
    return {
        "id": feature.id,
        "name": feature.name,
        "feature_type": feature.feature_type,
        "description": feature.description,
        "tag": feature.tag,
        "rank": feature.rank,
        "subject": feature.subject,
        "base": feature.base,
        "mods": list(feature.mods),
        "stress": feature.stress,
        "cause": feature.cause,
        "custom": True,
    }


def as_feature(feature: CustomFeature) -> Feature:
    """Present an invented feature as the rule set's Feature for the sheet and compendium."""
    return Feature(
        id=feature.id,
        name=feature.name,
        feature_type=feature.feature_type,
        description=feature.description,
        size="",
        uses=0,
        armor=0,
        tag=feature.tag,
        rank=feature.rank,
    )


def merged_features(spec, character: Character) -> Mapping[str, Feature]:
    """The rule set's features plus the character's invented features."""
    features = dict(spec.features)
    for feature_id in character.features:
        custom = character.custom_features.get(feature_id)
        if custom is not None:
            features[feature_id] = as_feature(custom)
    return features


def feature_text(feature: CustomFeature) -> str:
    lines = [feature.description]
    if feature.subject:
        lines.append(f"Subject: {feature.subject}")
    if feature.base:
        lines.append(f"Base: {feature.base}")
    if feature.mods:
        lines.append("Mods: " + ", ".join(feature.mods))
    if feature.stress:
        lines.append(f"Stress: {feature.stress}")
    return "\n".join(lines)
