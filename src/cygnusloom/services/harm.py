"""Apply harm to a character, NPC, or threat. A character uses the rule set's `harm` counter
and its thresholds; a threat uses its own harm capacity. Armor subtracts unless the harm
pierces it."""

from __future__ import annotations

from typing import Any

from sqlmodel import Session

from cygnusloom.content.character import CharacterSpec
from cygnusloom.services import effects
from cygnusloom.services.death import begin_dying
from cygnusloom.services.effects import EffectContext
from cygnusloom.services.events import (
    ThreatHarmed,
    load_state,
    name_key,
    record,
)
from cygnusloom.services.state import alias_key


def apply_harm(
    db: Session,
    campaign_id: int,
    target: str,
    amount: int,
    *,
    cause: str,
    cx: EffectContext,
    plus: str = "sum",
    armor: int | None = None,
    piercing: bool = False,
) -> dict[str, Any]:
    """Apply `amount` harm to a character, NPC, or threat by name. Returns a small result."""
    state = load_state(db, campaign_id)
    threat = state.threats.get(alias_key(target))
    if threat is None:
        for candidate in state.threats.values():
            if alias_key(candidate.name) == alias_key(target):
                threat = candidate
                break
    if threat is not None:
        used_armor = threat.armor if armor is None else armor
        if piercing:
            used_armor = 0
        taken = max(0, amount - used_armor)
        after = max(0, threat.harm - taken)
        record(
            db,
            campaign_id,
            ThreatHarmed(
                id=threat.id,
                name=threat.name,
                amount=taken,
                harm=after,
                harm_max=threat.harm_max,
                cause=cause,
                broken=after <= 0,
            ),
        )
        db.commit()
        return {
            "target": threat.name,
            "kind": threat.kind,
            "amount": amount,
            "armor": used_armor,
            "taken": taken,
            "harm": after,
            "harm_max": threat.harm_max,
            "broken": after <= 0,
        }

    character = state.characters.get(name_key(target))
    if character is None:
        raise ValueError(f"there is no character or threat named {target!r}")
    rules = cx.counters.get("harm")
    if rules is None:
        raise ValueError("this rule set has no harm counter")
    used_armor = character.armor if armor is None else armor
    if piercing:
        used_armor = 0
    taken = max(0, amount - used_armor)
    before = character.counters.get(rules.id, rules.initial)
    effects.change_counter(
        db,
        campaign_id,
        character.name,
        rules,
        taken,
        absolute=False,
        cause=cause,
        cx=cx,
        plus=plus,
    )
    after = load_state(db, campaign_id).characters[name_key(character.name)].counters.get(
        rules.id, rules.initial
    )
    if rules.maximum is not None and after >= rules.maximum and not character.dead:
        if "dying" in cx.conditions:
            effects.gain_condition(db, campaign_id, character.name, "dying", cause, cx.combat)
        if cx.combat is not None:
            begin_dying(
                db,
                campaign_id,
                cx.combat,
                character.name,
                cause=cause,
                spec=cx.spec,
            )
    return {
        "target": character.name,
        "kind": character.kind,
        "amount": amount,
        "armor": used_armor,
        "taken": taken,
        "harm": after,
        "harm_max": rules.maximum,
        "before": before,
        "dying": rules.maximum is not None and after >= rules.maximum,
    }
