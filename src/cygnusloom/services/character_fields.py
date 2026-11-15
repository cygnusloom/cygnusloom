"""The rule set's character fields a player may change at any time.

A character field is flavor or mechanical. Flavor is identity and colour: a look, an accent, a
biography. A player owns their player character's flavor and may rewrite it whenever they like,
while a hireling's flavor belongs to the GM who invented them. A hireling's mechanical fields,
such as a student's College, are the player's to set. Everything else on a sheet changes through
creation, a respec, or an advancement.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import Engine

from cygnusloom.content.character import CharacterSpec, Field
from cygnusloom.services.common import InvalidInput, session_scope
from cygnusloom.services.events import (
    PC_KIND,
    Character,
    CharacterFieldSet,
    load_state,
    name_key,
    record,
)
from cygnusloom.services.state import CampaignState

# A field's text, matching the length a GM's set_character_field allows.
MAX_FIELD_TEXT = 2000


def editable_fields(spec: CharacterSpec, character: Character) -> tuple[Field, ...]:
    """The character fields this player may change on this character. A player character's
    flavor is the player's own; a hireling's flavor is the GM's, so only its mechanical fields
    are offered. A character with no player-faced sheet, such as one whose rule set defines no
    fields, has nothing to edit."""
    if character.kind != PC_KIND or character.out_of_play:
        return ()
    flavor = not character.hireling
    return tuple(field for field in spec.fields.values() if field.flavor == flavor)


def controls_character(state: CampaignState, player_id: int, character: Character) -> bool:
    """Whether this account plays the character: its own player character, or a hireling of a
    character it plays for. A hireling's employer chain is followed, so a hireling of a hireling
    belongs to the same player."""
    current = character
    seen: set[str] = set()
    while current.hireling:
        key = name_key(current.employer)
        if not current.employer or key in seen:
            return False
        seen.add(key)
        employer = state.characters.get(key)
        if employer is None:
            return False
        current = employer
    return state.owned_by(current, player_id)


def character_fields(
    engine: Engine,
    player_id: int,
    campaign_id: int,
    character_name: str,
    spec: CharacterSpec,
) -> tuple[Character, tuple[Field, ...]]:
    """The character and the fields this account may change on it. Anything else looks like a
    missing character, so another player's sheet cannot be probed."""
    with session_scope(engine) as db:
        state = load_state(db, campaign_id)
        character = state.characters.get(name_key(character_name))
        if character is None or not controls_character(state, player_id, character):
            raise InvalidInput("only a character you play can be changed")
        fields = editable_fields(spec, character)
        if not fields:
            raise InvalidInput(f"{character.name} has no details you can change")
        return character, fields


def set_character_fields(
    engine: Engine,
    player_id: int,
    campaign_id: int,
    character_name: str,
    submitted: Mapping[str, str],
    spec: CharacterSpec,
) -> tuple[str, ...]:
    """Change the character fields this account may edit, recording one event per changed field.
    A value outside a field's options is refused unless the field is flavor, which also takes the
    player's own words. Returns the names of the fields that changed."""
    with session_scope(engine) as db:
        state = load_state(db, campaign_id)
        character = state.characters.get(name_key(character_name))
        if character is None or not controls_character(state, player_id, character):
            raise InvalidInput("only a character you play can be changed")
        editable = {field.id: field for field in editable_fields(spec, character)}
        if not editable:
            raise InvalidInput(f"{character.name} has no details you can change")
        changes: list[tuple[Field, str]] = []
        for field_id, raw in submitted.items():
            field = editable.get(field_id)
            if field is None:
                raise InvalidInput(f"{field_id!r} is not a detail you can change")
            changes.append((field, _clean_value(spec, field, raw)))
        changed = [
            (field, value)
            for field, value in changes
            if character.fields.get(field.id, "") != value
        ]
        for field, value in changed:
            record(
                db,
                campaign_id,
                CharacterFieldSet(
                    name=character.name,
                    field=field.id,
                    label=field.name,
                    value=value,
                ),
            )
        db.commit()
    return tuple(field.name for field, _ in changed)


def _clean_value(spec: CharacterSpec, field: Field, raw: str) -> str:
    """A submitted field value: trimmed, bounded, and, for a mechanical table field, one of the
    table's options. A flavor field may use the player's own words."""
    value = (raw or "").strip()
    if len(value) > MAX_FIELD_TEXT:
        raise InvalidInput(f"{field.name} must be at most {MAX_FIELD_TEXT} characters")
    if field.table is not None and value and not field.flavor:
        allowed = [option.value for option in spec.tables[field.table]]
        if value not in allowed:
            raise InvalidInput(f"{value!r} is not a choice of {field.name}")
    return value
