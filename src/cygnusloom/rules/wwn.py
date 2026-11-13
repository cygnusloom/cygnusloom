"""Python side of the Worlds Without Number rule set: checks, morale, advancement, and gear.

Numeric class, XP, casting, art, tradition, and skill-prerequisite charts are loaded from the
ruleset's validated ``progression`` data. Remaining rule-specific behavior is kept here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from cygnusloom.content.character import (
    CharacterError,
    CharacterSpec,
    Feature,
    check_ordered_feature_rank,
    derived_stats,
    feature_pick_options,
)
from cygnusloom.content.library import LevelGrants
from cygnusloom.content.lookup import AmbiguousId, resolve_id
from cygnusloom.rules.context import ToolContext
from cygnusloom.rules.dice import roll_total
from cygnusloom.rules.wwn_progression import (
    CastingRow,
    ClassTable,
    WWNProgression,
    combine_class_tables,
    require_wwn_progression,
)
from cygnusloom.services import inventory as inventory_service
from cygnusloom.services import reactions
from cygnusloom.services.common import InvalidInput, whole_number
from cygnusloom.services.events import (
    PC_KIND,
    CheckMade,
    EffectNoted,
    FeatureLearned,
    Harmed,
    MaxChanged,
    Recovered,
    current,
    decode,
    events_since,
    record,
)
from cygnusloom.services.hooks import HookContext

# The character mark that records committed Effort, keyed by duration and then tradition:
# {"scene": {"high": 1}, "day": {"necromancer": 2}}.
EFFORT_MARK = "effort_committed"
EFFORT_DURATIONS = ("scene", "day", "indefinite")

# A Focus's free bonus skill, when the Focus is taken during advancement, counts as this many
# skill points toward the skill rather than as a single skill pick.
FOCUS_BONUS_SKILL_POINTS = 3


def _resolve_skill(spec: CharacterSpec, skill: str) -> str:
    """The rule set's skill id for a loose id or display name, such as `Sneak`."""
    try:
        return resolve_id(spec.skills, skill, kind="skill", names=lambda rules: (rules.name,))
    except AmbiguousId as exc:
        raise InvalidInput(str(exc)) from exc
    except LookupError:
        options = ", ".join(sorted(spec.skills)) or "none"
        raise InvalidInput(
            f"there is no skill named {skill!r}; the skills are: {options}"
        ) from None


def _resolve_feature(spec: CharacterSpec, feature: str) -> Feature:
    """The rule set's feature for a loose id or display name. Shared names, such as a Focus's
    two ranks, are an error rather than a guess."""
    try:
        ident = resolve_id(
            spec.features, feature, kind="feature", names=lambda found: (found.name,)
        )
    except AmbiguousId as exc:
        raise InvalidInput(str(exc)) from exc
    except LookupError:
        raise InvalidInput(f"there is no feature named {feature!r}") from None
    return spec.features[ident]


def _resolve_attribute(spec: CharacterSpec, stat: str) -> str:
    """The rule set's non-derived attribute id for a loose id or display name."""
    attributes = {ident: rules for ident, rules in spec.stats.items() if rules.derived is None}
    try:
        return resolve_id(attributes, stat, kind="attribute", names=lambda rules: (rules.name,))
    except AmbiguousId as exc:
        raise InvalidInput(str(exc)) from exc
    except LookupError:
        options = ", ".join(attributes) or "none"
        raise InvalidInput(
            f"there is no attribute named {stat!r}; the attributes are: {options}"
        ) from None


def class_table(class_id: str, progression: WWNProgression) -> ClassTable:
    """The advancement table for a class id. Raises InvalidInput for an unknown class."""
    found = progression.class_tables.get(class_id.strip().casefold())
    if found is None:
        options = ", ".join(sorted(progression.class_tables))
        raise InvalidInput(f"there is no class table for {class_id!r}; the classes are: {options}")
    return found


def class_ids(found: Any) -> list[str]:
    """The character's class template ids, the primary class first and a second class after."""
    ids = []
    for kind in ("class", "class2"):
        class_id = str(found.templates.get(kind, "")).strip().casefold()
        if class_id:
            ids.append(class_id)
    return ids


def advancement_table(found: Any, progression: WWNProgression) -> ClassTable:
    """The chart a character advances on: its class table, or two partial tables combined."""
    ids = class_ids(found)
    if not ids:
        raise InvalidInput(f"{found.name} has no class, so has no level to gain")
    tables = [class_table(class_id, progression) for class_id in ids]
    if len(tables) == 1:
        return tables[0]
    return combine_class_tables(tables, progression.class_pairing)


def casting_class_id(found: Any, progression: WWNProgression) -> str | None:
    """The class whose casting chart a character uses: the class first, then the second class."""
    for class_id in class_ids(found):
        if class_id in progression.casting_class_tables:
            return class_id
    return None


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


def tradition_ids(found: Any, progression: WWNProgression) -> list[str]:
    """Every art tradition a character has. A Mage/Mage Adventurer has two, and a Healer or
    Vowed has one from its class, whether that class is the primary or the second partial one."""
    ids = [
        str(found.templates[kind])
        for kind in ("tradition", "tradition2")
        if found.templates.get(kind)
    ]
    for class_id in class_ids(found):
        tradition = class_table(class_id, progression).tradition
        if tradition and tradition not in ids:
            ids.append(tradition)
    return ids


def tradition_tags(found: Any, progression: WWNProgression) -> list[str]:
    return [
        tradition_tag(tradition, progression) for tradition in tradition_ids(found, progression)
    ]


def tradition_effort_stat(tradition: str, progression: WWNProgression) -> str:
    """The Effort stat a tradition uses, or "" when the tradition is unknown."""
    return tradition_data(tradition, progression)[1]


def _score_check(
    hc: HookContext,
    character: Any,
    *,
    label: str,
    key: str,
    score: int | None,
    dice: str,
    outcome: str,
    cause: str,
    rng: Any,
    pc_message: str,
    missing_text: str | None = None,
) -> dict[str, Any]:
    """Roll under a statblock score and record the check.

    Morale and Instinct share this shape: a player character is refused, a roll no higher than
    the score succeeds, and the roll is recorded as a `CheckMade`. They differ only in the label,
    die, score key, outcome key, and the wording of the refusal. A None score means the sheet
    has none, so nothing is rolled."""
    if character.kind == PC_KIND:
        raise InvalidInput(pc_message)
    db, campaign_id = hc.session()
    if score is None:
        if missing_text is None:
            raise InvalidInput(f"{character.name} has no {label} score to check")
        record(db, campaign_id, EffectNoted(name=character.name, text=missing_text))
        return {
            "character": character.name,
            "check": label,
            key: None,
            "roll": None,
            outcome: None,
            "cause": cause,
        }
    roll = roll_total(dice, rng)
    success = roll <= score
    record(
        db,
        campaign_id,
        CheckMade(
            name=character.name,
            check=label,
            stat=key,
            stat_name=label,
            roll=roll,
            bonus=0,
            total=roll,
            difficulty=score,
            success=success,
            cause=cause,
        ),
    )
    return {
        "character": character.name,
        "check": label,
        key: score,
        "roll": roll,
        outcome: success,
        "cause": cause,
    }


def check_morale(name: str, cause: str, ctx: ToolContext) -> dict[str, Any]:
    """Roll 2d6 against an NPC's Morale score, which comes from its statblock (an NPC with no
    statblock uses the rule set's default). A roll no higher than the score holds; a higher roll
    breaks. Records the check, or notes that the character has no Morale score to check. The
    source gives Morale only to NPCs and monsters: player characters never make this check and
    carry no Morale score of their own."""

    def action(hc: HookContext) -> dict[str, Any]:
        character = hc.character(name)
        return _score_check(
            hc,
            character,
            label="Morale",
            key="morale",
            score=character.stats.get("morale"),
            dice="2d6",
            outcome="holds",
            cause=cause,
            rng=ctx.world.rng,
            pc_message="player characters never make Morale checks",
            missing_text=(
                f"{character.name} has no Morale score, so no Morale check is rolled ({cause})."
            ),
        )

    return ctx.world.with_hook_context(action)


def instinct_check(name: str, instinct: int, cause: str, ctx: ToolContext) -> dict[str, Any]:
    """Check an NPC's Instinct: roll 1d10 and compare it to the score in the statblock. A roll no
    higher than the Instinct means the creature acts on impulse. Instinct is a statblock score,
    not a character stat; player characters never check Instinct."""
    value = whole_number(instinct, "instinct", 0, 10)

    def action(hc: HookContext) -> dict[str, Any]:
        return _score_check(
            hc,
            hc.character(name),
            label="Instinct",
            key="instinct",
            score=value,
            dice="1d10",
            outcome="impulsive",
            cause=cause,
            rng=ctx.world.rng,
            pc_message="player characters never check Instinct",
        )

    return ctx.world.with_hook_context(action)


def gain_growth(character: str, ctx: ToolContext) -> dict[str, Any]:
    """Roll the character's background Growth table for a level's growth. Apply the result with
    improve_attribute, spend_skill_point, or the appropriate counter."""

    def identify(hc: HookContext) -> dict[str, str]:
        found = hc.character(character)
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
            found = hc.character(character)
            return {"bonus": found.modifier(spec, "cha")}

        bonus = int(ctx.world.with_hook_context(read)["bonus"])
    roll = roll_total("2d6", ctx.world.rng)
    total = roll + bonus

    def action(hc: HookContext) -> dict[str, Any]:
        nonlocal total
        found = hc.character(character)
        db, campaign_id = hc.session()
        override = ""
        for triggered in reactions.match_reaction_roll(
            found, ctx.world.character, ctx.world.features, total
        ):
            effect = triggered.reaction.effect
            if effect["op"] == "add_total":
                total += int(effect["amount"])
                reactions.use(db, campaign_id, found, triggered)
            elif effect["op"] == "reroll":
                rerolled = roll_total("2d6", ctx.world.rng) + bonus
                total = max(total, rerolled)
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


def xp_track(progression: WWNProgression, rate: str) -> tuple[int, ...]:
    """The experience thresholds of one named track from the progression data."""
    track = progression.xp.get(rate)
    if track is None:
        options = ", ".join(sorted(progression.xp))
        raise InvalidInput(
            f"there is no experience track named {rate!r}; the tracks are: {options}"
        )
    return track


def _xp_rate(ctx: ToolContext, progression: WWNProgression, rate: str = "") -> str:
    """The campaign's experience track, or the explicit override the GM gave."""
    chosen = rate or str(ctx.world.campaign_options().get("xp_rate", ""))
    if not chosen:
        chosen = next(iter(progression.xp))
    if chosen not in progression.xp:
        options = ", ".join(sorted(progression.xp))
        raise InvalidInput(
            f"there is no experience track named {chosen!r}; the tracks are: {options}"
        )
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
    progression = require_wwn_progression(ctx.world)
    rate = _xp_rate(ctx, progression, rate)
    table = xp_track(progression, rate)

    def validate_level(hc: HookContext) -> int:
        found = hc.character(character)
        level = found.counters.get("level", 1)
        return _chart_level(level, progression, found.name, "experience")

    ctx.world.with_hook_context(validate_level)
    changed = ctx.world.adjust_counter(character, "xp", value)
    total = int(changed["value"])

    def action(hc: HookContext) -> dict[str, Any]:
        found = hc.character(character)
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


def _level_facts(
    character: str,
    ctx: ToolContext,
    progression: WWNProgression,
    rate: str,
) -> dict[str, Any]:
    """Everything a level projection needs, read once and without changing anything."""
    spec = ctx.world.character
    table_xp = xp_track(progression, rate)

    def read(hc: HookContext) -> dict[str, Any]:
        found = hc.character(character)
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
        ids = class_ids(found)
        if not ids:
            raise InvalidInput(f"{found.name} has no class, so has no level to gain")
        level_hp_bonus = sum(
            spec.features[feature_id].level_hp_bonus * count
            for feature_id, count in found.features.items()
            if feature_id in spec.features
        )
        return {
            "name": found.name,
            "level": level,
            "xp": xp,
            "threshold": threshold,
            "class_ids": ids,
            "table": advancement_table(found, progression),
            "casting_class": casting_class_id(found, progression),
            "traditions": tradition_ids(found, progression),
            "hp": found.stats.get("hp", 0),
            "con": found.modifier(spec, "con"),
            "level_hp_bonus": level_hp_bonus,
        }

    return ctx.world.with_hook_context(read)


def project_level(
    found: Mapping[str, Any], progression: WWNProgression, *, rate: str
) -> LevelGrants:
    """Project one new level's gains without rolling dice or changing the character.

    Both the page summary and the applied level use this one projection, so their automatic
    gains and pick pools can never disagree.
    """
    level = int(found["level"])
    new_level = level + 1
    table: ClassTable = found["table"]
    casting_class = str(found["casting_class"] or found["class_ids"][0])
    casting = (
        casting_row(str(found["casting_class"]), new_level, progression)
        if found["casting_class"] is not None
        else None
    )
    pools = {
        "skill_points": 4 if table.quick_learner else 3,
        "focus_picks": 1 if new_level in table.focus_levels else 0,
        "art_picks": sum(
            _art_picks(casting_class, tradition, new_level, progression)
            for tradition in found["traditions"]
        ),
        "spell_picks": _spell_picks(
            str(found["casting_class"] or ""),
            str(found["traditions"][0]) if found["traditions"] else "",
            progression,
        ),
    }
    attack = table.attack[new_level - 1]
    values = {
        "hp_dice": table.hit_die,
        "hp_constant": table.hp_constant,
        "con": int(found["con"]),
        "old_hp": int(found["hp"]),
        "level_hp_bonus": int(found["level_hp_bonus"]),
        "attack_bonus": attack,
        "castings": casting.casts if casting is not None else None,
    }
    lines = [
        f"Hit points: rerolled for level {new_level} (the old total plus one if the roll is worse).",
        f"Attack bonus: {attack}.",
        f"Skill points: {pools['skill_points']}.",
    ]
    if pools["focus_picks"]:
        lines.append(f"Focus pick: {pools['focus_picks']}.")
    if pools["art_picks"]:
        lines.append(f"Art pick: {pools['art_picks']}.")
    if pools["spell_picks"]:
        lines.append(f"Spell pick: {pools['spell_picks']}.")
    if casting is not None:
        lines.append(f"Castings: {casting.casts}.")
    lines.append("Saving throws improve by one.")
    return LevelGrants(
        level=level,
        new_level=new_level,
        xp=int(found["xp"]),
        threshold=int(found["threshold"]),
        rate=rate,
        pools=pools,
        values=values,
        lines=tuple(lines),
    )


def apply_level_up(character: str, ctx: ToolContext, rate: str = "") -> dict[str, Any]:
    """Raise a character one level, if their experience reaches the next threshold. Rolls the
    class hit dice for the new level's HP as the book says, sets the class attack bonus, adds
    the skill points the class grants, grants any focus or art pick due, raises a spellcaster's
    castings, and recomputes the derived stats (saves, Strain, Effort)."""
    progression = require_wwn_progression(ctx.world)
    rate = _xp_rate(ctx, progression, rate)
    spec = ctx.world.character
    found = _level_facts(character, ctx, progression, rate)
    grants = project_level(found, progression, rate=rate)
    new_level = grants.new_level
    ctx.world.adjust_counter(character, "level", 1)
    # The book rerolls the class hit dice for the new level, adding Constitution to each die,
    # and keeps the old total plus one when the reroll is no better.
    rolled = roll_total(f"{new_level}d{grants.values['hp_dice']}", ctx.world.rng)
    new_hp = rolled + new_level * (int(grants.values["hp_constant"]) + int(grants.values["con"]))
    gained = max(1, new_hp)
    if gained <= int(grants.values["old_hp"]):
        gained = int(grants.values["old_hp"]) + 1
    gained += int(grants.values["level_hp_bonus"])
    ctx.world.set_stat_max(character, "hp", gained, cause=f"level {new_level}")
    ctx.world.set_stat_max(
        character,
        "attack_bonus",
        int(grants.values["attack_bonus"]),
        cause=f"level {new_level}",
    )
    for counter in ("skill_points", "focus_picks", "art_picks", "spell_picks"):
        amount = grants.granted(counter)
        if amount:
            ctx.world.adjust_counter(character, counter, amount)
    castings = grants.values.get("castings")
    if castings is not None:
        ctx.world.set_stat_max(character, "castings", int(castings), cause=f"level {new_level}")
    _recompute_derived(ctx, character, spec, cause=f"level {new_level}")
    return {
        "character": character,
        "level": new_level,
        "old_level": grants.level,
        "new_level": new_level,
        "hp": gained,
        "attack_bonus": grants.values["attack_bonus"],
        "skill_points": grants.pools["skill_points"],
        "focus_picks": grants.pools["focus_picks"],
        "art_picks": grants.pools["art_picks"],
        "spell_picks": grants.pools["spell_picks"],
        "castings": castings,
        "rate": rate,
        "grants": dict(grants.pools),
    }


def _spell_picks(casting_class: str, tradition: str, progression: WWNProgression) -> int:
    """The new spells a level grants, as the class's casting table configures. A full Mage of
    High Magic learns two; a full Necromancer or Elementalist learns one, as the source's
    class charts say."""
    table_id = progression.casting_class_tables.get(casting_class.strip().casefold())
    if table_id is None:
        return 0
    table = progression.casting_tables[table_id]
    if table_id == "full_mage":
        tag = tradition_tag(tradition, progression)
        if tag in ("necromancer", "elementalist"):
            return 1
    return table.spell_picks


def _art_picks(class_id: str, tradition: str, level: int, progression: WWNProgression) -> int:
    """The art picks a class chart grants at a level: a full Mage uses the full chart, while a
    partial class, including a second partial class, uses the partial chart."""
    tag = tradition_tag(tradition, progression)
    if not tag:
        return 0
    chart = (
        progression.art_levels_full
        if class_table(class_id, progression).art_chart == "full"
        else progression.art_levels_partial
    )
    return 1 if level in chart.get(tag, frozenset()) else 0


def level_up_summary(character: str, ctx: ToolContext) -> dict[str, Any]:
    """What the next level grants before it is taken, so the level-up page can show the automatic
    gains and the size of each pick pool the player will spend."""
    progression = require_wwn_progression(ctx.world)
    rate = _xp_rate(ctx, progression)
    found = _level_facts(character, ctx, progression, rate)
    grants = project_level(found, progression, rate=rate)
    return {
        "character": character,
        **grants.as_summary(),
    }


def level_up(character: str, ctx: ToolContext, rate: str = "") -> dict[str, Any]:
    """The GM's level-up tool. An NPC advances at once; a player character is left for the
    player to advance on the character sheet, because its picks are the player's choices."""

    def read(hc: HookContext) -> tuple[str, str]:
        found = hc.character(character)
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
    """The Foci a focus pick offers: the focus picks of both class templates, with a rank-2
    Focus offered once its rank-1 Focus is known."""
    spec = ctx.world.character

    def read(hc: HookContext) -> dict[str, Any]:
        found = hc.character(character)
        ids = class_ids(found)
        if not ids:
            raise InvalidInput(f"{found.name} has no class")
        return {
            "options": [feature.id for feature in _focus_features(spec, ids)],
            "features": dict(found.features),
        }

    found = ctx.world.with_hook_context(read)
    return _feature_options(spec, found["options"], found["features"], set(pick.exclude))


def _focus_features(spec: CharacterSpec, ids: Iterable[str]) -> list[Feature]:
    """Every Focus the templates' focus picks offer, once, in template order."""
    features: list[Feature] = []
    seen: set[str] = set()
    for class_id in ids:
        template = spec.templates.get(class_id)
        if template is None:
            raise InvalidInput(f"there is no class template named {class_id!r}")
        for choice in template.feat_picks:
            tags = choice.tags or ((choice.tag,) if choice.tag else ())
            if "focus" not in tags:
                continue
            for feature in feature_pick_options(spec, choice):
                if feature.id not in seen:
                    seen.add(feature.id)
                    features.append(feature)
    return features


def art_options(
    character: str, pick: Any, ctx: ToolContext, level: int | None = None
) -> list[dict[str, Any]]:
    """The arts of the character's own traditions that they do not already know."""
    spec = ctx.world.character
    progression = require_wwn_progression(ctx.world)

    def read(hc: HookContext) -> dict[str, Any]:
        found = hc.character(character)
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
        found = hc.character(character)
        casting_class = casting_class_id(found, progression)
        level = new_level or found.counters.get("level", 1)
        row = casting_row(casting_class, level, progression) if casting_class else None
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
    skill = _resolve_skill(spec, skill)

    def read(hc: HookContext) -> int:
        found = hc.character(character)
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
    skill = _resolve_skill(spec, skill)

    def read(hc: HookContext) -> dict[str, int]:
        found = hc.character(character)
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
        cost = progression.skill_costs.get(new_level)
        needs_level = progression.skill_minimums.get(new_level)
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
        return {
            "points": points,
            "before": before,
            "new_level": new_level,
            "cost": cost,
        }

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


def improve_attribute(character: str, stat: str, ctx: ToolContext) -> dict[str, Any]:
    """Spend skill points on a permanent +1 to one of the six attributes. The progression data
    sets each boost's cost, minimum character level, and the attribute maximum."""
    spec = ctx.world.character
    progression = require_wwn_progression(ctx.world)
    stat = _resolve_attribute(spec, stat)

    def read(hc: HookContext) -> dict[str, int]:
        found = hc.character(character)
        boosts = found.counters.get("attribute_boosts", 0)
        next_step = boosts + 1
        if next_step not in progression.attribute_costs:
            raise InvalidInput(
                f"{found.name} has already bought all {max(progression.attribute_costs)} "
                f"attribute boosts"
            )
        cost = progression.attribute_costs[next_step]
        needs_level = progression.attribute_minimums[next_step]
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
        if value >= progression.attribute_maximum:
            raise InvalidInput(
                f"{found.name}'s {spec.stats[stat].name} is already at "
                f"{progression.attribute_maximum}"
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
    progression = require_wwn_progression(ctx.world)
    feature = _resolve_feature(spec, focus)
    focus = feature.id
    if feature.tag not in ("focus", "origin"):
        raise InvalidInput(f"{feature.name} is not a focus")
    required = _prior_focus(feature)
    if required is not None and required not in spec.features:
        required = None

    def read(hc: HookContext) -> dict[str, Any]:
        found = hc.character(character)
        if found.counters.get("focus_picks", 0) < 1:
            raise InvalidInput(f"{found.name} has no focus picks to spend")
        _check_focus_class(spec, class_ids(found), feature)
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
            value = min(progression.attribute_maximum, stats.get(stat, 0) + delta)
            ctx.world.set_stat_max(character, stat, value, cause=f"the focus {feature.name}")
            stats[stat] = value
        elif op == "skill":
            skill = str(effect["id"])
            if skill not in spec.skills:
                raise InvalidInput(f"{feature.name} names no skill {skill!r}")
            skill_progression = spec.skill_progression
            if skill_progression is None:
                raise InvalidInput("the rule set has no skill progression")
            before = spec.skill_level(skills, skill)
            value, remaining = _raise_with_skill_points(
                spec,
                skills,
                skill,
                FOCUS_BONUS_SKILL_POINTS * max(1, delta),
                progression.skill_costs,
                skill_progression.maximum,
            )
            if value > before:
                ctx.world.raise_skill(character, skill, value, cause=f"the focus {feature.name}")
                skills[skill] = value
            if remaining:
                ctx.world.adjust_counter(character, "skill_points", remaining)
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


def _raise_with_skill_points(
    spec: CharacterSpec,
    skills: Mapping[str, int],
    skill: str,
    points: int,
    costs: Mapping[int, int],
    maximum: int,
) -> tuple[int, int]:
    """Raise a skill as far as a budget of skill points allows, and return the level reached plus
    the points left. The book keeps leftover Focus skill points as credit toward a later advance;
    the engine keeps them as spendable skill points instead."""
    level = spec.skill_level(skills, skill)
    while level < maximum:
        cost = costs.get(level + 1)
        if cost is None or cost > points:
            break
        points -= cost
        level += 1
    return level, points


def _check_focus_class(spec: CharacterSpec, ids: Iterable[str], feature: Feature) -> None:
    ids = list(ids)
    if not ids or feature.tag != "focus" or feature.rank <= 0:
        return
    choices = _focus_features(spec, ids)
    if feature.rank == 1:
        allowed = any(option.id == feature.id for option in choices)
    else:
        allowed = any(
            option.rank == 1
            and option.name == feature.name
            and option.tag == feature.tag
            and option.feature_type == feature.feature_type
            for option in choices
        )
    if not allowed:
        names = " or ".join(
            spec.templates[class_id].name for class_id in ids if class_id in spec.templates
        )
        raise InvalidInput(f"{feature.name} is not available to {names}")


def learn_art(character: str, art: str, ctx: ToolContext) -> dict[str, Any]:
    """Spend an art pick on an art of the character's own configured tradition."""
    spec = ctx.world.character
    progression = require_wwn_progression(ctx.world)
    feature = _resolve_feature(spec, art)
    art = feature.id
    if feature.feature_type != "feat" or feature.tag not in progression.art_levels_partial:
        raise InvalidInput(f"{feature.name} is not an art")

    def read(hc: HookContext) -> dict[str, Any]:
        found = hc.character(character)
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
    feature = _resolve_feature(spec, spell)
    spell = feature.id
    if feature.feature_type != "spell":
        raise InvalidInput(f"{feature.name} is not a spell")

    def read(hc: HookContext) -> dict[str, Any]:
        found = hc.character(character)
        tags = tradition_tags(found, progression)
        if not tags:
            raise InvalidInput(f"{found.name} has no magical tradition, so cannot learn spells")
        if feature.tag not in {*tags, "high"}:
            raise InvalidInput(
                f"{feature.name} is not a High Magic spell or a spell of {found.name}'s "
                f"traditions, {', '.join(tags)}"
            )
        class_id = casting_class_id(found, progression)
        row = (
            casting_row(class_id, found.counters.get("level", 1), progression)
            if class_id is not None
            else None
        )
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


# Marks that count consecutive days without food or water.
PRIVATION_FOOD = "privation_food"
PRIVATION_WATER = "privation_water"


def _hook_items(hc: HookContext, key: str) -> tuple[str, ...]:
    """A non-empty item-name list from the rule set's hooks options."""
    value = (hc.hooks.options if hc.hooks is not None else {}).get(key)
    if (
        not isinstance(value, (list, tuple))
        or not value
        or not all(isinstance(name, str) and name.strip() for name in value)
    ):
        raise InvalidInput(f"the WWN hooks definition must provide a non-empty {key} list")
    return tuple(name for name in value)


def _hook_text(hc: HookContext, key: str) -> str:
    """A non-empty text option from the rule set's hooks options."""
    value = (hc.hooks.options if hc.hooks is not None else {}).get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidInput(f"the WWN hooks definition must provide {key}")
    return value


def _hook_int(hc: HookContext, key: str) -> int:
    """A whole-number option from the rule set's hooks options."""
    value = (hc.hooks.options if hc.hooks is not None else {}).get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidInput(f"the WWN hooks definition must provide a whole-number {key}")
    return value


def _carried_matching(
    hc: HookContext, character: str, names: Iterable[str]
) -> dict[str, Any] | None:
    """The first carried item whose name matches one of `names` and still has a use."""
    found = hc.character(character)
    for item in found.items:
        if (
            any(inventory_service.same_item_name(item.name, name) for name in names)
            and item.max_uses > 0
            and item.uses > 0
        ):
            return item
    return None


def _spend_one(hc: HookContext, character: str, names: Iterable[str]) -> str:
    """Spend one use of the first matching consumable and return its name, or "" if none."""
    item = _carried_matching(hc, character, names)
    if item is None or hc.inventory is None:
        return ""
    db, campaign_id = hc.session()
    inventory_service.use_item(
        db,
        campaign_id,
        hc.inventory,
        character,
        item.name,
        ignore_ready=True,
    )
    return item.name


def _has_water_container(
    hc: HookContext, character: str, containers: Iterable[str]
) -> bool:
    found = hc.character(character)
    return any(
        any(inventory_service.same_item_name(item.name, name) for name in containers)
        for item in found.items
    )


def _apply_privation(hc: HookContext, character: str, kind: str, cause: str) -> None:
    """Add the day's System Strain for hunger or thirst and keep the streak."""
    found = hc.character(character)
    mark = PRIVATION_FOOD if kind == "food" else PRIVATION_WATER
    streak = int(found.marks.get(mark) or 0) + 1
    if kind == "food":
        strain = 0 if streak == 1 else 1
    else:
        strain = 2 if streak == 1 else 3
    db, campaign_id = hc.session()
    if strain:
        available = current(found, "strain")
        taken = min(strain, available)
        if taken:
            record(
                db, campaign_id, Harmed(name=found.name, stat="strain", taken=taken, cause=cause)
            )
        if available <= strain:
            hc.set_mark(
                found.name,
                "privation_deadline",
                True,
                f"{found.name}'s System Strain is at its maximum; a Physical save is owed by dawn.",
            )
    hc.set_mark(found.name, mark, streak, f"{found.name} lacks {kind} ({streak} day(s)).")


def before_rest(hc: HookContext, length: str, start: int, hours_per_part: int) -> None:
    """Spend a day's food and water for each party member, and record privation.

    The source gives a night's rest only to a character who is fed and watered. The engine
    consumes the supply; a character without it is held back from recovery by `held_back`."""
    if length not in ("night", "extended"):
        return
    days = 7 if length == "extended" else 1
    food_items = _hook_items(hc, "food_items")
    water_items = _hook_items(hc, "water_items")
    water_containers = _hook_items(hc, "water_containers")
    state = hc.state()
    for character in [c for c in state.characters.values() if c.kind == PC_KIND]:
        fed = 0
        watered = 0
        for _ in range(days):
            food = _spend_one(hc, character.name, food_items)
            if food:
                fed += 1
            day_water = bool(
                food
                and any(
                    inventory_service.same_item_name(food, name) for name in water_items
                )
            )
            if not day_water:
                water = _spend_one(hc, character.name, water_items)
                day_water = bool(
                    water or _has_water_container(hc, character.name, water_containers)
                )
            if day_water:
                watered += 1
        if fed < days:
            _apply_privation(hc, character.name, "food", "a day without food")
        else:
            hc.set_mark(character.name, PRIVATION_FOOD, None, "food privation ended")
        if watered < days:
            _apply_privation(hc, character.name, "water", "a day without water")
        else:
            hc.set_mark(character.name, PRIVATION_WATER, None, "water privation ended")


def held_back(hc: HookContext, name: str) -> str:
    """What stops a character recovering on a rest, or "" when nothing does."""
    found = hc.character(name)
    missing = [
        kind
        for kind, mark in (("food", PRIVATION_FOOD), ("water", PRIVATION_WATER))
        if found.marks.get(mark)
    ]
    if not missing:
        return ""
    return " and ".join(missing) + " privation"


def stabilize(hc: HookContext, name: str) -> str:
    """Refuse mundane stabilization without the Heal check the source requires.

    Magic and aid items stabilize through their own paths, so this guard applies only to the
    ordinary `stabilize` tool. The check must have succeeded earlier in the same turn, at the
    rule's minimum difficulty, with the Heal skill."""
    skill = _hook_text(hc, "stabilize_skill")
    difficulty = _hook_int(hc, "stabilize_difficulty")
    db, campaign_id = hc.session()
    events = events_since(db, campaign_id)
    turn_start = 0
    for row in events:
        if row.kind == "turn.begun":
            turn_start = row.id
    for row in events:
        if row.id <= turn_start or row.kind != "check.made":
            continue
        check = decode(row)
        if not isinstance(check, CheckMade) or not check.success:
            continue
        if check.skill == skill and check.difficulty >= difficulty:
            return ""
    label = skill.replace("_", " ").title()
    return (
        f"stabilizing needs a successful {label} check first: call roll_check with the healer's "
        f"{label} skill at difficulty {difficulty} plus the full rounds since the fall, +2 "
        "without a healer's pouch, and call stabilize only after that check succeeds."
    )


def after_rest(
    hc: HookContext,
    name: str,
    length: str,
    start: int,
    end: int,
    interrupted: bool,
) -> None:
    """WWN recovery, run once per party member after a party rest has already moved the clock."""
    progression = require_wwn_progression(hc)
    _recover_with_hook(hc, name, length, interrupted, progression)


def _recover_with_hook(
    hc: HookContext,
    character: str,
    length: str,
    interrupted: bool,
    progression: WWNProgression,
) -> dict[str, Any]:
    """Record a rest's recovery: the night's hit points, Strain, and castings, and the Effort
    whose duration ends with the rest."""
    found = hc.character(character)
    db, campaign_id = hc.session()
    restored: dict[str, int] = {}

    def recover(stat: str, amount: int, why: str) -> None:
        if amount <= 0 or stat not in found.stats:
            return
        record(
            db,
            campaign_id,
            Recovered(name=found.name, stat=stat, amount=amount, cause=why),
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


def _recompute_derived(
    ctx: ToolContext, character: str, spec: CharacterSpec, *, cause: str
) -> None:
    """Write the derived stats for the character's new level, counter values, and skills."""

    def action(hc: HookContext) -> None:
        found = hc.character(character)
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
        found = hc.character(character)
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
        found = hc.character(character)
        dexterity = found.modifier(spec, "dex")
        return found.armor or 10 + dexterity

    return ctx.world.with_hook_context(action)
