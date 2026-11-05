"""Python side of the Worlds Without Number rule set: checks, morale, advancement, and gear.

Numeric class, XP, casting, art, tradition, and skill-prerequisite charts are loaded from the
ruleset's validated ``progression`` data. Remaining rule-specific behavior is kept here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from cygnusforge.content.character import (
    CharacterError,
    CharacterSpec,
    Feature,
    check_ordered_feature_rank,
    derived_stats,
    feature_pick_options,
)
from cygnusforge.rules.context import ToolContext
from cygnusforge.rules.dice import roll_total
from cygnusforge.rules.wwn_progression import (
    CastingRow,
    ClassTable,
    WWNProgression,
    require_wwn_progression,
)
from cygnusforge.services import reactions
from cygnusforge.services.common import InvalidInput, whole_number
from cygnusforge.services.events import (
    PC_KIND,
    CheckMade,
    EffectNoted,
    FeatureLearned,
    MaxChanged,
    StatRestored,
    name_key,
    record,
)
from cygnusforge.services.hooks import HookContext

MAX_ATTRIBUTE = 18


# The character mark that records committed Effort, keyed by duration and then tradition:
# {"scene": {"high": 1}, "day": {"necromancer": 2}}.
EFFORT_MARK = "effort_committed"
EFFORT_DURATIONS = ("scene", "day", "indefinite")


def class_table(class_id: str, progression: WWNProgression) -> ClassTable:
    """The advancement table for a class id. Raises InvalidInput for an unknown class."""
    found = progression.class_tables.get(class_id.strip().casefold())
    if found is None:
        options = ", ".join(sorted(progression.class_tables))
        raise InvalidInput(f"there is no class table for {class_id!r}; the classes are: {options}")
    return found


def _chart_level(level: Any, progression: WWNProgression, character: str, chart: str) -> int:
    if type(level) is not int or not 1 <= level <= progression.max_level:
        raise InvalidInput(
            f"{character} is level {level!r}; the {chart} chart supports levels "
            f"1 through {progression.max_level}"
        )
    return level


def casting_row(class_id: str, level: int, progression: WWNProgression) -> CastingRow | None:
    """Return a class's casting row, or None if its class has no casting chart."""
    class_id = class_id.strip().casefold()
    table_id = progression.casting_class_tables.get(class_id)
    if table_id is None:
        return None
    if type(level) is not int or not 1 <= level <= progression.max_level:
        raise InvalidInput(
            f"{class_id}'s {table_id} casting chart supports levels 1 through "
            f"{progression.max_level}; got level {level}"
        )
    row = progression.casting_tables[table_id].get(level)
    if row is None:
        raise InvalidInput(f"{class_id}'s {table_id} casting chart has no row for level {level}")
    return row


def tradition_data(tradition: str, progression: WWNProgression) -> tuple[str, str]:
    """Return a tradition's normalized spell tag and configured Effort stat."""
    key = tradition.strip().casefold()
    tag = progression.tradition_tags.get(key, key)
    return tag, progression.tradition_effort_stats.get(tag, "")


def tradition_tag(tradition: str, progression: WWNProgression) -> str:
    """The spell tag a tradition template grants, such as `high` for `high_mage`."""
    return tradition_data(tradition, progression)[0]


def tradition_ids(found: Any) -> list[str]:
    """Every art tradition a character has. A Mage/Mage Adventurer has two, and a Healer or
    Vowed has one from its class rather than a tradition template."""
    ids = [
        str(found.templates[kind])
        for kind in ("tradition", "tradition2")
        if found.templates.get(kind)
    ]
    class_id = str(found.templates.get("class", "")).strip().casefold()
    if class_id in {"healer", "vowed"}:
        ids.append(class_id)
    return ids


def tradition_tags(found: Any, progression: WWNProgression) -> list[str]:
    return [tradition_tag(tradition, progression) for tradition in tradition_ids(found)]


def tradition_effort_stat(tradition: str, progression: WWNProgression) -> str:
    """The Effort stat a tradition uses, or "" when the tradition is unknown."""
    return tradition_data(tradition, progression)[1]


def check_morale(name: str, cause: str, ctx: ToolContext) -> dict[str, Any]:
    """Roll 2d6 against an NPC's Morale score, which comes from its statblock (an NPC with no
    statblock uses the rule set's default). A roll no higher than the score holds; a higher roll
    breaks. Records the check, or notes that the character has no Morale score to check. The
    source gives Morale only to NPCs and monsters: player characters never make this check and
    carry no Morale score of their own."""

    def action(hc: HookContext) -> dict[str, Any]:
        _, character = _character(hc, name)
        if character.kind == PC_KIND:
            raise InvalidInput("player characters never make Morale checks")
        morale = character.stats.get("morale")
        db, campaign_id = hc.session()
        if morale is None:
            record(
                db,
                campaign_id,
                EffectNoted(
                    name=character.name,
                    text=(
                        f"{character.name} has no Morale score, so no Morale check is rolled "
                        f"({cause})."
                    ),
                ),
            )
            return {
                "character": character.name,
                "check": "Morale",
                "morale": None,
                "roll": None,
                "holds": None,
                "cause": cause,
            }
        roll = roll_total("2d6", ctx.world.rng)
        holds = roll <= morale
        record(
            db,
            campaign_id,
            CheckMade(
                name=character.name,
                check="Morale",
                stat="morale",
                stat_name="Morale",
                roll=roll,
                bonus=0,
                total=roll,
                difficulty=morale,
                success=holds,
                cause=cause,
            ),
        )
        return {
            "character": character.name,
            "check": "Morale",
            "morale": morale,
            "roll": roll,
            "holds": holds,
            "cause": cause,
        }

    return ctx.world.with_hook_context(action)


# The saving throw names the GM says, and the stats they read.
SAVE_STATS: Mapping[str, str] = {
    "physical": "save_physical",
    "evasion": "save_evasion",
    "mental": "save_mental",
    "luck": "save_luck",
}


def _save_stat(save: str) -> str:
    """The stat for a named save, accepting the short or full name."""
    key = save.strip().casefold().replace("save_", "")
    stat = SAVE_STATS.get(key, save if save in SAVE_STATS.values() else "")
    if not stat:
        options = ", ".join(SAVE_STATS)
        raise InvalidInput(f"there is no save named {save!r}; the saves are: {options}")
    return stat


def player_roll_save(
    character: str, save: str, cause: str, ctx: ToolContext, roll: int = 0
) -> dict[str, Any]:
    """Ask the player to roll a saving throw, and record their d20. Use it instead of `roll_save`
    whenever the save is for the player's own character; the player presses the roll button."""
    if not 1 <= roll <= 20:
        raise InvalidInput("the player's save roll must be 1 to 20")
    return ctx.world.roll_save(character, _save_stat(save), cause, natural=roll)


def instinct_check(name: str, instinct: int, cause: str, ctx: ToolContext) -> dict[str, Any]:
    """Check an NPC's Instinct: roll 1d10 and compare it to the score in the statblock. A roll no
    higher than the Instinct means the creature acts on impulse. Instinct is a statblock score,
    not a character stat; player characters never check Instinct."""
    value = whole_number(instinct, "instinct", 0, 10)

    def action(hc: HookContext) -> dict[str, Any]:
        _, character = _character(hc, name)
        if character.kind == PC_KIND:
            raise InvalidInput("player characters never check Instinct")
        roll = roll_total("1d10", ctx.world.rng)
        impulsive = roll <= value
        db, campaign_id = hc.session()
        record(
            db,
            campaign_id,
            CheckMade(
                name=character.name,
                check="Instinct",
                stat="instinct",
                stat_name="Instinct",
                roll=roll,
                bonus=0,
                total=roll,
                difficulty=value,
                success=impulsive,
                cause=cause,
            ),
        )
        return {
            "character": character.name,
            "check": "Instinct",
            "instinct": value,
            "roll": roll,
            "impulsive": impulsive,
            "cause": cause,
        }

    return ctx.world.with_hook_context(action)


def gain_growth(character: str, ctx: ToolContext) -> dict[str, Any]:
    """Roll the character's background Growth table for a level's growth. Apply the result with
    improve_attribute, spend_skill_point, or the appropriate counter."""

    def identify(hc: HookContext) -> dict[str, str]:
        _, found = _character(hc, character)
        background = found.templates.get("background", "")
        return {
            "character": found.name,
            "background": background,
            "table": f"growth_{background}",
        }

    result = ctx.world.with_hook_context(identify)
    try:
        growth = ctx.world.draw_character_table(result["table"])
    except InvalidInput as error:
        raise InvalidInput(f"{result['character']} has no background Growth table") from error
    return {**result, "growth": growth}


def roll_reaction(
    character: str, cause: str, ctx: ToolContext, greeting: bool = True
) -> dict[str, Any]:
    """Roll 2d6 for an NPC group's first reaction, adding the greeting character's Charisma
    modifier when a player character is in a position to greet them. The higher the total, the
    friendlier the reaction: 2 or less is as hostile as the situation allows, 3 to 5 is unfriendly,
    6 to 8 is as expected, 9 to 11 is friendlier than expected, and 12 or more is as helpful as
    their nature permits."""
    bonus = 0
    if greeting:
        spec = ctx.world.character

        def read(hc: HookContext) -> dict[str, int]:
            _, found = _character(hc, character)
            return {"bonus": spec.modifier(found.stats, "cha")}

        bonus = int(ctx.world.with_hook_context(read)["bonus"])
    roll = roll_total("2d6", ctx.world.rng)
    total = roll + bonus

    def action(hc: HookContext) -> dict[str, Any]:
        nonlocal total
        _, found = _character(hc, character)
        db, campaign_id = hc.session()
        override = ""
        for triggered in reactions.match_reaction_roll(
            found, ctx.world.character, ctx.world.features, total
        ):
            effect = triggered.reaction.effect
            if effect["op"] == "add_total":
                total += int(effect["amount"])
                reactions.use(db, campaign_id, found, triggered)
            elif effect["op"] == "set_reaction" and not override:
                override = str(effect["to"])
                reactions.use(db, campaign_id, found, triggered)
        reaction = override or _reaction_for_total(total)
        record(
            db,
            campaign_id,
            EffectNoted(
                name=found.name,
                text=f"Reaction roll {total} ({reaction}; {cause}).",
            ),
        )
        return {
            "character": found.name,
            "roll": roll,
            "bonus": bonus,
            "total": total,
            "reaction": reaction,
            "cause": cause,
        }

    return ctx.world.with_hook_context(action)


def _reaction_for_total(total: int) -> str:
    """The book's reaction band for a total."""
    if total <= 2:
        return "as aggressively hostile as the situation allows"
    if total <= 5:
        return "more unfriendly and hostile than expected"
    if total <= 8:
        return "as hostile or friendly as they would usually be"
    if total <= 11:
        return "friendlier and more benign than expected"
    return "as friendly and helpful as their nature and the situation permit"


def player_attack(
    target: str,
    weapon: str,
    ctx: ToolContext,
    modifier: str = "",
    roll: int = 0,
    hit_modifier: int = 0,
    range_band: str = "",
    dual_wield: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Record the player character's attack with a carried weapon, or unarmed, using the d20 the
    player rolled. The engine rolls damage; Shock applies on a miss. `hit_modifier` carries cover,
    range, dual wielding, or another situational penalty or bonus. The character must be taking
    their turn unless `force` says the story needs the attack outside it."""
    if not 1 <= roll <= 20:
        raise InvalidInput("the player's attack roll must be 1 to 20")
    return ctx.world.player_attack(
        target,
        weapon,
        modifier,
        attack_roll=roll,
        hit_modifier=hit_modifier,
        range_band=range_band,
        dual_wield=dual_wield,
        force=force,
    )


def _xp_rate(ctx: ToolContext, rate: str = "") -> str:
    """The campaign's experience track, or the explicit override the GM gave."""
    chosen = rate or str(ctx.world.campaign_options().get("xp_rate", "fast"))
    if chosen not in ("fast", "slow"):
        raise InvalidInput("the experience track must be 'fast' or 'slow'")
    return chosen


def gain_xp(
    character: str,
    amount: int,
    cause: str,
    ctx: ToolContext,
    *,
    rate: str = "",
) -> dict[str, Any]:
    """Add experience to a character's `xp` counter. When the total reaches the next level's
    threshold, note that the GM may call `level_up`; experience never levels a character by
    itself."""
    value = whole_number(amount, "amount", 0, 1_000_000)
    rate = _xp_rate(ctx, rate)
    progression = require_wwn_progression(ctx.world)
    table = progression.xp_slow if rate == "slow" else progression.xp_fast

    def validate_level(hc: HookContext) -> int:
        _, found = _character(hc, character)
        level = found.counters.get("level", 1)
        return _chart_level(level, progression, found.name, "experience")

    ctx.world.with_hook_context(validate_level)
    changed = ctx.world.adjust_counter(character, "xp", value)
    total = int(changed["value"])

    def action(hc: HookContext) -> dict[str, Any]:
        _, found = _character(hc, character)
        level = found.counters.get("level", 1)
        level = _chart_level(level, progression, found.name, "experience")
        threshold = table[level] if level < progression.max_level else None
        ready = threshold is not None and total >= threshold
        if ready:
            db, campaign_id = hc.session()
            record(
                db,
                campaign_id,
                EffectNoted(
                    name=found.name,
                    text=(
                        f"{found.name} has {total} experience, enough to reach level "
                        f"{level + 1}; the GM may call level_up ({cause})."
                    ),
                ),
            )
        return {
            "character": found.name,
            "xp": total,
            "level": level,
            "next_level": level + 1 if threshold is not None else None,
            "next_threshold": threshold,
            "ready": ready,
            "cause": cause,
        }

    return ctx.world.with_hook_context(action)


def apply_level_up(character: str, ctx: ToolContext, rate: str = "") -> dict[str, Any]:
    """Raise a character one level, if their experience reaches the next threshold. Rolls the
    class hit dice for the new level's HP as the book says, sets the class attack bonus, adds
    three skill points (four for an Expert or partial Expert), grants any focus or art pick due,
    raises a spellcaster's castings, and recomputes the derived stats (saves, Strain, Effort)."""
    rate = _xp_rate(ctx, rate)
    spec = ctx.world.character
    progression = require_wwn_progression(ctx.world)
    table_xp = progression.xp_slow if rate == "slow" else progression.xp_fast

    def read(hc: HookContext) -> dict[str, Any]:
        _, found = _character(hc, character)
        level = found.counters.get("level", 1)
        level = _chart_level(level, progression, found.name, "experience")
        xp = found.counters.get("xp", 0)
        if level >= progression.max_level:
            raise InvalidInput(
                f"{found.name} is already at the highest level, {progression.max_level}"
            )
        threshold = table_xp[level]
        if xp < threshold:
            raise InvalidInput(
                f"{found.name} has {xp} experience; level {level + 1} needs {threshold} on the "
                f"{rate} track"
            )
        class_id = found.templates.get("class", "")
        if not class_id:
            raise InvalidInput(f"{found.name} has no class, so has no level to gain")
        return {
            "level": level,
            "class_id": class_id,
            "traditions": tradition_ids(found),
            "hp": found.stats.get("hp", 0),
            "con": spec.modifier(found.stats, "con"),
            "stats": dict(found.stats),
            "skills": dict(found.skills),
            "features": set(found.features),
        }

    found = ctx.world.with_hook_context(read)
    level = int(found["level"])
    new_level = level + 1
    table = class_table(str(found["class_id"]), progression)
    ctx.world.adjust_counter(character, "level", 1)
    # The book rerolls the class hit dice for the new level, adding Constitution to each die,
    # and keeps the old total plus one when the reroll is no better.
    rolled = roll_total(f"{new_level}d{table.hit_die}", ctx.world.rng)
    new_hp = rolled + new_level * (table.hp_constant + int(found["con"]))
    gained = max(1, new_hp)
    if gained <= int(found["hp"]):
        gained = int(found["hp"]) + 1
    if "focus_die_hard" in found["features"]:
        gained += 2
    ctx.world.set_stat_max(character, "hp", gained, cause=f"level {new_level}")
    ctx.world.set_stat_max(
        character, "attack_bonus", table.attack[new_level - 1], cause=f"level {new_level}"
    )
    points = 4 if table.quick_learner else 3
    ctx.world.adjust_counter(character, "skill_points", points)
    focus_picks = 1 if new_level in table.focus_levels else 0
    if focus_picks:
        ctx.world.adjust_counter(character, "focus_picks", focus_picks)
    art_picks = sum(
        _art_picks(str(found["class_id"]), tradition, new_level, progression)
        for tradition in found["traditions"]
    )
    if art_picks:
        ctx.world.adjust_counter(character, "art_picks", art_picks)
    casting = casting_row(str(found["class_id"]), new_level, progression)
    if casting is not None:
        ctx.world.set_stat_max(character, "castings", casting.casts, cause=f"level {new_level}")
    spell_picks = _spell_picks(str(found["class_id"]), casting, progression)
    if spell_picks:
        ctx.world.adjust_counter(character, "spell_picks", spell_picks)
    _recompute_derived(ctx, character, spec, cause=f"level {new_level}")
    return {
        "character": character,
        "level": new_level,
        "hp": gained,
        "attack_bonus": table.attack[new_level - 1],
        "skill_points": points,
        "focus_picks": focus_picks,
        "art_picks": art_picks,
        "spell_picks": spell_picks,
        "castings": casting.casts if casting is not None else None,
        "rate": rate,
    }


def _spell_picks(class_id: str, casting: CastingRow | None, progression: WWNProgression) -> int:
    """The new spells a level grants: two for a full High Mage, one for every other spellcaster."""
    if casting is None:
        return 0
    table_id = progression.casting_class_tables.get(class_id.strip().casefold())
    return 2 if table_id == "full_mage" else 1


def _art_picks(class_id: str, tradition: str, level: int, progression: WWNProgression) -> int:
    """The art picks a class chart grants at a level: a single arcane tradition uses the full
    chart, while an Adventurer's partial mage uses the partial chart. A dual mage counts as a
    partial mage for both traditions."""
    tag = tradition_tag(tradition, progression)
    if not tag:
        return 0
    levels = (
        progression.art_levels_full
        if class_id.strip().casefold() == "mage"
        else progression.art_levels_partial
    )
    return 1 if level in levels.get(tag, frozenset()) else 0


def level_up_summary(character: str, ctx: ToolContext) -> dict[str, Any]:
    """What the next level grants before it is taken, so the level-up page can show the automatic
    gains and the size of each pick pool the player will spend."""
    progression = require_wwn_progression(ctx.world)
    rate = _xp_rate(ctx)
    table_xp = progression.xp_slow if rate == "slow" else progression.xp_fast

    def read(hc: HookContext) -> dict[str, Any]:
        _, found = _character(hc, character)
        level = _chart_level(found.counters.get("level", 1), progression, found.name, "experience")
        if level >= progression.max_level:
            raise InvalidInput(
                f"{found.name} is already at the highest level, {progression.max_level}"
            )
        xp = found.counters.get("xp", 0)
        threshold = table_xp[level]
        if xp < threshold:
            raise InvalidInput(
                f"{found.name} has {xp} experience; level {level + 1} needs {threshold} on the "
                f"{rate} track"
            )
        class_id = found.templates.get("class", "")
        if not class_id:
            raise InvalidInput(f"{found.name} has no class, so has no level to gain")
        return {
            "level": level,
            "xp": xp,
            "threshold": threshold,
            "class_id": class_id,
            "traditions": tradition_ids(found),
            "rate": rate,
        }

    found = ctx.world.with_hook_context(read)
    level = int(found["level"])
    new_level = level + 1
    class_id = str(found["class_id"])
    table = class_table(class_id, progression)
    points = 4 if table.quick_learner else 3
    focus_picks = 1 if new_level in table.focus_levels else 0
    art_picks = sum(
        _art_picks(class_id, tradition, new_level, progression) for tradition in found["traditions"]
    )
    casting = casting_row(class_id, new_level, progression)
    spell_picks = _spell_picks(class_id, casting, progression)
    attack = table.attack[new_level - 1]
    lines = [
        f"Hit points: rerolled for level {new_level} (the old total plus one if the roll is worse).",
        f"Attack bonus: {attack}.",
        f"Skill points: {points}.",
    ]
    if focus_picks:
        lines.append(f"Focus pick: {focus_picks}.")
    if art_picks:
        lines.append(f"Art pick: {art_picks}.")
    if spell_picks:
        lines.append(f"Spell pick: {spell_picks}.")
    if casting is not None:
        lines.append(f"Castings: {casting.casts}.")
    lines.append("Saving throws improve by one.")
    return {
        "character": character,
        "level": level,
        "new_level": new_level,
        "xp": found["xp"],
        "threshold": found["threshold"],
        "rate": rate,
        "attack_bonus": attack,
        "castings": casting.casts if casting is not None else None,
        "lines": lines,
        "grants": {
            "skill_points": points,
            "focus_picks": focus_picks,
            "art_picks": art_picks,
            "spell_picks": spell_picks,
        },
    }


def level_up(character: str, ctx: ToolContext, rate: str = "") -> dict[str, Any]:
    """The GM's level-up tool. An NPC advances at once; a player character is left for the
    player to advance on the character sheet, because its picks are the player's choices."""

    def read(hc: HookContext) -> tuple[str, str]:
        _, found = _character(hc, character)
        return found.kind, found.name

    kind, name = ctx.world.with_hook_context(read)
    if kind == PC_KIND:
        return {
            "character": name,
            "pending": True,
            "message": (
                f"{name} is a player character. The player chooses the level-up on the "
                "character sheet; no level was applied by this tool."
            ),
        }
    return apply_level_up(name, ctx, rate=rate)


def focus_options(
    character: str, pick: Any, ctx: ToolContext, level: int | None = None
) -> list[dict[str, Any]]:
    """The Foci a focus pick offers: the class's general focus list, with a rank-2 Focus offered
    once its rank-1 Focus is known."""
    spec = ctx.world.character

    def read(hc: HookContext) -> dict[str, Any]:
        _, found = _character(hc, character)
        class_id = found.templates.get("class", "")
        template = spec.templates.get(class_id, None)
        if template is None:
            raise InvalidInput(f"{found.name} has no class")
        choice = next(
            (candidate for candidate in template.feat_picks if candidate.id == "class_focus_any"),
            None,
        )
        if choice is None:
            return {"options": [], "features": dict(found.features)}
        return {
            "options": [feature.id for feature in feature_pick_options(spec, choice)],
            "features": dict(found.features),
        }

    found = ctx.world.with_hook_context(read)
    return _feature_options(spec, found["options"], found["features"], set(pick.exclude))


def art_options(
    character: str, pick: Any, ctx: ToolContext, level: int | None = None
) -> list[dict[str, Any]]:
    """The arts of the character's own traditions that they do not already know."""
    spec = ctx.world.character
    progression = require_wwn_progression(ctx.world)

    def read(hc: HookContext) -> dict[str, Any]:
        _, found = _character(hc, character)
        return {
            "tags": set(tradition_tags(found, progression)),
            "features": dict(found.features),
        }

    found = ctx.world.with_hook_context(read)
    options = [
        feature.id
        for feature in spec.features.values()
        if feature.feature_type == "feat"
        and feature.tag in found["tags"]
        and feature.tag != "focus"
    ]
    return _feature_options(spec, options, found["features"], set(pick.exclude))


def spell_options(
    character: str, pick: Any, ctx: ToolContext, new_level: int | None = None
) -> list[dict[str, Any]]:
    """The spells a caster may learn now: their traditions and High Magic, up to the highest
    spell level their class and new level allow."""
    spec = ctx.world.character
    progression = require_wwn_progression(ctx.world)

    def read(hc: HookContext) -> dict[str, Any]:
        _, found = _character(hc, character)
        class_id = found.templates.get("class", "")
        level = new_level or found.counters.get("level", 1)
        row = casting_row(class_id, level, progression)
        tags = set(tradition_tags(found, progression)) | {"high"}
        return {
            "tags": tags,
            "max_rank": row.max_spell_level if row is not None else 0,
            "features": dict(found.features),
        }

    found = ctx.world.with_hook_context(read)
    options = [
        feature.id
        for feature in spec.features.values()
        if feature.feature_type == "spell"
        and feature.tag in found["tags"]
        and feature.rank <= found["max_rank"]
    ]
    return _feature_options(spec, options, found["features"], set(pick.exclude))


def _feature_options(
    spec: CharacterSpec,
    option_ids: Iterable[str],
    held: Mapping[str, int],
    exclude: set[str],
) -> list[dict[str, Any]]:
    """The unheld features among `option_ids`, plus the next rank of a held Focus, as plain
    dictionaries the page can show."""
    options: list[dict[str, Any]] = []
    seen: set[str] = set()
    for option_id in option_ids:
        feature = spec.features.get(option_id)
        if feature is None or option_id in exclude or option_id in seen:
            continue
        candidates = [feature]
        if option_id in held:
            candidates.extend(_next_ranks(spec, feature))
        for candidate in candidates:
            if candidate.id in seen or candidate.id in exclude:
                continue
            if candidate.id in held and not candidate.repeatable:
                continue
            seen.add(candidate.id)
            options.append(
                {
                    "id": candidate.id,
                    "name": candidate.name,
                    "description": candidate.description,
                    "rank": candidate.rank,
                    "repeatable": candidate.repeatable,
                }
            )
    return sorted(options, key=lambda option: (option["name"], option["id"]))


def _next_ranks(spec: CharacterSpec, feature: Feature) -> list[Feature]:
    return [
        candidate
        for candidate in spec.features.values()
        if candidate.name == feature.name
        and candidate.tag == feature.tag
        and candidate.feature_type == feature.feature_type
        and candidate.rank == feature.rank + 1
    ]


def apply_skill_points(
    character: str, pick: Any, changes: Mapping[str, int], ctx: ToolContext
) -> dict[str, Any]:
    """Raise each named skill to its submitted level, one level at a time, through the normal
    skill-point tool."""
    applied = []
    for skill, target in changes.items():
        current = _pc_skill_level(character, skill, ctx)
        while current < target:
            applied.append(spend_skill_point(character, skill, ctx))
            current += 1
    return {"character": character, "skills": applied}


def _pc_skill_level(character: str, skill: str, ctx: ToolContext) -> int:
    spec = ctx.world.character

    def read(hc: HookContext) -> int:
        _, found = _character(hc, character)
        return spec.skill_level(found.skills, skill)

    return int(ctx.world.with_hook_context(read))


def apply_attribute_boosts(
    character: str, pick: Any, boosts: Mapping[str, int], ctx: ToolContext
) -> dict[str, Any]:
    """Buy each submitted number of permanent attribute boosts through the normal tool."""
    applied = []
    for stat, count in boosts.items():
        for _ in range(count):
            applied.append(improve_attribute(character, stat, ctx))
    return {"character": character, "attributes": applied}


def apply_focus_pick(character: str, pick: Any, choice: str, ctx: ToolContext) -> dict[str, Any]:
    """Spend a focus pick on the chosen Focus."""
    return learn_focus(character, choice, ctx)


def apply_art_pick(character: str, pick: Any, choice: str, ctx: ToolContext) -> dict[str, Any]:
    """Spend an art pick on the chosen art."""
    return learn_art(character, choice, ctx)


def apply_spell_pick(character: str, pick: Any, choice: str, ctx: ToolContext) -> dict[str, Any]:
    """Spend a spell pick on the chosen spell."""
    result = learn_spell(character, choice, ctx)
    remaining = ctx.world.adjust_counter(character, "spell_picks", -1)
    return {**result, "spell_picks": remaining["value"]}


def spend_skill_point(character: str, skill: str, ctx: ToolContext) -> dict[str, Any]:
    """Spend skill points to raise one skill by a level. The cost rises with the level, as the
    book's table says (1, 2, 3, 4, 5 points for levels 0 to 4), and the progression chart sets
    the character-level prerequisite for each skill level."""
    spec = ctx.world.character
    progression = require_wwn_progression(ctx.world)
    if skill not in spec.skills:
        options = ", ".join(sorted(spec.skills)) or "none"
        raise InvalidInput(f"there is no skill named {skill!r}; the skills are: {options}")

    def read(hc: HookContext) -> dict[str, int]:
        _, found = _character(hc, character)
        points = found.counters.get("skill_points", 0)
        skill_progression = spec.skill_progression
        if skill_progression is None:
            raise InvalidInput("the rule set has no skill progression")
        before = spec.skill_level(found.skills, skill)
        if spec.skill_trained(found.skills, skill) and before >= skill_progression.maximum:
            raise InvalidInput(
                f"{found.name}'s {spec.skills[skill].name} is already at level "
                f"{skill_progression.maximum}"
            )
        new_level = (
            before + 1 if spec.skill_trained(found.skills, skill) else skill_progression.first_level
        )
        cost = SKILL_LEVEL_COSTS.get(new_level)
        needs_level = progression.skill_level_minimums.get(new_level)
        if (
            new_level > skill_progression.maximum
            or new_level > progression.max_skill_level
            or cost is None
            or needs_level is None
        ):
            raise InvalidInput(
                f"the Worlds Without Number skill advancement chart does not support "
                f"level {new_level}"
            )
        level = _chart_level(found.counters.get("level", 1), progression, found.name, "experience")
        if level < needs_level:
            raise InvalidInput(
                f"{spec.skills[skill].name} level {new_level} needs character level "
                f"{needs_level}; {found.name} is level {level}"
            )
        if points < cost:
            unit = "point" if cost == 1 else "points"
            raise InvalidInput(
                f"{spec.skills[skill].name} level {new_level} costs {cost} skill {unit}; "
                f"{found.name} has {points}"
            )
        return {"points": points, "before": before, "new_level": new_level, "cost": cost}

    found = ctx.world.with_hook_context(read)
    new_level = int(found["new_level"])
    ctx.world.raise_skill(character, skill, new_level, cause="skill points")
    remaining = ctx.world.adjust_counter(character, "skill_points", -int(found["cost"]))
    _recompute_derived(ctx, character, spec, cause=f"{spec.skills[skill].name} {new_level}")
    return {
        "character": character,
        "skill": skill,
        "skill_name": spec.skills[skill].name,
        "level": new_level,
        "cost": found["cost"],
        "remaining": remaining["value"],
    }


# Skill-point costs by target skill level.
SKILL_LEVEL_COSTS: Mapping[int, int] = {0: 1, 1: 2, 2: 3, 3: 4, 4: 5}
# The minimum character level for each attribute boost, in the order they are bought.
ATTRIBUTE_BOOST_MINIMUMS: tuple[int, ...] = (2, 2, 3, 6, 9)


def improve_attribute(character: str, stat: str, ctx: ToolContext) -> dict[str, Any]:
    """Spend skill points on a permanent +1 to one of the six attributes. The first boost costs
    one point, the second two, and so on to five, and each has its own minimum character level.
    A character can buy five boosts in total."""
    spec = ctx.world.character
    if stat not in spec.stats or spec.stats[stat].derived is not None:
        options = ", ".join(s for s in spec.stats if spec.stats[s].derived is None)
        raise InvalidInput(f"there is no attribute named {stat!r}; the attributes are: {options}")

    def read(hc: HookContext) -> dict[str, int]:
        _, found = _character(hc, character)
        boosts = found.counters.get("attribute_boosts", 0)
        if boosts >= len(ATTRIBUTE_BOOST_MINIMUMS):
            raise InvalidInput(f"{found.name} has already bought all five attribute boosts")
        cost = boosts + 1
        needs_level = ATTRIBUTE_BOOST_MINIMUMS[boosts]
        level = found.counters.get("level", 1)
        if level < needs_level:
            raise InvalidInput(
                f"the next attribute boost needs character level {needs_level}; "
                f"{found.name} is level {level}"
            )
        points = found.counters.get("skill_points", 0)
        if points < cost:
            raise InvalidInput(
                f"the next attribute boost costs {cost} skill points; {found.name} has {points}"
            )
        value = found.stats.get(stat, 0)
        if value >= MAX_ATTRIBUTE:
            raise InvalidInput(
                f"{found.name}'s {spec.stats[stat].name} is already at {MAX_ATTRIBUTE}"
            )
        return {"boosts": boosts, "cost": cost, "value": value}

    found = ctx.world.with_hook_context(read)
    value = int(found["value"]) + 1
    ctx.world.set_stat_max(character, stat, value, cause="an attribute boost")
    ctx.world.adjust_counter(character, "attribute_boosts", 1)
    remaining = ctx.world.adjust_counter(character, "skill_points", -int(found["cost"]))
    _recompute_derived(ctx, character, spec, cause=f"{spec.stats[stat].name} boost")
    return {
        "character": character,
        "stat": stat,
        "value": value,
        "boosts": int(found["boosts"]) + 1,
        "cost": found["cost"],
        "remaining": remaining["value"],
    }


def learn_focus(character: str, focus: str, ctx: ToolContext) -> dict[str, Any]:
    """Spend a focus pick on a focus. A second rank needs the first rank already held. The focus's
    creation effects are applied through the world's tools."""
    spec = ctx.world.character
    feature = spec.features.get(focus)
    if feature is None:
        raise InvalidInput(f"there is no feature named {focus!r}")
    if feature.tag not in ("focus", "origin"):
        raise InvalidInput(f"{feature.name} is not a focus")
    required = _prior_focus(feature)
    if required is not None and required not in spec.features:
        required = None

    def read(hc: HookContext) -> dict[str, Any]:
        _, found = _character(hc, character)
        if found.counters.get("focus_picks", 0) < 1:
            raise InvalidInput(f"{found.name} has no focus picks to spend")
        _check_focus_class(spec, found.templates.get("class"), feature)
        if focus in found.features and not feature.repeatable:
            raise InvalidInput(f"{found.name} already has {feature.name}")
        if feature.defense_floor is not None and not found.defense_tracked:
            raise InvalidInput(f"{found.name}'s equipped defense has not been recorded")
        if required is not None and required not in found.features:
            raise InvalidInput(
                f"{feature.name} is the second rank; {found.name} needs "
                f"{spec.features[required].name} first"
            )
        held = {**found.features, focus: found.features.get(focus, 0) + 1}
        try:
            check_ordered_feature_rank(spec, feature, held)
        except CharacterError as exc:
            raise InvalidInput(str(exc)) from exc
        return {"stats": dict(found.stats), "skills": dict(found.skills)}

    held = ctx.world.with_hook_context(read)
    stats = dict(held["stats"])
    skills = dict(held["skills"])
    notes: list[str] = []
    for effect in feature.effects:
        op = effect.get("op")
        delta = _effect_delta(effect, feature.name)
        if op == "stat":
            stat = str(effect["id"])
            if stat not in spec.stats:
                raise InvalidInput(f"{feature.name} names no stat {stat!r}")
            value = min(MAX_ATTRIBUTE, stats.get(stat, 0) + delta)
            ctx.world.set_stat_max(character, stat, value, cause=f"the focus {feature.name}")
            stats[stat] = value
        elif op == "skill":
            skill = str(effect["id"])
            if skill not in spec.skills:
                raise InvalidInput(f"{feature.name} names no skill {skill!r}")
            progression = spec.skill_progression
            if progression is None:
                raise InvalidInput("the rule set has no skill progression")
            before = spec.skill_level(skills, skill)
            if spec.skill_trained(skills, skill):
                value = min(progression.maximum, before + delta)
            elif delta > 0:
                value = min(progression.maximum, progression.first_level + delta - 1)
            else:
                value = before + delta
            if value > before:
                ctx.world.raise_skill(character, skill, value, cause=f"the focus {feature.name}")
                skills[skill] = value
        elif op == "counter":
            counter = str(effect["id"])
            ctx.world.adjust_counter(character, counter, delta)
        elif op == "armor":
            current = _armor_total(ctx, character, spec)
            ctx.world.set_armor(character, max(0, current + delta))
        elif op == "note":
            notes.append(str(effect.get("text", "")))
        else:
            raise InvalidInput(f"{feature.name} has an unknown effect {op!r}")
    _learn_feature(ctx, character, feature, notes, cause="a focus pick")
    remaining = ctx.world.adjust_counter(character, "focus_picks", -1)
    _recompute_derived(ctx, character, spec, cause=f"the focus {feature.name}")
    return {
        "character": character,
        "focus": focus,
        "name": feature.name,
        "rank": feature.rank,
        "focus_picks": remaining["value"],
    }


def _check_focus_class(spec: CharacterSpec, class_id: str | None, feature: Feature) -> None:
    if not class_id or feature.tag != "focus" or feature.rank <= 0:
        return
    template = spec.templates.get(class_id)
    if template is None:
        raise InvalidInput(f"there is no class template named {class_id!r}")
    general_pick = next(
        (pick for pick in template.feat_picks if pick.id == "class_focus_any"), None
    )
    if general_pick is None:
        raise InvalidInput(f"{template.name} has no configured general Focus options")
    options = feature_pick_options(spec, general_pick)
    if feature.rank == 1:
        allowed = any(option.id == feature.id for option in options)
    else:
        allowed = any(
            option.rank == 1
            and option.name == feature.name
            and option.tag == feature.tag
            and option.feature_type == feature.feature_type
            for option in options
        )
    if not allowed:
        raise InvalidInput(f"{feature.name} is not available to {template.name}")


def learn_art(character: str, art: str, ctx: ToolContext) -> dict[str, Any]:
    """Spend an art pick on an art of the character's own configured tradition."""
    spec = ctx.world.character
    progression = require_wwn_progression(ctx.world)
    feature = spec.features.get(art)
    if feature is None:
        raise InvalidInput(f"there is no feature named {art!r}")
    if feature.feature_type != "feat" or feature.tag not in progression.art_levels_partial:
        raise InvalidInput(f"{feature.name} is not an art")

    def read(hc: HookContext) -> dict[str, Any]:
        _, found = _character(hc, character)
        tags = tradition_tags(found, progression)
        if not tags:
            raise InvalidInput(f"{found.name} has no magical tradition, so cannot learn arts")
        if feature.tag not in tags:
            raise InvalidInput(
                f"{feature.name} is an art of the {feature.tag} tradition, not "
                f"{found.name}'s {', '.join(tags)}"
            )
        if found.counters.get("art_picks", 0) < 1:
            raise InvalidInput(f"{found.name} has no art picks to spend")
        if art in found.features:
            raise InvalidInput(f"{found.name} already has {feature.name}")
        return {}

    ctx.world.with_hook_context(read)
    _learn_feature(ctx, character, feature, (), cause="an art pick")
    remaining = ctx.world.adjust_counter(character, "art_picks", -1)
    return {
        "character": character,
        "art": art,
        "name": feature.name,
        "tradition": feature.tag,
        "art_picks": remaining["value"],
    }


def learn_spell(character: str, spell: str, ctx: ToolContext) -> dict[str, Any]:
    """Teach a spell to a spellcaster. A High Mage learns High Magic; a Necromancer or
    Elementalist may learn High Magic or a spell of their own New Magic. The spell's level must
    be one the character can cast."""
    spec = ctx.world.character
    progression = require_wwn_progression(ctx.world)
    feature = spec.features.get(spell)
    if feature is None:
        raise InvalidInput(f"there is no feature named {spell!r}")
    if feature.feature_type != "spell":
        raise InvalidInput(f"{feature.name} is not a spell")

    def read(hc: HookContext) -> dict[str, Any]:
        _, found = _character(hc, character)
        tags = tradition_tags(found, progression)
        if not tags:
            raise InvalidInput(f"{found.name} has no magical tradition, so cannot learn spells")
        if feature.tag not in {*tags, "high"}:
            raise InvalidInput(
                f"{feature.name} is not a High Magic spell or a spell of {found.name}'s "
                f"traditions, {', '.join(tags)}"
            )
        class_id = found.templates.get("class", "")
        row = casting_row(class_id, found.counters.get("level", 1), progression)
        if row is None:
            raise InvalidInput(f"{found.name}'s class cannot cast spells")
        if feature.rank > row.max_spell_level:
            raise InvalidInput(
                f"{feature.name} is level {feature.rank}; {found.name} can cast spells of level "
                f"{row.max_spell_level} at most"
            )
        if spell in found.features:
            raise InvalidInput(f"{found.name} already knows {feature.name}")
        return {}

    ctx.world.with_hook_context(read)
    _learn_feature(ctx, character, feature, (), cause="study")
    return {
        "character": character,
        "spell": spell,
        "name": feature.name,
        "rank": feature.rank,
        "tradition": feature.tag,
    }


def roll_starting_silver(character: str, ctx: ToolContext) -> dict[str, Any]:
    """Give a character the book's alternative to a starting package: 3d6 x 10 silver pieces."""
    amount = roll_total("3d6", ctx.world.rng) * 10
    ctx.world.change_currency(character, amount, cause="starting silver")
    return {"character": character, "silver": amount}


def grant_package(character: str, package: str, ctx: ToolContext) -> dict[str, Any]:
    """Give a character the gear of one of the book's equipment packages. Creation already grants
    the kit the player chose, so this is for a character who joined without one, such as a
    promoted hireling. The packages are the rule set's `kit` templates."""
    kits = [t for t in ctx.world.character.templates.values() if t.kind == "kit"]
    found = next(
        (
            kit
            for kit in kits
            if package.strip().casefold() in (kit.id.casefold(), kit.name.casefold())
        ),
        None,
    )
    if found is None:
        options = ", ".join(sorted(kit.id for kit in kits)) or "none"
        raise InvalidInput(f"there is no package {package!r}; the packages are: {options}")
    given: list[str] = []
    for item in found.grant.items:
        ctx.world.give_item(character, item)
        given.append(item)
    if found.grant.silver:
        ctx.world.change_currency(character, found.grant.silver, cause=f"the {found.name} package")
    for item in found.grant.equip:
        ctx.world.equip_armor(character, item)
    return {
        "character": character,
        "package": found.id,
        "name": found.name,
        "items": given,
        "silver": found.grant.silver,
    }


def rest(
    character: str,
    ctx: ToolContext,
    length: str = "night",
    reason: str = "",
    interrupted: bool = False,
) -> dict[str, Any]:
    """Take a rest through the world, then apply the rule set's recovery: a night restores hit
    points equal to the character's level, one point of System Strain, all spell castings, and
    scene and day Effort. A shorter rest restores scene Effort only."""
    progression = require_wwn_progression(ctx.world)
    result = ctx.world.rest(length, interrupted=interrupted, reason=reason)
    return {
        **result,
        **_rest_recovery(character, ctx, length, interrupted, progression),
    }


def recover_after_rest(character: str, ctx: ToolContext) -> dict[str, Any]:
    """Apply a night's recovery without advancing the clock, for a rest the GM tracked elsewhere.
    A night restores hit points equal to the character's level, one System Strain, every spell
    casting, and the Effort committed for a scene or a day."""
    progression = require_wwn_progression(ctx.world)
    return _rest_recovery(character, ctx, "night", False, progression)


def _rest_recovery(
    character: str,
    ctx: ToolContext,
    length: str,
    interrupted: bool,
    progression: WWNProgression,
) -> dict[str, Any]:
    """Record a rest's recovery: the night's hit points, Strain, and castings, and the Effort
    whose duration ends with the rest."""

    def action(hc: HookContext) -> dict[str, Any]:
        _, found = _character(hc, character)
        db, campaign_id = hc.session()
        restored: dict[str, int] = {}

        def recover(stat: str, amount: int, why: str) -> None:
            if amount <= 0 or stat not in found.stats:
                return
            record(
                db,
                campaign_id,
                StatRestored(name=found.name, stat=stat, amount=amount, cause=why),
            )
            restored[stat] = restored.get(stat, 0) + amount

        durations = (
            ("scene", "day") if length in ("night", "extended") and not interrupted else ("scene",)
        )
        marks = found.marks.get(EFFORT_MARK) or {}
        effort_recoveries: list[tuple[str, str, int]] = []
        for duration in durations:
            for tradition, amount in (marks.get(duration) or {}).items():
                stat = tradition_effort_stat(str(tradition), progression)
                if not stat:
                    raise InvalidInput(f"there is no Effort pool for the tradition {tradition!r}")
                if stat not in found.stats:
                    raise InvalidInput(f"{found.name} has no {stat} stat")
                effort_recoveries.append((duration, stat, int(amount)))
        kept = {duration: dict(held) for duration, held in marks.items()}

        if length in ("night", "extended") and not interrupted:
            nights = 7 if length == "extended" else 1
            if length == "extended" or "frail" not in found.conditions:
                recover("hp", found.counters.get("level", 1) * nights, f"a {length}'s rest")
                recover("strain", nights, f"a {length}'s rest")
            recover("castings", found.harm.get("castings", 0), f"a {length}'s rest")
        for duration, stat, amount in effort_recoveries:
            recover(stat, amount, f"{duration} Effort after a {length}'s rest")
        for duration in durations:
            kept.pop(duration, None)
        if kept != marks:
            hc.set_mark(
                found.name,
                EFFORT_MARK,
                kept or None,
                f"{found.name}'s {', '.join(durations)} Effort returns after a {length}'s rest.",
            )
        return {"character": found.name, "restored": restored}

    return ctx.world.with_hook_context(action)


def _character(hc: HookContext, name: str) -> tuple[Any, Any]:
    state = hc.state()
    character = state.characters.get(name_key(name))
    if character is None:
        raise InvalidInput(f"there is no character named {name!r}")
    return state, character


def _recompute_derived(
    ctx: ToolContext, character: str, spec: CharacterSpec, *, cause: str
) -> None:
    """Write the derived stats for the character's new level, counter values, and skills."""

    def action(hc: HookContext) -> None:
        _, found = _character(hc, character)
        try:
            values = derived_stats(
                spec,
                found.stats,
                found.counters,
                found.skills,
                found.templates,
                fields=found.fields,
            )
        except ValueError as error:
            raise InvalidInput(str(error)) from error
        db, campaign_id = hc.session()
        for stat, value in values.items():
            if found.stats.get(stat) == value:
                continue
            record(
                db,
                campaign_id,
                MaxChanged(
                    name=found.name,
                    stat=stat,
                    before=found.stats.get(stat, 0),
                    after=value,
                    cause=cause,
                ),
            )

    ctx.world.with_hook_context(action)


def _learn_feature(
    ctx: ToolContext,
    character: str,
    feature: Feature,
    notes: Iterable[str],
    *,
    cause: str,
) -> None:
    """Add a feature to a character and note anything its effects only describe."""

    def action(hc: HookContext) -> None:
        _, found = _character(hc, character)
        if feature.defense_floor is not None and not found.defense_tracked:
            raise InvalidInput(f"{found.name}'s equipped defense has not been recorded")
        db, campaign_id = hc.session()
        record(
            db,
            campaign_id,
            FeatureLearned(
                name=found.name,
                feature=feature.id,
                feature_name=feature.name,
                cause=cause,
                repeat=feature.repeatable,
            ),
        )
        for note in notes:
            if note:
                record(db, campaign_id, EffectNoted(name=found.name, text=note))

    ctx.world.with_hook_context(action)
    if feature.defense_floor is not None:
        ctx.world.recalculate_defense(character)


def _prior_focus(feature: Any) -> str | None:
    """The feature a higher rank of a focus requires: `focus_x_2` needs `focus_x`."""
    if feature.rank <= 1:
        return None
    suffix = f"_{feature.rank}"
    if feature.id.endswith(suffix):
        return feature.id[: -len(suffix)]
    return None


def _effect_delta(effect: Mapping[str, Any], label: str) -> int:
    delta = effect.get("delta", 0)
    if isinstance(delta, bool) or not isinstance(delta, int):
        raise InvalidInput(f"{label} has an effect delta that is not a whole number")
    return delta


def _armor_total(ctx: ToolContext, character: str, spec: CharacterSpec) -> int:
    """The current total AC, or the unarmored value if it has not been set."""

    def action(hc: HookContext) -> int:
        _, found = _character(hc, character)
        dexterity = spec.modifier(found.stats, "dex")
        return found.armor or 10 + dexterity

    return ctx.world.with_hook_context(action)
