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
    PC_KIND,
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
    "high_mage_partial": "high",
    "necromancer": "necromancer",
    "necromancer_partial": "necromancer",
    "elementalist": "elementalist",
    "elementalist_partial": "elementalist",
}
TRADITION_EFFORT: Mapping[str, str] = {
    "high": "effort_high",
    "necromancer": "effort_necromancer",
    "elementalist": "effort_elemental",
}
# The levels at which a full or partial mage's class chart grants another art.
ART_LEVELS_FULL: Mapping[str, frozenset[int]] = {
    "high": frozenset({2, 4, 6, 8, 10}),
    "necromancer": frozenset({2, 4, 6, 8, 10}),
    "elementalist": frozenset({2, 4, 6, 8, 10}),
}
ART_LEVELS_PARTIAL: Mapping[str, frozenset[int]] = {
    "high": frozenset({2, 3, 6, 9}),
    "necromancer": frozenset({3, 6, 9}),
    "elementalist": frozenset({3, 6, 9}),
}


@dataclass(frozen=True)
class StartingPackage:
    """One of the book's starting equipment packages. Items are named as the equipment list
    names them, so `give_item` finds them in the rule set's catalogue. `base_armor` is the
    Armor Class bonus the package's armor and shield give over 10 + Dexterity."""

    id: str
    name: str
    items: tuple[str, ...]
    silver: int = 0
    base_armor: int = 0


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
            base_armor=5,
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
            base_armor=2,
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
            base_armor=3,
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
            base_armor=4,
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
            base_armor=3,
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


def roll_save(character: str, save: str, cause: str, ctx: ToolContext) -> dict[str, Any]:
    """Roll a saving throw: d20 against the character's Physical, Evasion, Mental, or Luck save,
    succeeding on a roll equal to or above the score. A failure only reports the result; what it
    costs is the GM's to apply."""
    return ctx.world.roll_save(character, _save_stat(save), cause)


def player_roll_save(
    character: str, save: str, cause: str, ctx: ToolContext, roll: int = 0
) -> dict[str, Any]:
    """Ask the player to roll a saving throw, and record their d20. Use it instead of `roll_save`
    whenever the save is for the player's own character; the player presses the roll button."""
    if not 1 <= roll <= 20:
        raise InvalidInput("the player's save roll must be 1 to 20")
    return ctx.world.roll_save(character, _save_stat(save), cause, natural=roll)


def roll_initiative(ctx: ToolContext) -> dict[str, Any]:
    """Roll once for each side of an encounter, adding the best Dexterity modifier in each, and
    set the order. Call it after the participants have joined, before the first round. Ties go
    to the party."""
    return ctx.world.roll_side_initiative()


def instinct_check(name: str, instinct: int, cause: str, ctx: ToolContext) -> dict[str, Any]:
    """Check an NPC's Instinct: roll 1d10 and compare it to the score in the statblock. A roll no
    higher than the Instinct means the creature acts on impulse. Player characters never check
    Instinct."""
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


def roll_tables(tables: list[str], ctx: ToolContext) -> dict[str, Any]:
    """Roll on one or more of the rule set's dice tables and log the results. The world-generation
    tables include significant_terrain, terrain_quirk, nation_disputes, ruling_class,
    government_struggle, historical_event, and the four tag tables; dungeon_reaction (2d6) gives
    an NPC's first reaction to the party."""
    return ctx.world.roll_tables(tables)


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
        _, found = _character(hc, character)
        if total <= 2:
            reaction = "as aggressively hostile as the situation allows"
        elif total <= 5:
            reaction = "more unfriendly and hostile than expected"
        elif total <= 8:
            reaction = "as hostile or friendly as they would usually be"
        elif total <= 11:
            reaction = "friendlier and more benign than expected"
        else:
            reaction = "as friendly and helpful as their nature and the situation permit"
        db, campaign_id = hc.session()
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


def attack(
    attacker: str,
    attack: str,
    target: str,
    ctx: ToolContext,
    modifier: str = "",
    targets: list[str] | None = None,
) -> dict[str, Any]:
    """Make an NPC's attack: d20 plus its attack bonus against the target's Armor Class, with
    natural 1 always missing and natural 20 always hitting. The engine rolls damage and Shock."""
    return ctx.world.attack(attacker, attack, target, modifier, targets)


def player_attack(
    target: str, weapon: str, ctx: ToolContext, modifier: str = "", roll: int = 0
) -> dict[str, Any]:
    """Record the player character's attack with a carried weapon, or unarmed, using the d20 the
    player rolled. The engine rolls damage; Shock applies on a miss."""
    if not 1 <= roll <= 20:
        raise InvalidInput("the player's attack roll must be 1 to 20")
    return ctx.world.player_attack(target, weapon, modifier, attack_roll=roll)


def resolve_attacks(target: str, attacks: list[dict[str, str]], ctx: ToolContext) -> dict[str, Any]:
    """Resolve several NPC attacks on one character at once, such as a monster's several attacks
    or several foes striking the same target. Each rolls to hit in turn."""
    return ctx.world.resolve_attacks(target, attacks)


def start_encounter(map: str, ctx: ToolContext) -> dict[str, Any]:
    """Start an encounter on a map. Add the participants with add_participant, then call
    roll_initiative before the first round."""
    return ctx.world.start_encounter(map)


def create_encounter_map(
    name: str,
    location: str,
    description: str,
    ctx: ToolContext,
    columns: int | None = None,
    rows: int | None = None,
    template: str = "",
) -> dict[str, Any]:
    """Make a grid map for an encounter at a location, such as the ford, a room, or a yard."""
    return ctx.world.create_encounter_map(name, location, columns, rows, description, template)


def update_encounter_map(
    name: str,
    columns: int,
    rows: int,
    description: str,
    ctx: ToolContext,
) -> dict[str, Any]:
    """Change an encounter map's description or size."""
    return ctx.world.update_encounter_map(name, columns, rows, description)


def delete_encounter_map(name: str, ctx: ToolContext) -> dict[str, Any]:
    """Delete an encounter map that is no longer needed."""
    return ctx.world.delete_encounter_map(name)


def place_participant(name: str, column: int, row: int, ctx: ToolContext) -> dict[str, Any]:
    """Put an encounter participant on a cell of the map grid."""
    return ctx.world.place_participant(name, column, row)


def end_encounter(ctx: ToolContext) -> dict[str, Any]:
    """End the encounter in progress."""
    return ctx.world.end_encounter()


def add_participant(name: str, ctx: ToolContext, side: str | None = None) -> dict[str, Any]:
    """Add a character to the encounter on a side, the party or the foes by default."""
    return ctx.world.add_participant(name, side)


def remove_participant(name: str, ctx: ToolContext) -> dict[str, Any]:
    """Take a character out of the encounter."""
    return ctx.world.remove_participant(name)


def end_npc_turn(name: str, ctx: ToolContext) -> dict[str, Any]:
    """End an NPC's turn once its actions are resolved."""
    return ctx.world.end_npc_turn(name)


def advance_time(ctx: ToolContext, days: int = 0, parts: int = 0, hours: int = 0) -> dict[str, Any]:
    """Move the campaign clock forward, such as the hour a mortally wounded character has left to
    live or the days of downtime an advancement needs."""
    return ctx.world.advance_time(days, parts, hours)


def give_item(
    name: str, item: str, ctx: ToolContext, size: str = "", damage: str = ""
) -> dict[str, Any]:
    """Give a character an item, naming a catalogue item or a new one with its size and damage."""
    return ctx.world.give_item(name, item, size or "normal", damage)


def drop_item(name: str, item: str, ctx: ToolContext) -> dict[str, Any]:
    """Remove an item from a character's inventory."""
    return ctx.world.drop_item(name, item)


def buy_item(
    name: str, item: str, ctx: ToolContext, price: int | None = None, size: str = ""
) -> dict[str, Any]:
    """Buy a catalogue item, or price an item the catalogue does not list."""
    return ctx.world.buy_item(name, item, price, size)


def search_marketplace(query: str, ctx: ToolContext) -> dict[str, Any]:
    """Find items and services the marketplace sells."""
    return ctx.world.search_marketplace(query)


def create_monster(
    name: str,
    location: str,
    ctx: ToolContext,
    description: str | None = None,
    template: str | None = None,
    stats: dict[str, int | None] | None = None,
    armor: int | None = None,
    attacks: list[dict[str, Any]] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Create a creature from the bestiary and place it at a location."""
    return ctx.world.create_monster(
        name, location, description, template, stats, armor, attacks, force
    )


def search_monster(query: str, ctx: ToolContext) -> dict[str, Any]:
    """Find a bestiary statblock by name."""
    return ctx.world.search_monster(query)


def harm_character(
    name: str, stat: str, amount: int, cause: str, ctx: ToolContext
) -> dict[str, Any]:
    """Harm a character outside combat, such as a fall, poison, or privation."""
    return ctx.world.harm_character(name, stat, amount, cause)


def stabilize(name: str, explanation: str, ctx: ToolContext) -> dict[str, Any]:
    """Stop a mortally wounded character's dying clock, leaving them Frail."""
    return ctx.world.stabilize(name, explanation)


def recover_character(
    name: str, explanation: str, consequence: str, ctx: ToolContext
) -> dict[str, Any]:
    """Recover a character who was mortally wounded, as magical healing does."""
    return ctx.world.recover_character(name, explanation, consequence)


def player_save(ctx: ToolContext, roll: int = 0) -> dict[str, Any]:
    """Pass the player's own d20 roll for a save the engine has marked due."""
    return ctx.world.player_save(roll)


def set_armor(name: str, armor: int, ctx: ToolContext) -> dict[str, Any]:
    """Set a character's total Armor Class."""
    return ctx.world.set_armor(name, armor)


def retreat(name: str, safe: bool, ctx: ToolContext) -> dict[str, Any]:
    """Take a character out of the fight, safely when they have a clear route."""
    return ctx.world.retreat(name, safe)


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


def level_up(character: str, ctx: ToolContext, rate: str = "fast") -> dict[str, Any]:
    """Raise a character one level, if their experience reaches the next threshold. Rolls the
    class hit dice for the new level's HP as the book says, sets the class attack bonus, adds
    three skill points (four for an Expert or partial Expert), grants any focus or art pick due,
    raises a spellcaster's castings, and recomputes the derived stats (saves, Strain, Effort)."""
    if rate not in ("fast", "slow"):
        raise InvalidInput("rate must be 'fast' or 'slow'")
    spec = ctx.world.character

    def read(hc: HookContext) -> dict[str, Any]:
        _, found = _character(hc, character)
        level = found.counters.get("level", 1)
        xp = found.counters.get("xp", 0)
        if level >= MAX_LEVEL:
            raise InvalidInput(f"{found.name} is already at the highest level, {MAX_LEVEL}")
        table_xp = WWN_XP_SLOW if rate == "slow" else WWN_XP
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
    # The book rerolls the class hit dice for the new level, adding Constitution to each die,
    # and keeps the old total plus one when the reroll is no better.
    rolled = roll_total(f"{new_level}d{table.hit_die}", ctx.world.rng)
    new_hp = rolled + new_level * (table.hp_constant + int(found["con"]))
    gained = max(1, new_hp)
    if gained <= int(found["hp"]):
        gained = int(found["hp"]) + 1
    ctx.world.set_stat_max(character, "hp", gained, cause=f"level {new_level}")
    ctx.world.set_stat_max(
        character, "attack_bonus", table.attack[new_level - 1], cause=f"level {new_level}"
    )
    points = 4 if table.quick_learner else 3
    ctx.world.adjust_counter(character, "skill_points", points)
    focus_picks = 1 if new_level in table.focus_levels else 0
    if focus_picks:
        ctx.world.adjust_counter(character, "focus_picks", focus_picks)
    art_picks = _art_picks(str(found["class_id"]), str(found["tradition"]), new_level)
    if art_picks:
        ctx.world.adjust_counter(character, "art_picks", art_picks)
    casting = casting_row(str(found["class_id"]), new_level)
    if casting is not None:
        ctx.world.set_stat_max(character, "castings", casting.casts, cause=f"level {new_level}")
    _recompute_derived(ctx, character, spec, cause=f"level {new_level}")
    return {
        "character": character,
        "level": new_level,
        "hp": gained,
        "attack_bonus": table.attack[new_level - 1],
        "skill_points": points,
        "focus_picks": focus_picks,
        "art_picks": art_picks,
        "castings": casting.casts if casting is not None else None,
        "rate": rate,
    }


def _art_picks(class_id: str, tradition: str, level: int) -> int:
    """The art picks a class chart grants at a level: a single arcane tradition uses the full
    chart, while an Adventurer's partial mage uses the partial chart. A dual mage counts as a
    partial mage for both traditions."""
    tag = tradition_tag(tradition)
    if not tag:
        return 0
    levels = ART_LEVELS_FULL if class_id.strip().casefold() == "mage" else ART_LEVELS_PARTIAL
    return 1 if level in levels.get(tag, frozenset()) else 0


def spend_skill_point(character: str, skill: str, ctx: ToolContext) -> dict[str, Any]:
    """Spend skill points to raise one skill by a level. The cost rises with the level, as the
    book's table says (1, 2, 3, 4, 5 points for levels 0 to 4), and a character must be high
    enough level for levels 2 and above (levels 3, 6, and 9)."""
    spec = ctx.world.character
    if skill not in spec.skills:
        options = ", ".join(sorted(spec.skills)) or "none"
        raise InvalidInput(f"there is no skill named {skill!r}; the skills are: {options}")

    def read(hc: HookContext) -> dict[str, int]:
        _, found = _character(hc, character)
        points = found.counters.get("skill_points", 0)
        before = spec.skill_level(found.skills, skill)
        if before >= MAX_SKILL_LEVEL:
            raise InvalidInput(
                f"{found.name}'s {spec.skills[skill].name} is already at level {MAX_SKILL_LEVEL}"
            )
        new_level = before + 1
        cost = new_level + 1
        needs_level = SKILL_LEVEL_MINIMUMS[new_level]
        level = found.counters.get("level", 1)
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
        return {"points": points, "before": before, "cost": cost}

    found = ctx.world.with_hook_context(read)
    new_level = int(found["before"]) + 1
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


# The minimum character level for each skill level, and the cost in skill points to buy it.
SKILL_LEVEL_MINIMUMS: Mapping[int, int] = {0: 1, 1: 1, 2: 3, 3: 6, 4: 9}
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
    _recompute_derived(ctx, character, spec, cause=f"the focus {feature.name}")
    return {
        "character": character,
        "focus": focus,
        "name": feature.name,
        "rank": feature.rank,
        "focus_picks": remaining["value"],
    }


# The feature tags that mark an art of one of the three traditions.
ART_TAGS = ("high", "necromancer", "elementalist")


def learn_art(character: str, art: str, ctx: ToolContext) -> dict[str, Any]:
    """Spend an art pick on an art of the character's own tradition. The class chart grants art
    picks at levels 2, 4, 6, 8, and 10 for a full mage, and at levels 2, 3, 6, and 9 for a partial
    mage (3, 6, and 9 for a partial Necromancer or Elementalist)."""
    spec = ctx.world.character
    feature = spec.features.get(art)
    if feature is None:
        raise InvalidInput(f"there is no feature named {art!r}")
    if feature.feature_type != "feat" or feature.tag not in ART_TAGS:
        raise InvalidInput(f"{feature.name} is not an art")

    def read(hc: HookContext) -> dict[str, Any]:
        _, found = _character(hc, character)
        tradition = found.templates.get("tradition", "")
        if not tradition:
            raise InvalidInput(f"{found.name} has no magical tradition, so cannot learn arts")
        if feature.tag != tradition_tag(tradition):
            raise InvalidInput(
                f"{feature.name} is an art of the {feature.tag} tradition, not "
                f"{found.name}'s {tradition_tag(tradition)}"
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
        tag = tradition_tag(tradition)
        if feature.tag not in {tag, "high"}:
            raise InvalidInput(
                f"{feature.name} is not a High Magic spell or a spell of {found.name}'s "
                f"tradition, {tag}"
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


def roll_starting_silver(character: str, ctx: ToolContext) -> dict[str, Any]:
    """Give a character the book's alternative to a starting package: 3d6 x 10 silver pieces."""
    amount = roll_total("3d6", ctx.world.rng) * 10
    ctx.world.change_currency(character, amount, cause="starting silver")
    return {"character": character, "silver": amount}


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
    set_ac(character, base_armor=found.base_armor, ctx=ctx)
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
    return {**result, **_rest_recovery(character, ctx, length, interrupted)}


def recover_after_rest(character: str, ctx: ToolContext) -> dict[str, Any]:
    """Apply a night's recovery without advancing the clock, for a rest the GM tracked elsewhere.
    A night restores hit points equal to the character's level, one System Strain, every spell
    casting, and the Effort committed for a scene or a day."""
    return _rest_recovery(character, ctx, "night", False)


def _rest_recovery(
    character: str, ctx: ToolContext, length: str, interrupted: bool = False
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

        if length == "night" and not interrupted:
            if "frail" not in found.conditions:
                recover("hp", found.counters.get("level", 1), f"a {length}'s rest")
                recover("strain", 1, f"a {length}'s rest")
            recover("castings", found.harm.get("castings", 0), f"a {length}'s rest")
        durations = ("scene", "day") if length == "night" and not interrupted else ("scene",)
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
            values = derived_stats(spec, found.stats, found.counters, found.skills, found.templates)
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
