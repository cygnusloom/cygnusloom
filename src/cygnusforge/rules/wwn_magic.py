"""Worlds Without Number magic: prepared spells, daily castings, Effort, and System Strain.

Prepared spells and committed Effort live in character marks, the way the Cairn rule set keeps
deprivation, so they replay from the log:

- ``prepared`` is a list of spell feature ids, replaced by each `prepare_spells` call.
- ``effort_committed`` is keyed by duration and then tradition, such as
  ``{"scene": {"high": 1}, "day": {"necromancer": 2}}``. Each point is also recorded as harm on the
  tradition's Effort stat, so `current(effort_high)` is what is left.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from cygnusforge.content.character import CharacterSpec
from cygnusforge.rules.context import ToolContext
from cygnusforge.rules.dice import roll_total
from cygnusforge.rules.wwn import (
    EFFORT_DURATIONS,
    EFFORT_MARK,
    casting_row,
    max_effort,
    tradition_effort_stat,
    tradition_tag,
)
from cygnusforge.services.common import InvalidInput, whole_number
from cygnusforge.services.events import (
    CheckMade,
    EffectNoted,
    Harmed,
    SpellCast,
    StatRestored,
    current,
    name_key,
    record,
)
from cygnusforge.services.hooks import HookContext

# The character mark that holds the spell ids prepared for the day.
PREPARED_MARK = "prepared"
MAX_EFFORT_COMMIT = 100
MAX_STRAIN = 100
MAX_SPELLS = 50


def prepare_spells(character: str, spells: Sequence[str], ctx: ToolContext) -> dict[str, Any]:
    """Prepare a set of known spells. The list replaces the old set, and may hold no more spells
    than the character's class, level, and tradition allow."""
    spec = ctx.world.character
    chosen = [str(spell) for spell in spells]
    if len(set(chosen)) != len(chosen):
        raise InvalidInput("a prepared spell may not be listed twice")
    if len(chosen) > MAX_SPELLS:
        raise InvalidInput(f"prepare at most {MAX_SPELLS} spells")

    def check(hc: HookContext) -> dict[str, Any]:
        _, found = _character(hc, character)
        for spell in chosen:
            feature = _spell(spec, spell)
            if spell not in found.features:
                raise InvalidInput(f"{found.name} does not know {feature.name}")
        tradition = found.templates.get("tradition", "")
        if not tradition:
            raise InvalidInput(f"{found.name} has no magical tradition")
        row = casting_row(found.templates.get("class", ""), found.counters.get("level", 1))
        if row is None:
            raise InvalidInput(f"{found.name}'s class cannot cast spells")
        if len(chosen) > row.prepared:
            raise InvalidInput(
                f"{found.name} may prepare {row.prepared} spells at level "
                f"{found.counters.get('level', 1)}, not {len(chosen)}"
            )
        return {"limit": row.prepared, "tradition": tradition}

    allowed = ctx.world.with_hook_context(check)

    def write(hc: HookContext) -> dict[str, Any]:
        _, found = _character(hc, character)
        names = ", ".join(spec.features[spell].name for spell in chosen) or "no spells"
        hc.set_mark(
            found.name,
            PREPARED_MARK,
            chosen or None,
            f"{found.name} prepares {names}.",
        )
        return {
            "character": found.name,
            "prepared": chosen,
            "limit": allowed["limit"],
            "tradition": allowed["tradition"],
        }

    return ctx.world.with_hook_context(write)


def cast_spell(character: str, spell: str, ctx: ToolContext) -> dict[str, Any]:
    """Cast a prepared spell, spending one of the character's daily castings."""
    spec = ctx.world.character

    def action(hc: HookContext) -> dict[str, Any]:
        _, found = _character(hc, character)
        feature = _spell(spec, spell)
        prepared = found.marks.get(PREPARED_MARK) or []
        if spell not in prepared:
            raise InvalidInput(f"{found.name} has not prepared {feature.name}")
        if "castings" not in found.stats:
            raise InvalidInput(f"{found.name} has no castings stat")
        if current(found, "castings") < 1:
            raise InvalidInput(f"{found.name} has no spell castings left")
        db, campaign_id = hc.session()
        record(
            db,
            campaign_id,
            Harmed(
                name=found.name,
                stat="castings",
                taken=1,
                cause=f"casting {feature.name}",
            ),
        )
        record(
            db,
            campaign_id,
            SpellCast(name=found.name, spell=feature.name, source="a prepared spell"),
        )
        return {
            "character": found.name,
            "spell": spell,
            "name": feature.name,
            "castings_left": current(found, "castings") - 1,
        }

    return ctx.world.with_hook_context(action)


def commit_effort(
    character: str, tradition: str, amount: int, duration: str, ctx: ToolContext
) -> dict[str, Any]:
    """Commit Effort to a tradition for a scene, a day, or indefinitely. The amount is limited by
    the character's maximum Effort and by what is left of the tradition's pool."""
    spec = ctx.world.character
    value = whole_number(amount, "amount", 1, MAX_EFFORT_COMMIT)
    duration = _duration(duration)
    stat = tradition_effort_stat(tradition)
    if not stat:
        raise InvalidInput(f"there is no Effort pool for the tradition {tradition!r}")
    tag = tradition_tag(tradition)

    def action(hc: HookContext) -> dict[str, Any]:
        _, found = _character(hc, character)
        if stat not in found.stats:
            raise InvalidInput(f"{found.name} has no {stat} stat")
        maximum = max_effort(spec, found.stats, found.skills, found.templates.get("class", ""))
        marks = found.marks.get(EFFORT_MARK) or {}
        committed = sum(
            int(held.get(tag, 0)) for held in marks.values() if isinstance(held, Mapping)
        )
        if committed + value > maximum:
            raise InvalidInput(
                f"{found.name}'s {tag} Effort is limited to {maximum}; {committed} is already "
                f"committed, so at most {max(0, maximum - committed)} more can be"
            )
        if current(found, stat) < value:
            raise InvalidInput(
                f"{found.name} has only {current(found, stat)} {stat} left to commit"
            )
        db, campaign_id = hc.session()
        record(
            db,
            campaign_id,
            Harmed(
                name=found.name,
                stat=stat,
                taken=value,
                cause=f"{duration} Effort for {tag}",
            ),
        )
        updated = {key: dict(held) for key, held in marks.items()}
        held = updated.setdefault(duration, {})
        held[tag] = int(held.get(tag, 0)) + value
        hc.set_mark(
            found.name,
            EFFORT_MARK,
            updated,
            f"{found.name} commits {value} {duration} Effort to {tag}.",
        )
        return {
            "character": found.name,
            "tradition": tag,
            "stat": stat,
            "committed": committed + value,
            "duration": duration,
            "remaining": current(found, stat) - value,
            "maximum": maximum,
        }

    return ctx.world.with_hook_context(action)


def recover_effort(
    character: str, tradition: str, duration: str, ctx: ToolContext
) -> dict[str, Any]:
    """Reclaim the Effort committed to one tradition for one duration."""
    duration = _duration(duration)
    stat = tradition_effort_stat(tradition)
    if not stat:
        raise InvalidInput(f"there is no Effort pool for the tradition {tradition!r}")
    tag = tradition_tag(tradition)

    def action(hc: HookContext) -> dict[str, Any]:
        _, found = _character(hc, character)
        marks = found.marks.get(EFFORT_MARK) or {}
        held = dict(marks.get(duration) or {})
        amount = int(held.get(tag, 0))
        if amount <= 0:
            raise InvalidInput(f"{found.name} has no {duration} Effort committed to {tag}")
        db, campaign_id = hc.session()
        record(
            db,
            campaign_id,
            StatRestored(
                name=found.name,
                stat=stat,
                amount=amount,
                cause=f"{duration} Effort reclaimed",
            ),
        )
        updated = {key: dict(held) for key, held in marks.items()}
        del held[tag]
        if held:
            updated[duration] = held
        else:
            updated.pop(duration, None)
        committed = sum(int(held.get(tag, 0)) for held in updated.values())
        hc.set_mark(
            found.name,
            EFFORT_MARK,
            updated or None,
            f"{found.name} reclaims {amount} {duration} Effort from {tag}.",
        )
        return {
            "character": found.name,
            "tradition": tag,
            "stat": stat,
            "recovered": amount,
            "committed": committed,
        }

    return ctx.world.with_hook_context(action)


def add_strain(character: str, amount: int, cause: str, ctx: ToolContext) -> dict[str, Any]:
    """Add System Strain, never above the character's Strain score."""
    value = whole_number(amount, "amount", 1, MAX_STRAIN)

    def action(hc: HookContext) -> dict[str, Any]:
        _, found = _character(hc, character)
        if "strain" not in found.stats:
            raise InvalidInput(f"{found.name} has no System Strain")
        available = current(found, "strain")
        if available <= 0:
            raise InvalidInput(f"{found.name}'s System Strain is already at its maximum")
        taken = min(value, available)
        db, campaign_id = hc.session()
        record(
            db,
            campaign_id,
            Harmed(name=found.name, stat="strain", taken=taken, cause=cause),
        )
        return {
            "character": found.name,
            "strain": available - taken,
            "added": taken,
        }

    return ctx.world.with_hook_context(action)


def heal_strain(character: str, amount: int, cause: str, ctx: ToolContext) -> dict[str, Any]:
    """Recover System Strain, down to none."""
    value = whole_number(amount, "amount", 1, MAX_STRAIN)
    return ctx.world.restore_stat(character, "strain", value, cause)


def first_aid(healer: str, target: str, ctx: ToolContext) -> dict[str, Any]:
    """Roll 1d6 plus Heal skill, restore that much of the target's lost hit points (never above
    their maximum), and add one System Strain to them."""
    spec = ctx.world.character

    def action(hc: HookContext) -> dict[str, Any]:
        state = hc.state()
        doctor = state.characters.get(name_key(healer))
        if doctor is None:
            raise InvalidInput(f"there is no character named {healer!r}")
        patient = state.characters.get(name_key(target))
        if patient is None:
            raise InvalidInput(f"there is no character named {target!r}")
        if patient.dead:
            raise InvalidInput(f"{patient.name} is dead and cannot be helped by first aid")
        if "hp" not in patient.stats:
            raise InvalidInput(f"{patient.name} has no hit points")
        level = spec.skill_level(doctor.skills, "heal")
        bonus = level if level >= 0 else -1
        roll = roll_total("1d6", ctx.world.rng)
        total = roll + bonus
        healed = min(max(0, total), patient.harm.get("hp", 0))
        db, campaign_id = hc.session()
        record(
            db,
            campaign_id,
            CheckMade(
                name=doctor.name,
                check="First Aid",
                skill="heal",
                skill_name="Heal",
                skill_level=level,
                roll=roll,
                bonus=bonus,
                total=total,
                difficulty=0,
                success=healed > 0,
                cause=f"first aid for {patient.name}",
            ),
        )
        if healed:
            record(
                db,
                campaign_id,
                StatRestored(
                    name=patient.name,
                    stat="hp",
                    amount=healed,
                    cause=f"first aid from {doctor.name}",
                ),
            )
        strain = 0
        if "strain" in patient.stats and current(patient, "strain") > 0:
            record(
                db,
                campaign_id,
                Harmed(
                    name=patient.name,
                    stat="strain",
                    taken=1,
                    cause=f"first aid from {doctor.name}",
                ),
            )
            strain = 1
        else:
            record(
                db,
                campaign_id,
                EffectNoted(
                    name=patient.name,
                    text=(
                        f"{patient.name} has no System Strain to spare for "
                        f"{doctor.name}'s first aid."
                    ),
                ),
            )
        return {
            "healer": doctor.name,
            "target": patient.name,
            "roll": roll,
            "bonus": bonus,
            "total": total,
            "healed": healed,
            "strain": strain,
        }

    return ctx.world.with_hook_context(action)


def restore_spell_slots(character: str, ctx: ToolContext) -> dict[str, Any]:
    """Clear the castings spent today, as a night's rest does."""

    def action(hc: HookContext) -> dict[str, Any]:
        _, found = _character(hc, character)
        spent = found.harm.get("castings", 0)
        if spent <= 0:
            return {
                "character": found.name,
                "restored": 0,
                "castings": found.stats.get("castings", 0),
            }
        db, campaign_id = hc.session()
        record(
            db,
            campaign_id,
            StatRestored(
                name=found.name,
                stat="castings",
                amount=spent,
                cause="a night's rest",
            ),
        )
        return {
            "character": found.name,
            "restored": spent,
            "castings": found.stats.get("castings", 0),
        }

    return ctx.world.with_hook_context(action)


def _character(hc: HookContext, name: str) -> tuple[Any, Any]:
    state = hc.state()
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    return state, character


def _spell(spec: CharacterSpec, spell: str) -> Any:
    feature = spec.features.get(spell)
    if feature is None:
        raise InvalidInput(f"there is no feature named {spell!r}")
    if feature.feature_type != "spell":
        raise InvalidInput(f"{feature.name} is not a spell")
    return feature


def _duration(duration: str) -> str:
    value = duration.strip().casefold()
    if value not in EFFORT_DURATIONS:
        options = ", ".join(EFFORT_DURATIONS)
        raise InvalidInput(f"duration must be one of: {options}")
    return value


# Keep the level cap importable from this module too, so a tool description can name it.
__all__ = [
    "PREPARED_MARK",
    "add_strain",
    "cast_spell",
    "commit_effort",
    "first_aid",
    "heal_strain",
    "prepare_spells",
    "recover_effort",
    "restore_spell_slots",
]
