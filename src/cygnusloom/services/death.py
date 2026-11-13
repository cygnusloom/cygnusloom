"""Death for player characters. A character with a dying condition dies when its hours run out
unless aided (see the aid module). A character with a fatal condition dies on a failed critical
save. Which conditions are dying or fatal is set by the rule set."""

from __future__ import annotations

from sqlmodel import Session

from cygnusloom.content.character import CharacterSpec
from cygnusloom.content.library import CombatRules
from cygnusloom.services.events import (
    PC_KIND,
    CharacterDied,
    CharacterDying,
    CharacterStabilized,
    DyingRoundsPassed,
    load_state,
    name_key,
    record,
)


def _stabilizing_feature(character: object, spec: CharacterSpec | None) -> str:
    """The name of a feature that steadies its holder when mortally wounded, or empty."""
    if spec is None or not hasattr(character, "features"):
        return ""
    for feature_id in character.features:
        feature = spec.features.get(feature_id)
        if feature is not None and feature.stabilizes:
            return feature.name
    return ""


def begin_dying(
    db: Session,
    campaign_id: int,
    combat: CombatRules,
    name: str,
    cause: str = "",
    *,
    spec: CharacterSpec | None = None,
) -> None:
    """Start a player character's clock: it dies `combat.dying_hours` hours from now, or in
    `combat.dying_rounds` encounter rounds when the rule set gives that clock, unless aided. A
    character already dying or dead is left as it is."""
    character = load_state(db, campaign_id).characters.get(name_key(name))
    if character is None or character.dead:
        return
    if character.kind != PC_KIND and not character.important:
        return
    if character.dying_hours is not None or character.dying_rounds is not None:
        return
    if combat.dying_hours < 1 and combat.dying_rounds < 1:
        return
    stabilizer = _stabilizing_feature(character, spec)
    if stabilizer:
        record(
            db,
            campaign_id,
            CharacterStabilized(name=character.name, by=stabilizer),
        )
        db.commit()
        return
    record(
        db,
        campaign_id,
        CharacterDying(
            name=character.name,
            hours=combat.dying_hours,
            rounds=combat.dying_rounds or None,
            cause=cause,
        ),
    )
    db.commit()


def tick_dying_rounds(db: Session, campaign_id: int) -> None:
    """A round passed. Each dying character with a round clock loses one round; at zero it dies.
    Records one event per character whose clock moved."""
    state = load_state(db, campaign_id)
    changed = False
    for character in state.characters.values():
        if character.kind != PC_KIND or character.dead or character.dying_rounds is None:
            continue
        rounds = max(0, character.dying_rounds - 1)
        record(db, campaign_id, DyingRoundsPassed(name=character.name, rounds=rounds))
        changed = True
        if rounds == 0:
            record(
                db,
                campaign_id,
                CharacterDied(name=character.name, cause="of mortal wounds"),
            )
    if changed:
        db.commit()


def on_critical_save_failed(
    db: Session,
    campaign_id: int,
    combat: CombatRules,
    name: str,
    *,
    spec: CharacterSpec | None = None,
) -> None:
    """A player character failed its critical save. A character with a fatal condition dies at once;
    anyone else starts the dying clock, as set by the rule set."""
    character = load_state(db, campaign_id).characters.get(name_key(name))
    if character is None or (character.kind != PC_KIND and not character.important):
        return
    if any(cid in combat.fatal_conditions for cid in character.conditions):
        record(
            db,
            campaign_id,
            CharacterDied(
                name=character.name,
                cause="when the critical save failed while under a fatal condition",
            ),
        )
        db.commit()
        return
    begin_dying(db, campaign_id, combat, character.name, spec=spec)
