"""Death for player characters. A character in Critical Damage or with a Mortal Wound is dying: it
dies when its hours run out unless aided (see the aid module). A Doomed character dies on a failed
save against Critical Damage. Which conditions start the clock is set by the rule set."""

from __future__ import annotations

from sqlmodel import Session

from cygnusforge.content.library import CombatRules
from cygnusforge.services.events import (
    PC_KIND,
    CharacterDied,
    CharacterDying,
    load_state,
    name_key,
    record,
)


def begin_dying(
    db: Session, campaign_id: int, combat: CombatRules, name: str, cause: str = ""
) -> None:
    """Start a player character's clock: it dies `combat.dying_hours` hours from now unless aided.
    A character already dying or dead is left as it is."""
    character = load_state(db, campaign_id).characters.get(name_key(name))
    if character is None or character.kind != PC_KIND or character.dead:
        return
    if character.dying_hours is not None or combat.dying_hours < 1:
        return
    record(
        db,
        campaign_id,
        CharacterDying(name=character.name, hours=combat.dying_hours, cause=cause),
    )
    db.commit()


def on_critical_save_failed(db: Session, campaign_id: int, combat: CombatRules, name: str) -> None:
    """A player character failed its save against Critical Damage. A Doomed character dies at once;
    anyone else starts the one-hour clock."""
    character = load_state(db, campaign_id).characters.get(name_key(name))
    if character is None or character.kind != PC_KIND:
        return
    if any(cid in combat.fatal_conditions for cid in character.conditions):
        record(
            db,
            campaign_id,
            CharacterDied(
                name=character.name,
                cause="when the save against critical damage failed while doomed",
            ),
        )
        db.commit()
        return
    begin_dying(db, campaign_id, combat, character.name)
