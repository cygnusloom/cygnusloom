"""Python side of the Worlds Without Number rule set: checks, morale, advancement, and gear.

The class, XP, and casting tables below are ruleset-specific data. They are Python rather than
JSON because they drive arithmetic (hit dice, attack bonuses, focus picks, castings, prepared
spells), not lookups the GM makes. The values are from the free book, paraphrased into numbers:

- Hit dice are ``level d6 + constant x level``: Warrior +2, Expert +0, Mage -1, and the
  Adventurer pairings use the constant of their partial Warrior (+2) or partial Mage (-1) side.
- Attack bonuses and focus-pick levels are the printed class tables (focus picks at 2, 5, 7, 10).
- Fast XP is 0, 3, 6, 12, 18, 27, 39, 54, 72, 93; slow XP is 0, 6, 15, 24, 36, 51, 69, 87,
  105, 139. Fast is the default; `gain_xp` takes ``rate="slow"`` for the other.
- Full-mage castings per day are 1, 1, 2, 2, 3, 3, 4, 4, 5, 6 and prepared spells 3, 3, 4, 5,
  6, 7, 8, 9, 10, 12.
- Partial-mage castings are 1, 1, 1, 2, 2, 3, 3, 3, 4, 4 and prepared 2, 3, 3, 4, 5, 6, 7, 7,
  8, 9.
- Dual partial-mage castings are 1, 1, 2, 2, 2, 3, 3, 4, 4, 5 and prepared 3, 4, 5, 6, 8, 9,
  10, 12, 13, 15.
- Highest spell levels are 1, 1, 2, 2, 3, 3, 4, 4, 5, 5 for a full mage and 1, 1, 1, 1, 2, 2,
  2, 2, 3, 3 for a partial mage. A dual partial mage uses 1, 1, 1, 2, 2, 2, 3, 3, 3, 4.

The design's effort rule is 1 + Magic skill + the better of the Int and Cha modifiers, minus 1
for a partial mage, and never less than 1.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from cygnusforge.content.character import CharacterSpec, derived_stats
from cygnusforge.rules.context import ToolContext
from cygnusforge.rules.dice import roll_total
from cygnusforge.services.common import InvalidInput, whole_number
from cygnusforge.services.events import (
    ArmorSet,
    CheckMade,
    EffectNoted,
    FeatureLearned,
    MaxChanged,
    StatRestored,
    name_key,
    record,
)
from cygnusforge.services.hooks import HookContext

MAX_LEVEL = 10
MAX_SKILL_LEVEL = 4
MAX_BASE_ARMOR = 100
MAX_ATTRIBUTE = 18


# The size of the die each class rolls for a level's hit points, and the flat constant it adds to
# every level's roll.
@dataclass(frozen=True)
class ClassTable:
    """One class's advancement table. `hp_constant` is added to each level's hit die, `attack`
    holds the attack bonus for levels 1 to 10, and `focus_levels` are the levels that grant a
    focus pick. `quick_learner` adds a second skill point each level."""

    hit_die: int
    hp_constant: int
    attack: tuple[int, ...]
    focus_levels: frozenset[int]
    quick_learner: bool = False


FOCUS_LEVELS = frozenset({2, 5, 7, 10})

CLASS_TABLES: Mapping[str, ClassTable] = {
    "warrior": ClassTable(6, 2, (1, 2, 3, 4, 5, 6, 7, 8, 9, 10), FOCUS_LEVELS),
    "expert": ClassTable(6, 0, (0, 1, 1, 2, 2, 3, 3, 4, 4, 5), FOCUS_LEVELS, quick_learner=True),
    "mage": ClassTable(6, -1, (0, 0, 0, 0, 1, 1, 1, 1, 1, 2), FOCUS_LEVELS),
    # The class ids the WWN JSON uses.
    "adventurer_expert_warrior": ClassTable(
        6, 2, (1, 2, 2, 3, 4, 5, 5, 6, 6, 7), FOCUS_LEVELS, quick_learner=True
    ),
    "adventurer_expert_mage": ClassTable(
        6, 0, (0, 1, 1, 2, 2, 3, 3, 4, 4, 5), FOCUS_LEVELS, quick_learner=True
    ),
    "adventurer_warrior_mage": ClassTable(6, 2, (1, 2, 2, 3, 4, 5, 5, 6, 6, 7), FOCUS_LEVELS),
    "adventurer_mage_mage": ClassTable(6, -1, (0, 0, 0, 0, 1, 1, 1, 1, 1, 2), FOCUS_LEVELS),
    # The longer names from the design document, kept as aliases.
    "adventurer_partial_expert_partial_warrior": ClassTable(
        6, 2, (1, 2, 2, 3, 4, 5, 5, 6, 6, 7), FOCUS_LEVELS, quick_learner=True
    ),
    "adventurer_partial_expert_partial_mage": ClassTable(
        6, 0, (0, 1, 1, 2, 2, 3, 3, 4, 4, 5), FOCUS_LEVELS, quick_learner=True
    ),
    "adventurer_partial_mage_partial_warrior": ClassTable(
        6, 2, (1, 2, 2, 3, 4, 5, 5, 6, 6, 7), FOCUS_LEVELS
    ),
    "adventurer_partial_mage_partial_mage": ClassTable(
        6, -1, (0, 0, 0, 0, 1, 1, 1, 1, 1, 2), FOCUS_LEVELS
    ),
}

# Class ids whose spellcasting uses the single partial-mage table, and those that use the dual
# partial-mage table.
PARTIAL_MAGE_CLASSES = frozenset(
    {
        "adventurer_expert_mage",
        "adventurer_warrior_mage",
        "adventurer_partial_expert_partial_mage",
        "adventurer_partial_mage_partial_warrior",
    }
)
DUAL_MAGE_CLASSES = frozenset(
    {
        "adventurer_mage_mage",
        "adventurer_partial_mage_partial_mage",
    }
)

# Cumulative experience needed for each level, indexed from level 1.
WWN_XP: tuple[int, ...] = (0, 3, 6, 12, 18, 27, 39, 54, 72, 93)
WWN_XP_SLOW: tuple[int, ...] = (0, 6, 15, 24, 36, 51, 69, 87, 105, 139)


@dataclass(frozen=True)
class CastingRow:
    """A spellcaster's spell limits at one level."""

    max_spell_level: int
    casts: int
    prepared: int


FULL_MAGE_CASTING: Mapping[int, CastingRow] = {
    level: CastingRow(max_level, casts, prepared)
    for level, (max_level, casts, prepared) in enumerate(
        (
            (1, 1, 3),
            (1, 1, 3),
            (2, 2, 4),
            (2, 2, 5),
            (3, 3, 6),
            (3, 3, 7),
            (4, 4, 8),
            (4, 4, 9),
            (5, 5, 10),
            (5, 6, 12),
        ),
        start=1,
    )
}

PARTIAL_MAGE_CASTING: Mapping[int, CastingRow] = {
    level: CastingRow(max_level, casts, prepared)
    for level, (max_level, casts, prepared) in enumerate(
        (
            (1, 1, 2),
            (1, 1, 3),
            (1, 1, 3),
            (1, 2, 4),
            (2, 2, 5),
            (2, 3, 6),
            (2, 3, 7),
            (2, 3, 7),
            (3, 4, 8),
            (3, 4, 9),
        ),
        start=1,
    )
}

DUAL_MAGE_CASTING: Mapping[int, CastingRow] = {
    level: CastingRow(max_level, casts, prepared)
    for level, (max_level, casts, prepared) in enumerate(
        (
            (1, 1, 3),
            (1, 1, 4),
            (1, 2, 5),
            (2, 2, 6),
            (2, 2, 8),
            (2, 3, 9),
            (3, 3, 10),
            (3, 4, 12),
            (3, 4, 13),
            (4, 5, 15),
        ),
        start=1,
    )
}

# The character mark that records committed Effort, keyed by duration and then tradition:
# {"scene": {"high": 1}, "day": {"necromancer": 2}}.
EFFORT_MARK = "effort_committed"
EFFORT_DURATIONS = ("scene", "day", "indefinite")

# A tradition's spell tag and its Effort stat. The class templates are named `high_mage`,
# `necromancer`, and `elementalist`; spells are tagged `high`, `necromancer`, and `elementalist`.
TRADITION_TAGS: Mapping[str, str] = {
    "high": "high",
    "high_mage": "high",
    "necromancer": "necromancer",
    "elementalist": "elementalist",
}
TRADITION_EFFORT: Mapping[str, str] = {
    "high": "effort_high",
    "necromancer": "effort_necromancer",
    "elementalist": "effort_elementalist",
}


@dataclass(frozen=True)
class StartingPackage:
    """One of the book's starting equipment packages. Items are named as the equipment list
    names them, so `give_item` finds them in the rule set's catalogue."""

    id: str
    name: str
    items: tuple[str, ...]
    silver: int = 0


STARTING_PACKAGES: Mapping[str, StartingPackage] = {
    package.id: package
    for package in (
        StartingPackage(
            "pkg_armored_warrior",
            "Armored Warrior",
            (
                "Pieced Armor",
                "Large Shield",
                "Short Sword",
                "Dagger",
                "Backpack",
                "Tinder box and 3 torches",
            ),
        ),
        StartingPackage(
            "pkg_mage_healer_scholar",
            "Mage, Healer, or Scholar",
            (
                "Dagger",
                "Dagger",
                "Staff",
                "Backpack",
                "Lantern",
                "Tinder box",
                "Oil, 2 pint flasks",
                "Writing kit",
                "Paper, 20 sheets",
                "Rations, 1 week",
                "Waterskin",
                "Healer's pouch",
            ),
            silver=80,
        ),
        StartingPackage(
            "pkg_ranger_archer",
            "Ranger or Archer",
            (
                "Buff Coat",
                "Bow, Large",
                "Arrows, 20",
                "Quiver",
                "Dagger",
                "Hand Axe",
                "Backpack",
                "Cooking utensils",
                "Rations, 1 week",
                "Waterskin",
                "Tinder box and 3 torches",
            ),
            silver=20,
        ),
        StartingPackage(
            "pkg_roguish_wanderer",
            "Roguish Wanderer",
            (
                "Buff Coat",
                "Small Shield",
                "Short Sword",
                "Throwing Blade",
                "Throwing Blade",
                "Throwing Blade",
                "Throwing Blade",
                "Throwing Blade",
                "Backpack",
                "Rations, 1 week",
                "Waterskin",
                "Tinder box and 3 torches",
                "Grappling hook",
                "Rope, 50 ft",
            ),
        ),
        StartingPackage(
            "pkg_adventuring_peasant",
            "Adventuring Peasant",
            (
                "War Shirt",
                "Large Shield",
                "Light Spear",
                "Dagger",
                "Backpack",
                "Rations, 1 week",
                "Mule and small cart",
                "Tinder box and 3 torches",
            ),
        ),
        StartingPackage(
            "pkg_gentry_wayfarer",
            "Gentry Wayfarer",
            (
                "Buff Coat",
                "Small Shield",
                "Short Sword",
                "Backpack",
                "Rations, 1 week",
                "Waterskin",
                "Fine suit of clothing",
                "Writing kit",
                "Paper, 20 sheets",
            ),
            silver=20,
        ),
    )
}


def class_table(class_id: str) -> ClassTable:
    """The advancement table for a class id. Raises InvalidInput for an unknown class."""
    found = CLASS_TABLES.get(class_id.strip().casefold())
    if found is None:
        options = ", ".join(sorted(CLASS_TABLES))
        raise InvalidInput(f"there is no class table for {class_id!r}; the classes are: {options}")
    return found


def casting_row(class_id: str, level: int) -> CastingRow | None:
    """The spellcasting limits for a class at a level, or None when the class cannot cast spells.
    A full mage uses the full table; a single partial mage uses the partial table; two partial
    mage classes use the dual table."""
    if not 1 <= level <= MAX_LEVEL:
        return None
    class_id = class_id.strip().casefold()
    if class_id == "mage":
        return FULL_MAGE_CASTING[level]
    if class_id in DUAL_MAGE_CLASSES:
        return DUAL_MAGE_CASTING[level]
    if class_id in PARTIAL_MAGE_CLASSES or "partial_mage" in class_id:
        return PARTIAL_MAGE_CASTING[level]
    return None


def is_partial_mage(class_id: str) -> bool:
    """Whether a class is a partial spellcaster, whose Effort maximum is one lower."""
    class_id = class_id.strip().casefold()
    return (
        class_id in PARTIAL_MAGE_CLASSES
        or class_id in DUAL_MAGE_CLASSES
        or ("partial_mage" in class_id)
    )


def tradition_tag(tradition: str) -> str:
    """The spell tag a tradition template grants, such as `high` for `high_mage`."""
    key = tradition.strip().casefold()
    return TRADITION_TAGS.get(key, key)


def tradition_effort_stat(tradition: str) -> str:
    """The Effort stat a tradition uses, or "" when the tradition is unknown."""
    return TRADITION_EFFORT.get(tradition_tag(tradition), "")


def max_effort(
    spec: CharacterSpec, stats: Mapping[str, int], skills: Mapping[str, int], class_id: str
) -> int:
    """The maximum Effort a spellcaster has: 1 + Magic skill + the better Int or Cha modifier,
    less 1 for a partial mage, and never below 1."""
    magic = max(0, spec.skill_level(skills, "magic"))
    best = max(spec.modifier(stats, "int"), spec.modifier(stats, "cha"))
    value = 1 + magic + best
    if is_partial_mage(class_id):
        value -= 1
    return max(1, value)


def roll_check(
    character: str,
    skill: str,
    stat: str,
    ctx: ToolContext,
    difficulty: int = 8,
    modifier: int = 0,
    cause: str = "",
) -> dict[str, Any]:
    """Roll the rule set's skill check for a character."""
    return ctx.world.roll_check(
        "skill",
        character,
        skill=skill,
        stat=stat,
        difficulty=difficulty,
        modifier=modifier,
        cause=cause,
    )


def check_morale(name: str, cause: str, ctx: ToolContext) -> dict[str, Any]:
    """Roll 2d6 against a character's Morale score. A roll no higher than the score holds; a higher
    roll breaks. Records the check, or notes that the character has no Morale score to check."""

    def action(hc: HookContext) -> dict[str, Any]:
        _, character = _character(hc, name)
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


def gain_xp(
    character: str,
    amount: int,
    cause: str,
    ctx: ToolContext,
    *,
    rate: str = "fast",
) -> dict[str, Any]:
    """Add experience to a character's `xp` counter. When the total reaches the next level's
    threshold, note that the GM may call `level_up`; experience never levels a character by
    itself."""
    value = whole_number(amount, "amount", 0, 1_000_000)
    if rate not in ("fast", "slow"):
        raise InvalidInput("rate must be 'fast' or 'slow'")
    table = WWN_XP_SLOW if rate == "slow" else WWN_XP
    changed = ctx.world.adjust_counter(character, "xp", value)
    total = int(changed["value"])

    def action(hc: HookContext) -> dict[str, Any]:
        _, found = _character(hc, character)
        level = found.counters.get("level", 1)
        threshold = table[level] if level < MAX_LEVEL else None
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


def level_up(character: str, ctx: ToolContext) -> dict[str, Any]:
    """Raise a character one level, if their experience reaches the next threshold. Rolls the
    class hit die for the new level's HP, sets the class attack bonus, adds skill points and any
    focus pick, raises a spellcaster's castings, and recomputes the derived stats (saves, Strain,
    Effort) for the new level."""
    spec = ctx.world.character

    def read(hc: HookContext) -> dict[str, Any]:
        _, found = _character(hc, character)
        level = found.counters.get("level", 1)
        xp = found.counters.get("xp", 0)
        if level >= MAX_LEVEL:
            raise InvalidInput(f"{found.name} is already at the highest level, {MAX_LEVEL}")
        threshold = WWN_XP[level]
        if xp < threshold:
            raise InvalidInput(
                f"{found.name} has {xp} experience; level {level + 1} needs {threshold}"
            )
        class_id = found.templates.get("class", "")
        if not class_id:
            raise InvalidInput(f"{found.name} has no class, so has no level to gain")
        return {
            "level": level,
            "class_id": class_id,
            "tradition": found.templates.get("tradition", ""),
            "hp": found.stats.get("hp", 0),
            "con": spec.modifier(found.stats, "con"),
            "stats": dict(found.stats),
            "skills": dict(found.skills),
        }

    found = ctx.world.with_hook_context(read)
    level = int(found["level"])
    new_level = level + 1
    table = class_table(str(found["class_id"]))
    ctx.world.adjust_counter(character, "level", 1)
    rolled = roll_total(f"1d{table.hit_die}", ctx.world.rng)
    gained = max(1, rolled + table.hp_constant + int(found["con"]))
    ctx.world.set_stat_max(character, "hp", int(found["hp"]) + gained, cause=f"level {new_level}")
    ctx.world.set_stat_max(
        character, "attack_bonus", table.attack[new_level - 1], cause=f"level {new_level}"
    )
    points = 2 if table.quick_learner else 1
    ctx.world.adjust_counter(character, "skill_points", points)
    focus_picks = 1 if new_level in table.focus_levels else 0
    if focus_picks:
        ctx.world.adjust_counter(character, "focus_picks", focus_picks)
    casting = casting_row(str(found["class_id"]), new_level)
    if casting is not None:
        ctx.world.set_stat_max(character, "castings", casting.casts, cause=f"level {new_level}")
    _recompute_derived(ctx, character, spec, cause=f"level {new_level}")
    return {
        "character": character,
        "level": new_level,
        "hp_gained": gained,
        "attack_bonus": table.attack[new_level - 1],
        "skill_points": points,
        "focus_picks": focus_picks,
        "castings": casting.casts if casting is not None else None,
    }


def spend_skill_point(character: str, skill: str, ctx: ToolContext) -> dict[str, Any]:
    """Spend one skill point to raise one skill by a level, up to level 4."""
    spec = ctx.world.character
    if skill not in spec.skills:
        options = ", ".join(sorted(spec.skills)) or "none"
        raise InvalidInput(f"there is no skill named {skill!r}; the skills are: {options}")

    def read(hc: HookContext) -> dict[str, int]:
        _, found = _character(hc, character)
        points = found.counters.get("skill_points", 0)
        if points < 1:
            raise InvalidInput(f"{found.name} has no skill points to spend")
        before = spec.skill_level(found.skills, skill)
        if before >= MAX_SKILL_LEVEL:
            raise InvalidInput(
                f"{found.name}'s {spec.skills[skill].name} is already at level {MAX_SKILL_LEVEL}"
            )
        return {"points": points, "before": before}

    found = ctx.world.with_hook_context(read)
    new_level = int(found["before"]) + 1
    ctx.world.raise_skill(character, skill, new_level, cause="a skill point")
    ctx.world.adjust_counter(character, "skill_points", -1)
    return {
        "character": character,
        "skill": skill,
        "skill_name": spec.skills[skill].name,
        "level": new_level,
        "remaining": int(found["points"]) - 1,
    }


def learn_focus(character: str, focus: str, ctx: ToolContext) -> dict[str, Any]:
    """Spend a focus pick on a focus. A second rank needs the first rank already held. The focus's
    creation effects are applied through the world's tools."""
    spec = ctx.world.character
    feature = spec.features.get(focus)
    if feature is None:
        raise InvalidInput(f"there is no feature named {focus!r}")
    if feature.tag != "focus":
        raise InvalidInput(f"{feature.name} is not a focus")
    required = _prior_focus(feature)
    if required is not None and required not in spec.features:
        required = None

    def read(hc: HookContext) -> dict[str, Any]:
        _, found = _character(hc, character)
        if found.counters.get("focus_picks", 0) < 1:
            raise InvalidInput(f"{found.name} has no focus picks to spend")
        if focus in found.features:
            raise InvalidInput(f"{found.name} already has {feature.name}")
        if required is not None and required not in found.features:
            raise InvalidInput(
                f"{feature.name} is the second rank; {found.name} needs "
                f"{spec.features[required].name} first"
            )
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
            before = spec.skill_level(skills, skill)
            value = min(MAX_SKILL_LEVEL, before + delta)
            if value > before:
                ctx.world.raise_skill(character, skill, value, cause=f"the focus {feature.name}")
                skills[skill] = value
        elif op == "counter":
            counter = str(effect["id"])
            ctx.world.adjust_counter(character, counter, delta)
        elif op == "armor":
            current = _armor_base(ctx, character, spec)
            set_ac(character, max(0, current + delta), ctx)
        elif op == "note":
            notes.append(str(effect.get("text", "")))
        else:
            raise InvalidInput(f"{feature.name} has an unknown effect {op!r}")
    _learn_feature(ctx, character, feature, notes, cause="a focus pick")
    remaining = ctx.world.adjust_counter(character, "focus_picks", -1)
    return {
        "character": character,
        "focus": focus,
        "name": feature.name,
        "rank": feature.rank,
        "focus_picks": remaining["value"],
    }


def learn_spell(character: str, spell: str, ctx: ToolContext) -> dict[str, Any]:
    """Teach a spell to a spellcaster: it must be a spell feature of their tradition, no higher
    than the highest spell level their class and level can cast."""
    spec = ctx.world.character
    feature = spec.features.get(spell)
    if feature is None:
        raise InvalidInput(f"there is no feature named {spell!r}")
    if feature.feature_type != "spell":
        raise InvalidInput(f"{feature.name} is not a spell")

    def read(hc: HookContext) -> dict[str, Any]:
        _, found = _character(hc, character)
        tradition = found.templates.get("tradition", "")
        if not tradition:
            raise InvalidInput(f"{found.name} has no magical tradition, so cannot learn spells")
        if feature.tag != tradition_tag(tradition):
            raise InvalidInput(
                f"{feature.name} is not a spell of {found.name}'s tradition, {tradition}"
            )
        class_id = found.templates.get("class", "")
        row = casting_row(class_id, found.counters.get("level", 1))
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
    """Give a character the items and starting silver of one of the book's equipment packages."""
    found = STARTING_PACKAGES.get(package.strip().casefold())
    if found is None:
        options = ", ".join(sorted(STARTING_PACKAGES))
        raise InvalidInput(f"there is no package {package!r}; the packages are: {options}")
    given: list[str] = []
    for item in found.items:
        ctx.world.give_item(character, item)
        given.append(item)
    if found.silver:
        ctx.world.change_currency(character, found.silver, cause=f"the {found.name} package")
    return {
        "character": character,
        "package": found.id,
        "name": found.name,
        "items": given,
        "silver": found.silver,
    }


def set_ac(
    character: str = "",
    base_armor: int | None = None,
    ctx: ToolContext | None = None,
    *,
    name: str = "",
    ac: int | None = None,
) -> dict[str, Any]:
    """Set a character's Armor Class. With `base_armor`, the AC is 10 + Dexterity modifier +
    `base_armor` (0 unarmored, an armor item's AC less 10, plus a shield's 3 or 4). The tool call
    names the total instead: `ac` is recorded as it stands, with `base_armor` reported as the
    armor value behind it."""
    if ctx is None:
        raise InvalidInput("set_ac needs the tool context")
    target = (character or name).strip()
    if not target:
        raise InvalidInput("say whose Armor Class changes")
    if base_armor is None and ac is None:
        raise InvalidInput("give the base armor or the total Armor Class")
    base = None if base_armor is None else whole_number(base_armor, "base armor", 0, MAX_BASE_ARMOR)
    total = None if ac is None else whole_number(ac, "Armor Class", 5, 30)

    def action(hc: HookContext) -> dict[str, Any]:
        _, found = _character(hc, target)
        dexterity = ctx.world.character.modifier(found.stats, "dex")
        if base is not None:
            armor = 10 + dexterity + base
            armor_value = base
        else:
            armor = int(total)
            armor_value = armor - 10 - dexterity
        db, campaign_id = hc.session()
        record(db, campaign_id, ArmorSet(name=found.name, armor=armor))
        return {
            "character": found.name,
            "armor": armor,
            "base_armor": armor_value,
            "dexterity": dexterity,
        }

    return ctx.world.with_hook_context(action)


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
    result = ctx.world.rest(length, interrupted=interrupted, reason=reason)
    return {**result, **_rest_recovery(character, ctx, length)}


def recover_after_rest(character: str, ctx: ToolContext) -> dict[str, Any]:
    """Apply a night's recovery without advancing the clock, for a rest the GM tracked elsewhere.
    A night restores hit points equal to the character's level, one System Strain, every spell
    casting, and the Effort committed for a scene or a day."""
    return _rest_recovery(character, ctx, "night")


def _rest_recovery(character: str, ctx: ToolContext, length: str) -> dict[str, Any]:
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

        if length == "night":
            recover("hp", found.counters.get("level", 1), f"a {length}'s rest")
            recover("strain", 1, f"a {length}'s rest")
            recover("castings", found.harm.get("castings", 0), f"a {length}'s rest")
        durations = ("scene", "day") if length == "night" else ("scene",)
        marks = found.marks.get(EFFORT_MARK) or {}
        kept = {duration: dict(held) for duration, held in marks.items()}
        for duration in durations:
            for tradition, amount in (marks.get(duration) or {}).items():
                stat = tradition_effort_stat(str(tradition))
                if stat:
                    recover(stat, int(amount), f"{duration} Effort after a {length}'s rest")
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
            values = derived_stats(spec, found.stats, found.counters, found.skills)
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
    feature: Any,
    notes: Iterable[str],
    *,
    cause: str,
) -> None:
    """Add a feature to a character and note anything its effects only describe."""

    def action(hc: HookContext) -> None:
        _, found = _character(hc, character)
        db, campaign_id = hc.session()
        record(
            db,
            campaign_id,
            FeatureLearned(
                name=found.name,
                feature=feature.id,
                feature_name=feature.name,
                cause=cause,
            ),
        )
        for note in notes:
            if note:
                record(db, campaign_id, EffectNoted(name=found.name, text=note))

    ctx.world.with_hook_context(action)


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


def _armor_base(ctx: ToolContext, character: str, spec: CharacterSpec) -> int:
    """The armor item value behind a character's current AC, so an armor effect can add to it.
    A character whose AC has never been set is unarmored, so their base armor is 0 whatever
    their Dexterity."""

    def action(hc: HookContext) -> int:
        _, found = _character(hc, character)
        dexterity = spec.modifier(found.stats, "dex")
        armor = found.armor or 10 + dexterity
        return max(0, armor - 10 - dexterity)

    return ctx.world.with_hook_context(action)
