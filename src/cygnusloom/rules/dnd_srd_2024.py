"""Python side of the D&D 5.5e (SRD 5.2.1) rule set: checks, saves, spells, and advancement.

The SRD uses a d20 for everything: an attack, check, or save is a d20 plus an ability modifier,
plus the proficiency bonus when the character is proficient. The engine's skills store that
proficiency bonus as their level, so the generic check and attack paths already add it. Saves
need the DC the effect sets, which the generic save compares against a stat, so this module
rolls them itself. Spell slots, hit dice, death saves, and the class features gained at each
level are also handled here from the validated ``progression`` rule data.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from types import MappingProxyType
from typing import Any, Protocol

from cygnusloom.content.character import CharacterSpec, Feature
from cygnusloom.content.definitions import ContentError
from cygnusloom.content.library import LevelGrants
from cygnusloom.rules.context import ToolContext
from cygnusloom.rules.dice import ROLL_MODES, is_formula, roll_mode_values, roll_total
from cygnusloom.services.common import InvalidInput, whole_number
from cygnusloom.services.conditions import condition_modifier, condition_roll_mode
from cygnusloom.services.events import (
    PC_KIND,
    CharacterDied,
    CharacterRecovered,
    CharacterSaved,
    CharacterStabilized,
    CounterChanged,
    EffectNoted,
    EncounterAttacked,
    FeatureLearned,
    InitiativeSettled,
    SpellAttackMade,
    current,
    name_key,
    record,
    same_item_name,
)
from cygnusloom.services.hooks import HookContext

ABILITY_ORDER = ("str", "dex", "con", "int", "wis", "cha")
ABILITY_NAMES = {
    "str": "Strength",
    "dex": "Dexterity",
    "con": "Constitution",
    "int": "Intelligence",
    "wis": "Wisdom",
    "cha": "Charisma",
}
CASTING_KINDS = ("none", "full", "half", "pact")
SLOT_TABLES = ("none", "full", "half", "pact")
DEFAULT_DC = 15
# Character marks: the leveled spells prepared for the day, the always-prepared spells a
# subclass grants, and the concentration spell being maintained.
PREPARED_MARK = "prepared"
ALWAYS_PREPARED_MARK = "always_prepared"
CONCENTRATION_MARK = "concentration"
# The Magic Initiate spell cast for free since the last long rest.
MAGIC_INITIATE_MARK = "magic_initiate_free"
# Lineage and similar spells cast without a slot since the last long rest.
FREE_CAST_MARK = "free_cast"
# The magic items a character is attuned to, at most three.
ATTUNED_MARK = "attuned"
# The land a Circle of the Land druid chose for the current set of Circle spells.
LAND_MARK = "land"
# The counters the use_class_resource tool may spend.
RESOURCE_COUNTERS = frozenset(
    {
        "rage",
        "bardic_inspiration",
        "channel_divinity",
        "wild_shape",
        "second_wind",
        "action_surge",
        "indomitable",
        "focus_points",
        "lay_on_hands",
        "sorcery_points",
        "arcane_recovery",
        "favored_enemy",
        "tireless",
        "mystic_arcanum",
        "dark_ones_luck",
        "giant_ancestry",
    }
)


class _WorldWithRuleData(Protocol):
    @property
    def rule_data(self) -> object: ...


@dataclass(frozen=True)
class DndClass:
    """One class's fixed progression: hit points, saves, spellcasting, and the features and
    choices it gains at each level."""

    id: str
    hit_die: int
    saves: tuple[str, ...]
    casting: str
    ability: str
    subclass_level: int
    asi_levels: frozenset[int]
    fighting_style_levels: frozenset[int]
    expertise_levels: frozenset[int]
    cantrips: tuple[int, ...]
    spells: tuple[int, ...]
    slot_table: str
    features: Mapping[int, tuple[str, ...]]
    # Each resource counter's maximum at every level.
    resources: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    # The levels that grant Metamagic options, and how many each grants.
    metamagic_levels: frozenset[int] = frozenset()
    metamagic_counts: tuple[int, ...] = ()
    # Eldritch Invocation slots at every level.
    invocations: tuple[int, ...] = ()
    # Spells the class always has prepared, by the level that grants them.
    always_prepared: Mapping[int, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class DndSubclass:
    """One subclass's feature ids by the class level that grants them, and the spells it always
    prepares."""

    id: str
    class_id: str
    features: Mapping[int, tuple[str, ...]]
    spells: Mapping[int, tuple[str, ...]] = field(default_factory=dict)
    always_prepared: bool = False
    # Circle of the Land spell tables, keyed by land name and then class level.
    land_spells: Mapping[str, Mapping[int, tuple[str, ...]]] = field(default_factory=dict)


@dataclass(frozen=True)
class DndProgression:
    """The rule set's parsed class and spell tables."""

    max_level: int
    xp: Mapping[str, tuple[int, ...]]
    classes: Mapping[str, DndClass]
    subclasses: Mapping[str, DndSubclass]
    spell_lists: Mapping[str, Mapping[int, tuple[str, ...]]]
    slots: Mapping[str, tuple[tuple[int, ...], ...]]
    pact_slots: tuple[tuple[int, int], ...]


def configure_progression(
    data: Mapping[str, Any], character: CharacterSpec, label: str
) -> DndProgression:
    """Validate the rule set's ``progression`` data against its character definitions."""
    if not isinstance(character, CharacterSpec):
        raise ContentError(f"{label}: progression configurator needs a CharacterSpec")
    progression = parse_progression(data, label)
    for class_id, rules in progression.classes.items():
        template = character.templates.get(class_id)
        if template is None or template.kind != "class":
            raise ContentError(f"{label}: class {class_id!r} has no class template of the same id")
        for feature_id in _every_feature(rules, progression.subclasses):
            feature = character.features.get(feature_id)
            if feature is None:
                raise ContentError(f"{label}: class {class_id!r} names no feature {feature_id!r}")
        for spell_id in _class_spells(progression, class_id):
            spell = character.features.get(spell_id)
            if spell is None or spell.feature_type != "spell":
                raise ContentError(
                    f"{label}: class {class_id!r} spell list names no spell {spell_id!r}"
                )
        for resource in rules.resources:
            if resource not in character.counters:
                raise ContentError(f"{label}: class {class_id!r} names no counter {resource!r}")
    for subclass_id, subclass in progression.subclasses.items():
        if subclass.class_id not in progression.classes:
            raise ContentError(
                f"{label}: subclass {subclass_id!r} names no class {subclass.class_id!r}"
            )
        for spell_ids in subclass.spells.values():
            for spell_id in spell_ids:
                spell = character.features.get(spell_id)
                if spell is None or spell.feature_type != "spell":
                    raise ContentError(
                        f"{label}: subclass {subclass_id!r} names no spell {spell_id!r}"
                    )
        for land_levels in subclass.land_spells.values():
            for spell_ids in land_levels.values():
                for spell_id in spell_ids:
                    spell = character.features.get(spell_id)
                    if spell is None or spell.feature_type != "spell":
                        raise ContentError(
                            f"{label}: subclass {subclass_id!r} names no spell {spell_id!r}"
                        )
    return progression


def parse_progression(data: Mapping[str, Any], label: str = "progression") -> DndProgression:
    """Parse and check the progression mapping. Pure: no file access and no global state."""
    if not isinstance(data, Mapping):
        raise ContentError(f"{label}: must be an object")
    expected = {
        "type",
        "id",
        "python_module",
        "configure",
        "max_level",
        "xp",
        "classes",
        "subclasses",
        "spell_lists",
        "slots",
        "pact_slots",
    }
    unknown = sorted(set(data) - expected)
    missing = sorted(expected - set(data))
    if unknown:
        raise ContentError(f"{label}: unexpected fields {', '.join(unknown)}")
    if missing:
        raise ContentError(f"{label}: missing fields {', '.join(missing)}")
    if data["type"] != "rule_data" or data["id"] != "progression":
        raise ContentError(f"{label}: must be the rule_data definition 'progression'")
    max_level = _whole(data["max_level"], f"{label}.max_level", 1, 20)
    xp = _xp_tracks(data["xp"], max_level, f"{label}.xp")
    slots = _slot_tables(data["slots"], max_level, f"{label}.slots")
    pact = _pact_table(data["pact_slots"], max_level, f"{label}.pact_slots")
    classes_data = _object(data["classes"], f"{label}.classes")
    classes: dict[str, DndClass] = {}
    for class_id, raw in classes_data.items():
        class_label = f"{label}.classes[{class_id!r}]"
        entry = _object(raw, class_label)
        _keys(
            entry,
            {
                "hit_die",
                "saves",
                "casting",
                "ability",
                "subclass_level",
                "asi_levels",
                "fighting_style_levels",
                "expertise_levels",
                "cantrips",
                "spells",
                "slot_table",
                "features",
                "resources",
                "metamagic_levels",
                "metamagic_counts",
                "invocations",
                "always_prepared",
            },
            class_label,
        )
        casting = _text(entry["casting"], f"{class_label}.casting")
        if casting not in CASTING_KINDS:
            raise ContentError(f"{class_label}.casting: must be one of {CASTING_KINDS}")
        ability = _text(entry["ability"], f"{class_label}.ability", allow_empty=True)
        if ability and ability not in ABILITY_ORDER:
            raise ContentError(f"{class_label}.ability: names no ability {ability!r}")
        slot_table = _text(entry["slot_table"], f"{class_label}.slot_table")
        if slot_table not in slots and slot_table != "pact":
            raise ContentError(f"{class_label}.slot_table: names no slot table {slot_table!r}")
        if (casting == "none") != (slot_table == "none"):
            raise ContentError(
                f"{class_label}: casting {casting!r} and slot table {slot_table!r} disagree"
            )
        if slot_table == "pact" and casting != "pact":
            raise ContentError(f"{class_label}: the pact slot table needs casting 'pact'")
        saves = _texts(entry["saves"], f"{class_label}.saves")
        if len(saves) != 2 or any(save not in ABILITY_ORDER for save in saves):
            raise ContentError(f"{class_label}.saves: must name exactly two abilities")
        classes[class_id] = DndClass(
            id=class_id,
            hit_die=_whole(entry["hit_die"], f"{class_label}.hit_die", 1, 20),
            saves=saves,
            casting=casting,
            ability=ability,
            subclass_level=_whole(
                entry["subclass_level"], f"{class_label}.subclass_level", 1, max_level
            ),
            asi_levels=_levels(entry["asi_levels"], max_level, f"{class_label}.asi_levels"),
            fighting_style_levels=_levels(
                entry["fighting_style_levels"],
                max_level,
                f"{class_label}.fighting_style_levels",
            ),
            expertise_levels=_levels(
                entry["expertise_levels"], max_level, f"{class_label}.expertise_levels"
            ),
            cantrips=_per_level(entry["cantrips"], max_level, f"{class_label}.cantrips"),
            spells=_per_level(entry["spells"], max_level, f"{class_label}.spells"),
            slot_table=slot_table,
            features=_feature_levels(entry["features"], max_level, f"{class_label}.features"),
            resources={
                resource: _resource_levels(values, max_level, f"{class_label}.resources.{resource}")
                for resource, values in _object(
                    entry.get("resources", {}), f"{class_label}.resources"
                ).items()
            },
            metamagic_levels=_levels(
                entry.get("metamagic_levels", []), max_level, f"{class_label}.metamagic_levels"
            ),
            metamagic_counts=tuple(
                _whole(value, f"{class_label}.metamagic_counts", 0, 20)
                for value in entry.get("metamagic_counts", [])
            ),
            invocations=_per_level(
                entry.get("invocations", [0] * max_level),
                max_level,
                f"{class_label}.invocations",
            ),
            always_prepared={
                int(level): _texts(values, f"{class_label}.always_prepared[{level!r}]")
                for level, values in _object(
                    entry.get("always_prepared", {}), f"{class_label}.always_prepared"
                ).items()
            },
        )
    subclasses_data = _object(data["subclasses"], f"{label}.subclasses")
    subclasses: dict[str, DndSubclass] = {}
    for subclass_id, raw in subclasses_data.items():
        subclass_label = f"{label}.subclasses[{subclass_id!r}]"
        entry = _object(raw, subclass_label)
        _keys(
            entry,
            {"class", "features", "spells", "always_prepared", "land_spells"},
            subclass_label,
        )
        subclass_spells: dict[int, tuple[str, ...]] = {}
        for level_key, values in _object(
            entry.get("spells", {}), f"{subclass_label}.spells"
        ).items():
            try:
                number = int(level_key)
            except (TypeError, ValueError):
                raise ContentError(
                    f"{subclass_label}.spells: level keys must be whole numbers"
                ) from None
            subclass_spells[_whole(number, f"{subclass_label}.spells level", 1, max_level)] = (
                _texts(values, f"{subclass_label}.spells[{level_key}]")
            )
        always = entry.get("always_prepared", False)
        if not isinstance(always, bool):
            raise ContentError(f"{subclass_label}.always_prepared must be true or false")
        land_spells: dict[str, Mapping[int, tuple[str, ...]]] = {}
        for land, raw_levels in _object(
            entry.get("land_spells", {}), f"{subclass_label}.land_spells"
        ).items():
            land_levels: dict[int, tuple[str, ...]] = {}
            for level_key, values in _object(
                raw_levels, f"{subclass_label}.land_spells[{land!r}]"
            ).items():
                try:
                    number = int(level_key)
                except (TypeError, ValueError):
                    raise ContentError(
                        f"{subclass_label}.land_spells: level keys must be whole numbers"
                    ) from None
                land_levels[_whole(number, f"{subclass_label}.land_spells level", 1, max_level)] = (
                    _texts(values, f"{subclass_label}.land_spells[{land!r}][{level_key}]")
                )
            land_spells[land] = MappingProxyType(land_levels)
        subclasses[subclass_id] = DndSubclass(
            id=subclass_id,
            class_id=_text(entry["class"], f"{subclass_label}.class"),
            features=_feature_levels(entry["features"], max_level, f"{subclass_label}.features"),
            spells=MappingProxyType(subclass_spells),
            always_prepared=always,
            land_spells=MappingProxyType(land_spells),
        )
    spell_lists_data = _object(data["spell_lists"], f"{label}.spell_lists")
    spell_lists: dict[str, Mapping[int, tuple[str, ...]]] = {}
    for class_id, raw in spell_lists_data.items():
        list_label = f"{label}.spell_lists[{class_id!r}]"
        entry = _object(raw, list_label)
        by_rank: dict[int, tuple[str, ...]] = {}
        for rank, values in entry.items():
            try:
                number = int(rank)
            except (TypeError, ValueError):
                raise ContentError(f"{list_label}: rank keys must be whole numbers") from None
            number = _whole(number, f"{list_label} rank", 0, 9)
            by_rank[number] = _texts(values, f"{list_label}[{rank}]")
        spell_lists[class_id] = MappingProxyType(by_rank)
    return DndProgression(
        max_level=max_level,
        xp=MappingProxyType(xp),
        classes=MappingProxyType(classes),
        subclasses=MappingProxyType(subclasses),
        spell_lists=MappingProxyType(spell_lists),
        slots=MappingProxyType(slots),
        pact_slots=pact,
    )


def require_progression(world: _WorldWithRuleData) -> DndProgression:
    """The parsed progression the rule set configured, from a world or level-up context."""
    rule_data = getattr(world, "rule_data", None)
    if not isinstance(rule_data, Mapping) or "progression" not in rule_data:
        raise InvalidInput("this rule set has no D&D progression data")
    progression = rule_data["progression"]
    if not isinstance(progression, DndProgression):
        raise InvalidInput("the rule set's progression data was not configured")
    return progression


# --- character facts -------------------------------------------------------------------


def ability_modifier(score: int) -> int:
    """The SRD ability modifier: -5 at 1, 0 at 10-11, +1 at 12-13, and so on."""
    return (score - 10) // 2


def proficiency_bonus(level: int) -> int:
    """The proficiency bonus at a character level: +2 from 1 to 4, then +1 every four levels."""
    return 2 + (level - 1) // 4


def class_id(found: Any) -> str:
    """The character's class template id, or "" when it has none."""
    template = found.templates.get("class", "")
    return str(template).strip().casefold()


def subclass_id(found: Any, spec: CharacterSpec, progression: DndProgression) -> str:
    """The subclass marker the character chose, or "" before level 3."""
    for feature_id in found.features:
        name = feature_id.removeprefix("subclass-")
        if name in progression.subclasses:
            return name
    return ""


def _class_rules(found: Any, progression: DndProgression) -> DndClass:
    found_class = class_id(found)
    rules = progression.classes.get(found_class)
    if rules is None:
        raise InvalidInput(
            f"{found.name} has no class; the classes are: {', '.join(sorted(progression.classes))}"
        )
    return rules


def _level(found: Any) -> int:
    level = found.counters.get("level", 1)
    if not isinstance(level, int) or level < 1:
        raise InvalidInput(f"{found.name} has an invalid level {level!r}")
    return level


def _stored_level(found: Any) -> int:
    try:
        return _level(found)
    except InvalidInput:
        return 1


def _modifier(found: Any, spec: CharacterSpec, stat: str) -> int:
    if stat not in found.stats:
        return 0
    return found.modifier(spec, stat)


def _save_bonus(found: Any, spec: CharacterSpec, progression: DndProgression, stat: str) -> int:
    """An ability save: a statblock's printed save bonus, or the modifier plus the proficiency
    bonus when the class is proficient."""
    if found.kind != PC_KIND:
        printed = found.stats.get(f"save_{stat}")
        if printed is not None:
            # Statblock scores are stored non-negative, so a save bonus is recorded as 11 + the
            # printed bonus.
            return int(printed) - 11
    bonus = _modifier(found, spec, stat)
    if found.kind == PC_KIND:
        rules = progression.classes.get(class_id(found))
        if rules is not None and stat in rules.saves:
            bonus += proficiency_bonus(_level(found))
    return bonus


def _resource_targets(found: Any, rules: DndClass, level: int) -> dict[str, int]:
    """Each class resource's full value at a level, with the pools that follow a score or the
    level itself filled in."""
    targets: dict[str, int] = {}
    for counter, values in rules.resources.items():
        target = values[level - 1] if 0 < level <= len(values) else 0
        if counter == "bardic_inspiration":
            target = max(1, (int(found.stats.get("cha", 10)) - 10) // 2)
        elif counter == "lay_on_hands":
            target = 5 * level
        targets[counter] = target
    return targets


def _spell_max_rank(found: Any, progression: DndProgression) -> int:
    """The highest spell rank a character can cast at their level and class."""
    rules = _class_rules(found, progression)
    level = max(1, min(_level(found), progression.max_level))
    if rules.slot_table == "pact":
        return progression.pact_slots[level - 1][1]
    table = progression.slots.get(rules.slot_table, ())
    if not table:
        return 0
    row = table[level - 1]
    return max((index for index, count in enumerate(row, start=1) if count), default=0)


def _slot_counts(found: Any, progression: DndProgression) -> dict[int, int]:
    """The spell slots a character has at its level, by spell level, plus pact slots."""
    rules = _class_rules(found, progression)
    level = max(1, min(_level(found), progression.max_level))
    if rules.slot_table == "pact":
        count, slot_level = progression.pact_slots[level - 1]
        return {slot_level: count}
    table = progression.slots.get(rules.slot_table, ())
    if not table:
        return {}
    return {index: count for index, count in enumerate(table[level - 1], start=1) if count > 0}


def _class_spells(progression: DndProgression, class_id: str) -> tuple[str, ...]:
    ranks = progression.spell_lists.get(class_id, {})
    return tuple(spell for spells in ranks.values() for spell in spells)


def _every_feature(rules: DndClass, subclasses: Mapping[str, DndSubclass]) -> tuple[str, ...]:
    found = [feature for features in rules.features.values() for feature in features]
    for subclass in subclasses.values():
        if subclass.class_id == rules.id:
            found.extend(feature for features in subclass.features.values() for feature in features)
    return tuple(found)


# --- level-up ----------------------------------------------------------------------------


def level_up_summary(character: str, ctx: ToolContext) -> dict[str, Any]:
    """What the next level grants, before it is taken."""
    granted = _project(character, ctx)
    return {"character": character, **granted.as_summary()}


def project_level(character: str, ctx: ToolContext) -> LevelGrants:
    """The same projection, for callers that want the typed grants."""
    return _project(character, ctx)


def _project(character: str, ctx: ToolContext) -> LevelGrants:
    progression = require_progression(ctx.world)
    spec = ctx.world.character

    def read(hc: HookContext) -> dict[str, Any]:
        found = hc.character(character)
        level = _level(found)
        if level >= progression.max_level:
            raise InvalidInput(
                f"{found.name} is already at the highest level, {progression.max_level}"
            )
        rules = _class_rules(found, progression)
        rate = _xp_rate(ctx, progression)
        track = progression.xp[rate]
        threshold = track[level]
        xp = found.counters.get("xp", 0)
        if xp < threshold:
            raise InvalidInput(
                f"{found.name} has {xp} experience; level {level + 1} needs {threshold}"
            )
        return {
            "name": found.name,
            "level": level,
            "xp": xp,
            "threshold": threshold,
            "rate": rate,
            "rules": rules,
            "con": _modifier(found, spec, "con"),
            "hp": found.stats.get("hp", 0),
            "has_subclass": bool(subclass_id(found, spec, progression)),
            "level_hp_bonus": sum(
                spec.features[feature_id].level_hp_bonus
                for feature_id in found.features
                if feature_id in spec.features
                and (
                    spec.features[feature_id].rank == 0
                    or level + 1 > spec.features[feature_id].rank
                )
            ),
        }

    facts = ctx.world.with_hook_context(read)
    level = int(facts["level"])
    new_level = level + 1
    rules: DndClass = facts["rules"]
    pools = {
        # A wizard's spellbook grows by two spells a level, separate from the prepared limit.
        "spell_picks": 2
        if rules.id == "wizard"
        else max(0, rules.spells[new_level - 1] - rules.spells[level - 1]),
        "cantrip_picks": max(0, rules.cantrips[new_level - 1] - rules.cantrips[level - 1]),
        "asi_picks": 1 if new_level in rules.asi_levels else 0,
        "fighting_style_picks": 1 if new_level in rules.fighting_style_levels else 0,
        "expertise_picks": 2 if new_level in rules.expertise_levels else 0,
        "subclass_picks": 1
        if new_level == rules.subclass_level and not facts["has_subclass"]
        else 0,
    }
    old_pb = proficiency_bonus(level)
    new_pb = proficiency_bonus(new_level)
    hp_gain = max(1, rules.hit_die // 2 + 1 + int(facts["con"]))
    # Features such as Dwarven Toughness add to every level. A feature gained at this level
    # also adds its one-time bonus; its per-level bonus starts on the next level.
    hp_gain += int(facts["level_hp_bonus"])
    values = {
        "hp_gain": hp_gain,
        "hit_die": rules.hit_die,
        "proficiency_bonus": new_pb,
        "features": tuple(rules.features.get(new_level, ())),
    }
    con = int(facts["con"])
    lines = [
        (
            f"Hit points: +{hp_gain} (d{rules.hit_die} average {rules.hit_die // 2 + 1} "
            f"{'+' if con >= 0 else '-'} {abs(con)} Constitution)."
        ),
        f"Proficiency bonus: +{new_pb}.",
    ]
    if new_pb > old_pb:
        lines.append("Every proficient skill rises with the proficiency bonus.")
    if pools["spell_picks"]:
        lines.append(f"Spells learned: {pools['spell_picks']}.")
    if pools["cantrip_picks"]:
        lines.append(f"Cantrips learned: {pools['cantrip_picks']}.")
    if pools["asi_picks"]:
        lines.append("Ability Score Improvement: choose +1 to two abilities, or +2 to one.")
    if pools["fighting_style_picks"]:
        lines.append("Fighting Style: choose a fighting style feat.")
    if pools["expertise_picks"]:
        lines.append("Expertise: choose two proficient skills to double their proficiency.")
    if pools["subclass_picks"]:
        lines.append("Choose a subclass.")
    if values["features"]:
        names = [
            ctx.world.character.features[f].name
            for f in values["features"]
            if f in ctx.world.character.features
        ]
        if names:
            lines.append(f"Class features: {', '.join(names)}.")
    return LevelGrants(
        level=level,
        new_level=new_level,
        xp=int(facts["xp"]),
        threshold=int(facts["threshold"]),
        rate=str(facts["rate"]),
        pools=pools,
        values=values,
        lines=tuple(lines),
    )


def apply_level_up(character: str, ctx: ToolContext) -> dict[str, Any]:
    """Raise the character one level and grant everything the level gives automatically.

    The player's choices (spells, ASI, subclass, fighting style, expertise) are spent on the
    advancement page through the pick hooks below.
    """
    progression = require_progression(ctx.world)
    spec = ctx.world.character
    grants = _project(character, ctx)
    new_level = grants.new_level
    found = _read_character(character, ctx)
    rules = _class_rules(found, progression)
    old_level = grants.level
    ctx.world.adjust_counter(character, "level", 1)
    ctx.world.set_stat_max(
        character,
        "hp",
        int(found.stats.get("hp", 0)) + int(grants.values["hp_gain"]),
        cause=f"level {new_level}",
    )
    _set_slots(ctx, character, progression, rules, new_level, refill=True)
    ctx.world.adjust_counter(character, "hit_dice", 1)
    new_pb = int(grants.values["proficiency_bonus"])
    old_pb = proficiency_bonus(old_level)
    ctx.world.set_stat_max(character, "attack_bonus", new_pb, cause=f"level {new_level}")
    if new_pb > old_pb:
        _raise_proficient_skills(ctx, character, spec, old_pb, new_pb)
    for counter in (
        "spell_picks",
        "cantrip_picks",
        "asi_picks",
        "fighting_style_picks",
        "subclass_picks",
        "expertise_picks",
    ):
        amount = grants.granted(counter)
        if amount:
            ctx.world.adjust_counter(character, counter, amount)
    newly_learned = list(grants.values["features"])
    for feature_id in grants.values["features"]:
        _learn(ctx, character, feature_id, cause=f"level {new_level}")
    species = found.templates.get("species")
    if species:
        template = spec.templates.get(species)
        if template is not None:
            for granted in template.features:
                feature = spec.features.get(granted.feature)
                if (
                    feature is not None
                    and feature.min_level == new_level
                    and (not feature.requires_feature or feature.requires_feature in found.features)
                ):
                    _learn(ctx, character, feature.id, cause=f"species level {new_level}")
                    newly_learned.append(feature.id)
    # A feature gained at this level may add a one-time hit-point bonus.
    for feature_id in grants.values["features"]:
        feature = spec.features.get(feature_id)
        if feature is not None and feature.rank == new_level and feature.flat_hp_bonus:
            ctx.world.set_stat_max(
                character,
                "hp",
                int(_read_character(character, ctx).stats.get("hp", 0)) + feature.flat_hp_bonus,
                cause=f"{feature.name} at level {new_level}",
            )
    for counter, target in _resource_targets(found, rules, new_level).items():
        current = int(found.counters.get(counter, 0))
        if target != current:
            ctx.world.adjust_counter(character, counter, target - current)
    if new_level in rules.metamagic_levels:
        order = sorted(rules.metamagic_levels)
        index = order.index(new_level)
        count = rules.metamagic_counts[index] if index < len(rules.metamagic_counts) else 0
        if count:
            ctx.world.adjust_counter(character, "metamagic_picks", count)
    if "giant_ancestry" in found.counters:
        current = int(found.counters.get("giant_ancestry", 0))
        target = proficiency_bonus(new_level)
        if target != current:
            ctx.world.adjust_counter(character, "giant_ancestry", target - current)
    if rules.invocations:
        gained = rules.invocations[new_level - 1] - rules.invocations[old_level - 1]
        if gained > 0:
            ctx.world.adjust_counter(character, "invocation_picks", gained)
    chosen = subclass_id(found, spec, progression)
    if chosen:
        subclass = progression.subclasses[chosen]
        for feature_id in subclass.features.get(new_level, ()):
            _learn(ctx, character, feature_id, cause=f"{subclass.id} level {new_level}")
            newly_learned.append(feature_id)
            feature = spec.features.get(feature_id)
            if feature is not None and feature.flat_hp_bonus:
                ctx.world.set_stat_max(
                    character,
                    "hp",
                    int(_read_character(character, ctx).stats.get("hp", 0)) + feature.flat_hp_bonus,
                    cause=f"{feature.name} at level {new_level}",
                )
        _learn_always_prepared(ctx, character, subclass, new_level)
    return {
        "character": character,
        "level": new_level,
        "old_level": old_level,
        "hp": int(found.stats.get("hp", 0)) + int(grants.values["hp_gain"]),
        "proficiency_bonus": new_pb,
        "rate": grants.rate,
        "grants": dict(grants.pools),
        "features": newly_learned,
    }


def _raise_proficient_skills(
    ctx: ToolContext, character: str, spec: CharacterSpec, old_pb: int, new_pb: int
) -> None:
    """Raise every proficient skill with the proficiency bonus, and expertise skills twice."""
    found = _read_character(character, ctx)
    for skill_id in spec.skills:
        level = spec.skill_level(found.skills, skill_id)
        if level < old_pb:
            continue
        amount = (new_pb - old_pb) * (2 if level >= old_pb * 2 else 1)
        ctx.world.raise_skill(character, skill_id, level + amount, cause="proficiency bonus")


def _set_slots(
    ctx: ToolContext,
    character: str,
    progression: DndProgression,
    rules: DndClass,
    level: int,
    *,
    refill: bool,
) -> None:
    """Set the character's spell-slot counters to the class table's values for a level."""
    found = _read_character(character, ctx)
    targets = {index: 0 for index in range(1, 10)}
    if rules.slot_table == "pact":
        count, slot_level = progression.pact_slots[level - 1]
        targets[slot_level] = count
    elif rules.slot_table != "none":
        table = progression.slots[rules.slot_table]
        for index, count in enumerate(table[level - 1], start=1):
            targets[index] = count
    for index in range(1, 10):
        counter = f"slot_{index}"
        if counter not in prog_counter_ids(ctx):
            continue
        current_value = found.counters.get(counter, 0)
        target = targets[index]
        # A level-up refills slots the character has not spent; a rest refills all of them.
        if not refill and current_value > target:
            target = current_value
        if target != current_value:
            ctx.world.adjust_counter(character, counter, target - current_value)


def prog_counter_ids(ctx: ToolContext) -> set[str]:
    """The counters the character spec defines, which are the ones a tool may adjust."""
    return set(ctx.world.character.counters)


def _xp_rate(ctx: ToolContext, progression: DndProgression) -> str:
    chosen = str(ctx.world.campaign_options().get("xp_rate", ""))
    if chosen and chosen in progression.xp:
        return chosen
    return next(iter(progression.xp))


# --- level-up picks ----------------------------------------------------------------------


def spell_options(
    character: str, pick: Any, ctx: ToolContext, level: int | None = None
) -> list[dict[str, Any]]:
    """The spells or cantrips the character can learn now from the class list."""
    progression = require_progression(ctx.world)
    spec = ctx.world.character
    found = _read_character(character, ctx)
    rules = _class_rules(found, progression)
    target_level = level or _stored_level(found)
    max_rank = _max_rank_for(rules, progression, target_level)
    cantrips = pick.id == "cantrips"
    known = {feature_id for feature_id in found.features}
    options = []
    for rank, spell_ids in progression.spell_lists.get(rules.id, {}).items():
        if cantrips and rank != 0:
            continue
        if not cantrips and rank == 0:
            continue
        if not cantrips and rank > max_rank:
            continue
        for spell_id in spell_ids:
            feature = spec.features.get(spell_id)
            if feature is None or spell_id in known:
                continue
            options.append(_option(feature))
    return sorted(options, key=lambda option: (option["rank"], option["name"]))


def _max_rank_for(rules: DndClass, progression: DndProgression, level: int) -> int:
    level = max(1, min(level, progression.max_level))
    if rules.slot_table == "pact":
        return progression.pact_slots[level - 1][1]
    if rules.slot_table == "none":
        return 0
    row = progression.slots[rules.slot_table][level - 1]
    return max((index for index, count in enumerate(row, start=1) if count), default=0)


def feat_options(
    character: str, pick: Any, ctx: ToolContext, level: int | None = None
) -> list[dict[str, Any]]:
    """The Ability Score Improvement feat and the general or epic feats open at this level."""
    require_progression(ctx.world)
    spec = ctx.world.character
    found = _read_character(character, ctx)
    target_level = level or _stored_level(found)
    options = []
    for feature in spec.features.values():
        if feature.feature_type != "feat":
            continue
        if feature.tag not in ("general", "epic-boon"):
            continue
        if target_level < feature.rank:
            continue
        if feature.id in found.features and not feature.repeatable:
            continue
        options.append(_option(feature))
    return sorted(options, key=lambda option: (option["rank"], option["name"]))


def fighting_style_options(
    character: str, pick: Any, ctx: ToolContext, level: int | None = None
) -> list[dict[str, Any]]:
    """The fighting style feats the character does not already know."""
    spec = ctx.world.character
    found = _read_character(character, ctx)
    return [
        _option(feature)
        for feature in spec.features.values()
        if feature.feature_type == "feat"
        and feature.tag == "fighting-style"
        and feature.id not in found.features
    ]


def _tagged_feat_options(
    character: str, ctx: ToolContext, tag: str, level: int | None = None
) -> list[dict[str, Any]]:
    """The feat options with a tag that the character does not already know and whose
    prerequisite level is open."""
    spec = ctx.world.character
    found = _read_character(character, ctx)
    target_level = level or _stored_level(found)
    return sorted(
        (
            _option(feature)
            for feature in spec.features.values()
            if feature.feature_type == "feat"
            and feature.tag == tag
            and feature.rank <= target_level
            and (feature.id not in found.features or feature.repeatable)
        ),
        key=lambda option: (option["rank"], option["name"]),
    )


def metamagic_options(
    character: str, pick: Any, ctx: ToolContext, level: int | None = None
) -> list[dict[str, Any]]:
    """The Metamagic options the sorcerer does not already know."""
    return _tagged_feat_options(character, ctx, "metamagic", level)


def invocation_options(
    character: str, pick: Any, ctx: ToolContext, level: int | None = None
) -> list[dict[str, Any]]:
    """The Eldritch Invocations open to the warlock at this level."""
    return _tagged_feat_options(character, ctx, "invocation", level)


def subclass_options(
    character: str, pick: Any, ctx: ToolContext, level: int | None = None
) -> list[dict[str, Any]]:
    """The subclasses open to the character's class."""
    progression = require_progression(ctx.world)
    spec = ctx.world.character
    found = _read_character(character, ctx)
    rules = _class_rules(found, progression)
    return [
        _option(spec.features[feature_id])
        for subclass in progression.subclasses.values()
        if subclass.class_id == rules.id
        and (feature_id := f"subclass-{subclass.id}") in spec.features
        and feature_id not in found.features
    ]


def expertise_options(
    character: str, pick: Any, ctx: ToolContext, level: int | None = None
) -> list[dict[str, Any]]:
    """One option for each proficient skill the character can sharpen into expertise."""
    spec = ctx.world.character
    found = _read_character(character, ctx)
    proficiency = proficiency_bonus(_stored_level(found))
    options = []
    for skill_id in spec.skills:
        level = spec.skill_level(found.skills, skill_id)
        if level < proficiency:
            continue
        feature_id = f"expertise-{skill_id}"
        feature = spec.features.get(feature_id)
        if feature is None:
            continue
        options.append(_option(feature))
    return options


def apply_spell_pick(character: str, pick: Any, choice: str, ctx: ToolContext) -> dict[str, Any]:
    """Learn the chosen spell and spend one spell pick."""
    result = learn_spell(character, choice, ctx)
    remaining = ctx.world.adjust_counter(character, pick.counter, -1)
    return {**result, pick.counter: remaining["value"]}


def apply_feat_pick(character: str, pick: Any, choice: str, ctx: ToolContext) -> dict[str, Any]:
    """Learn the chosen feat and spend one feat pick."""
    result = _learn(ctx, character, choice, cause=pick.label)
    remaining = ctx.world.adjust_counter(character, pick.counter, -1)
    return {**result, pick.counter: remaining["value"]}


def apply_fighting_style_pick(
    character: str, pick: Any, choice: str, ctx: ToolContext
) -> dict[str, Any]:
    """Learn the chosen fighting style and spend one pick."""
    result = _learn(ctx, character, choice, cause="fighting style")
    remaining = ctx.world.adjust_counter(character, pick.counter, -1)
    return {**result, pick.counter: remaining["value"]}


def _learn_always_prepared(
    ctx: ToolContext, character: str, subclass: DndSubclass, level: int
) -> None:
    """Learn a subclass's always-prepared spells and add them to the prepared mark."""
    if not subclass.always_prepared:
        return
    spell_ids = [
        spell
        for spell_level, spells in subclass.spells.items()
        if spell_level <= level
        for spell in spells
    ]
    for spell_id in spell_ids:
        _learn(ctx, character, spell_id, cause=f"{subclass.id} spells")
    if not spell_ids:
        return

    def action(hc: HookContext) -> None:
        found = hc.character(character)
        existing = [str(item) for item in found.marks.get(ALWAYS_PREPARED_MARK) or []]
        merged = list(dict.fromkeys([*existing, *spell_ids]))
        hc.set_mark(
            found.name,
            ALWAYS_PREPARED_MARK,
            merged,
            f"{found.name} always has {len(merged)} subclass spells prepared.",
        )

    ctx.world.with_hook_context(action)


def apply_subclass_pick(character: str, pick: Any, choice: str, ctx: ToolContext) -> dict[str, Any]:
    """Choose a subclass: learn its marker and every subclass feature gained so far."""
    progression = require_progression(ctx.world)
    found = _read_character(character, ctx)
    subclass_id = choice.removeprefix("subclass-")
    subclass = progression.subclasses.get(subclass_id)
    if subclass is None:
        raise InvalidInput(f"there is no subclass named {subclass_id!r}")
    level = _level(found)
    result = _learn(ctx, character, choice, cause="subclass")
    for feature_level, feature_ids in subclass.features.items():
        if feature_level <= level:
            for feature_id in feature_ids:
                _learn(ctx, character, feature_id, cause=f"{subclass.id} level {feature_level}")
    _learn_always_prepared(ctx, character, subclass, level)
    remaining = ctx.world.adjust_counter(character, pick.counter, -1)
    return {**result, pick.counter: remaining["value"]}


def apply_expertise_pick(
    character: str, pick: Any, choice: str, ctx: ToolContext
) -> dict[str, Any]:
    """Double the chosen skill's proficiency by raising it to its current level plus PB."""
    spec = ctx.world.character
    found = _read_character(character, ctx)
    skill_id = choice.removeprefix("expertise-")
    if skill_id not in spec.skills:
        raise InvalidInput(f"{choice!r} is not an expertise option")
    current_level = spec.skill_level(found.skills, skill_id)
    bonus = proficiency_bonus(_stored_level(found))
    new_level = min(
        current_level + bonus, spec.skill_progression.maximum if spec.skill_progression else 20
    )
    result = ctx.world.raise_skill(character, skill_id, new_level, cause="expertise")
    remaining = ctx.world.adjust_counter(character, pick.counter, -1)
    return {**result, pick.counter: remaining["value"]}


def _option(feature: Feature) -> dict[str, Any]:
    return {
        "id": feature.id,
        "name": feature.name,
        "description": feature.description,
        "rank": feature.rank,
        "repeatable": feature.repeatable,
    }


# --- tools --------------------------------------------------------------------------------


def roll_save(
    name: str,
    stat: str,
    dc: int,
    cause: str,
    ctx: ToolContext,
    roll: int = 0,
    roll_mode: str = "normal",
) -> dict[str, Any]:
    """Roll an ability save against a DC, or take the player's roll, and record it."""
    spec = ctx.world.character
    stat_id = _ability_id(stat)
    difficulty = whole_number(dc, "dc", 1, 40)
    found = _read_character(name, ctx)
    bonus = _save_bonus(found, spec, require_progression(ctx.world), stat_id)
    bonus += condition_modifier(found, getattr(ctx.world, "conditions", None), "save")
    if roll_mode == "normal":
        roll_mode = condition_roll_mode(found, getattr(ctx.world, "conditions", None), "save")
    mode = _roll_mode(roll_mode)
    if roll:
        rolled = whole_number(roll, "roll", 1, 20)
    else:
        _, rolled = roll_mode_values("1d20", ctx.world.rng, mode=mode)
    total = rolled + bonus
    passed = total >= difficulty

    def action(hc: HookContext) -> None:
        db, campaign_id = hc.session()
        record(
            db,
            campaign_id,
            CharacterSaved(
                name=found.name,
                stat=stat_id,
                roll=rolled,
                score=difficulty,
                passed=passed,
                cause=cause,
            ),
        )
        record(
            db,
            campaign_id,
            EffectNoted(
                name=found.name,
                text=(
                    f"{found.name}'s {ABILITY_NAMES[stat_id]} save: {rolled} "
                    f"{'+' if bonus >= 0 else '-'} {abs(bonus)} = {total} against DC "
                    f"{difficulty} ({'success' if passed else 'failure'})."
                ),
            ),
        )
        db.commit()

    ctx.world.with_hook_context(action)
    return {
        "character": found.name,
        "stat": stat_id,
        "roll": rolled,
        "bonus": bonus,
        "total": total,
        "dc": difficulty,
        "passed": passed,
        "roll_mode": mode,
    }


def death_save(name: str, ctx: ToolContext, roll: int = 0) -> dict[str, Any]:
    """Roll a death save for a player character at 0 hit points."""
    found = _read_character(name, ctx)
    if found.kind != PC_KIND:
        raise InvalidInput("death saves are for player characters")
    if found.dead:
        raise InvalidInput(f"{found.name} is already dead")
    if current(found, "hp") > 0:
        raise InvalidInput(f"{found.name} is not at 0 hit points")
    rolled = whole_number(roll, "roll", 1, 20) if roll else roll_total("1d20", ctx.world.rng)
    failures = found.counters.get("death_saves", 0)
    successes = found.counters.get("death_successes", 0)
    outcome = "success" if rolled >= 10 else "failure"
    if rolled == 20:
        outcome = "revived"
    elif rolled == 1:
        outcome = "critical failure"
    if outcome == "revived":
        _record_death_save(ctx, found.name, rolled, True, "a natural 20 restores 1 hit point")
        ctx.world.restore_stat(found.name, "hp", 1, cause="a natural 20 on a death save")
        _reset_death_saves(ctx, found.name)
        _record_stabilized(ctx, found.name, by="a natural 20 on a death save")
        _record_recovered(ctx, found.name, "A natural 20 on a death save brings them around.")
        return {
            "character": found.name,
            "roll": rolled,
            "outcome": outcome,
            "failures": 0,
            "successes": 0,
        }
    gain = 2 if rolled == 1 else 0
    if outcome == "success":
        successes += 1
        _adjust_death_counter(ctx, found.name, "death_successes", 1)
    else:
        failures += 1 + gain
        _adjust_death_counter(ctx, found.name, "death_saves", 1 + gain)
    _record_death_save(ctx, found.name, rolled, outcome == "success", outcome)
    if failures >= 3:

        def action(hc: HookContext) -> None:
            db, campaign_id = hc.session()
            record(
                db, campaign_id, CharacterDied(name=found.name, cause="three failed death saves")
            )
            db.commit()

        ctx.world.with_hook_context(action)
        _reset_death_saves(ctx, found.name)
        return {
            "character": found.name,
            "roll": rolled,
            "outcome": "dead",
            "failures": 3,
            "successes": successes,
        }
    if successes >= 3:
        _record_stabilized(ctx, found.name, by="three successful death saves")
        _reset_death_saves(ctx, found.name)
        return {
            "character": found.name,
            "roll": rolled,
            "outcome": "stable",
            "failures": failures,
            "successes": 3,
        }
    return {
        "character": found.name,
        "roll": rolled,
        "outcome": outcome,
        "failures": failures,
        "successes": successes,
    }


def _record_recovered(ctx: ToolContext, name: str, explanation: str) -> None:
    def action(hc: HookContext) -> None:
        db, campaign_id = hc.session()
        record(
            db,
            campaign_id,
            CharacterRecovered(name=name, explanation=explanation, consequence=""),
        )
        db.commit()

    ctx.world.with_hook_context(action)


def _record_death_save(ctx: ToolContext, name: str, roll: int, passed: bool, cause: str) -> None:
    def action(hc: HookContext) -> None:
        db, campaign_id = hc.session()
        record(
            db,
            campaign_id,
            CharacterSaved(
                name=name, stat="death", roll=roll, score=10, passed=passed, cause=cause
            ),
        )
        db.commit()

    ctx.world.with_hook_context(action)


def _record_stabilized(ctx: ToolContext, name: str, *, by: str) -> None:
    def action(hc: HookContext) -> None:
        db, campaign_id = hc.session()
        record(db, campaign_id, CharacterStabilized(name=name, by=by))
        db.commit()

    ctx.world.with_hook_context(action)


def _adjust_death_counter(ctx: ToolContext, name: str, counter: str, amount: int) -> None:
    if counter in prog_counter_ids(ctx):
        ctx.world.adjust_counter(name, counter, amount)


def _reset_death_saves(ctx: ToolContext, name: str) -> None:
    found = _read_character(name, ctx)
    for counter in ("death_saves", "death_successes"):
        value = found.counters.get(counter, 0)
        if value and counter in prog_counter_ids(ctx):
            ctx.world.adjust_counter(name, counter, -value)


def spend_hit_die(name: str, ctx: ToolContext) -> dict[str, Any]:
    """Spend one Hit Point Die on a short rest and heal its roll plus Constitution."""
    progression = require_progression(ctx.world)
    spec = ctx.world.character
    found = _read_character(name, ctx)
    if found.counters.get("hit_dice", 0) <= 0:
        raise InvalidInput(f"{found.name} has no Hit Point Dice left")
    if current(found, "hp") <= 0:
        raise InvalidInput(f"{found.name} is at 0 hit points and must stabilize first")
    rules = _class_rules(found, progression)
    rolled = roll_total(f"1d{rules.hit_die}", ctx.world.rng)
    healed = max(1, rolled + _modifier(found, spec, "con"))
    ctx.world.adjust_counter(name, "hit_dice", -1)
    result = ctx.world.restore_stat(name, "hp", healed, cause="spending a Hit Point Die")
    return {
        "character": found.name,
        "hit_die": f"1d{rules.hit_die}",
        "roll": rolled,
        "healed": healed,
        "hit_dice_left": found.counters.get("hit_dice", 1) - 1,
        **result,
    }


def attune_item(character: str, item: str, ctx: ToolContext) -> dict[str, Any]:
    """Attune to a carried magic item during a short rest. A character holds at most three
    attunements."""
    spec = ctx.world.character
    found = _read_character(character, ctx)
    feature = _item_feature(spec, item)
    if "attunement" not in (feature.description or "").casefold():
        raise InvalidInput(f"{feature.name} does not require attunement")
    if not any(same_item_name(carried.name, feature.name) for carried in found.items):
        raise InvalidInput(f"{found.name} does not carry {feature.name}")
    attuned = [str(entry) for entry in found.marks.get(ATTUNED_MARK) or []]
    if feature.id in attuned or feature.name in attuned:
        return {"character": found.name, "attuned": attuned, "already": True}
    if len(attuned) >= 3:
        raise InvalidInput(f"{found.name} is already attuned to three items")

    def action(hc: HookContext) -> None:
        current = hc.character(character)
        names = [*attuned, feature.name]
        hc.set_mark(
            current.name,
            ATTUNED_MARK,
            names,
            f"{current.name} attunes to {feature.name}.",
        )

    ctx.world.with_hook_context(action)
    return {
        "character": found.name,
        "attuned": [*attuned, feature.name],
        "item": feature.id,
    }


def unattune_item(character: str, item: str, ctx: ToolContext) -> dict[str, Any]:
    """End an attunement to a magic item."""
    spec = ctx.world.character
    found = _read_character(character, ctx)
    feature = _item_feature(spec, item)
    attuned = [str(entry) for entry in found.marks.get(ATTUNED_MARK) or []]
    remaining = [entry for entry in attuned if entry not in {feature.id, feature.name}]
    if len(remaining) == len(attuned):
        raise InvalidInput(f"{found.name} is not attuned to {feature.name}")

    def action(hc: HookContext) -> None:
        current = hc.character(character)
        hc.set_mark(
            current.name,
            ATTUNED_MARK,
            remaining or None,
            f"{current.name} ends the attunement to {feature.name}.",
        )

    ctx.world.with_hook_context(action)
    return {"character": found.name, "attuned": remaining, "item": feature.id}


def _item_feature(spec: CharacterSpec, item: str) -> Feature:
    wanted = item.strip().casefold()
    matches = [
        feature
        for feature in spec.features.values()
        if feature.feature_type == "item"
        and (feature.id.casefold() == wanted or feature.name.casefold() == wanted)
    ]
    if not matches:
        raise InvalidInput(f"there is no item named {item!r}")
    if len(matches) > 1:
        raise InvalidInput(f"{item!r} is ambiguous; give the item id")
    return matches[0]


def choose_land(character: str, land: str, ctx: ToolContext) -> dict[str, Any]:
    """Choose the land whose Circle spells a Circle of the Land druid has prepared."""
    progression = require_progression(ctx.world)
    spec = ctx.world.character
    found = _read_character(character, ctx)
    rules = _class_rules(found, progression)
    chosen_subclass = subclass_id(found, spec, progression)
    if chosen_subclass is None:
        raise InvalidInput(f"{found.name} has no subclass")
    subclass = progression.subclasses[chosen_subclass]
    wanted = land.strip().casefold()
    if wanted not in subclass.land_spells:
        raise InvalidInput(
            f"{found.name}'s subclass has no land {land!r}; choose one of "
            + ", ".join(sorted(subclass.land_spells))
        )
    level = _level(found)
    spell_ids = [
        spell
        for spell_level, spells in subclass.land_spells[wanted].items()
        if spell_level <= level
        for spell in spells
    ]
    for spell_id in spell_ids:
        _learn(ctx, character, spell_id, cause=f"{wanted} land spells")

    def action(hc: HookContext) -> None:
        current = hc.character(character)
        hc.set_mark(
            current.name,
            LAND_MARK,
            wanted,
            f"{current.name} chooses the {wanted} land for the Circle spells.",
        )
        hc.set_mark(
            current.name,
            ALWAYS_PREPARED_MARK,
            spell_ids or None,
            f"{current.name} always has the {wanted} land spells prepared.",
        )

    ctx.world.with_hook_context(action)
    return {
        "character": found.name,
        "land": wanted,
        "spells": spell_ids,
        "class": rules.id,
    }


def award_xp(character: str, amount: int, ctx: ToolContext, cause: str = "") -> dict[str, Any]:
    """Award experience for a defeated encounter, a completed quest, or a milestone. Levels are
    taken on the advancement page once the experience reaches the next threshold."""
    found = _read_character(character, ctx)
    gained = whole_number(amount, "amount", 1, 1_000_000)
    result = ctx.world.adjust_counter(character, "xp", gained)
    return {
        "character": found.name,
        "gained": gained,
        "xp": result["value"],
        "cause": cause or "experience awarded",
    }


def roll_initiative(ctx: ToolContext) -> dict[str, Any]:
    """Roll initiative for every combatant: d20 plus Dexterity, highest total first. A bestiary
    NPC keeps the initiative bonus printed in its statblock when one was stored. Records the
    order before the first turn, so no player has to roll a separate initiative save."""

    def action(hc: HookContext) -> dict[str, Any]:
        state = hc.state()
        encounter = state.encounter
        if encounter is None:
            raise InvalidInput("there is no encounter to roll initiative for")
        if encounter.settled:
            raise InvalidInput("initiative is already set for this encounter")
        if encounter.round > 1:
            raise InvalidInput("initiative is rolled only at the start of an encounter")
        if encounter.acting is not None:
            raise InvalidInput(f"{encounter.acting} is taking a turn; finish it first")
        if not encounter.order:
            raise InvalidInput("there is no one in the encounter to roll initiative for")
        rolls: list[dict[str, Any]] = []
        for name in encounter.order:
            character = state.characters.get(name_key(name))
            if character is None:
                continue
            bonus = _initiative_bonus(character, getattr(ctx.world, "monsters", {}))
            roll = ctx.world.rng.randint(1, 20)
            rolls.append(
                {
                    "name": character.name,
                    "roll": roll,
                    "bonus": bonus,
                    "total": roll + bonus,
                }
            )
        if not rolls:
            raise InvalidInput("there is no one in the encounter to roll initiative for")
        order = [
            entry["name"]
            for entry in sorted(
                rolls,
                key=lambda entry: (-int(entry["total"]), encounter.order.index(entry["name"])),
            )
        ]
        db, campaign_id = hc.session()
        record(db, campaign_id, InitiativeSettled(order=order, rolls=rolls, individual=True))
        db.commit()
        return {"order": order, "rolls": rolls}

    return ctx.world.with_hook_context(action)


def _initiative_bonus(character: Any, monsters: Any = None) -> int:
    """Dexterity for a player character; a bestiary NPC's printed initiative bonus when known."""
    if character.kind != PC_KIND and monsters:
        template = next(
            (
                monster
                for monster in monsters.values()
                if monster.name.casefold() == str(character.creature).casefold()
            ),
            None,
        )
        if template is not None:
            return int(template.initiative_bonus)
    printed = character.stats.get("initiative_bonus")
    if character.kind != PC_KIND and printed is not None:
        return int(printed)
    dex = int(character.stats.get("dex", 10))
    bonus = (dex - 10) // 2
    # Alert adds the proficiency bonus to Initiative.
    if "feat-alert" in character.features:
        bonus += proficiency_bonus(_stored_level(character))
    return bonus


def attacks_per_turn(character: Any) -> int:
    """The 2024 Extra Attack tiers. The feature ids carry the class and tier."""
    features = character.features
    if "class-fighter-three-extra-attacks" in features:
        return 4
    if "class-fighter-two-extra-attacks" in features:
        return 3
    if any(
        feature in features
        for feature in (
            "class-barbarian-extra-attack",
            "class-fighter-extra-attack",
            "class-monk-extra-attack",
            "class-paladin-extra-attack",
            "class-ranger-extra-attack",
        )
    ):
        return 2
    return 1


def end_concentration(name: str, ctx: ToolContext) -> dict[str, Any]:
    """End the concentration spell a character is maintaining, such as when they choose to stop or
    fail a Constitution save against damage."""

    def action(hc: HookContext) -> dict[str, Any]:
        found = hc.character(name)
        current_spell = found.marks.get(CONCENTRATION_MARK)
        if current_spell:
            hc.set_mark(
                found.name,
                CONCENTRATION_MARK,
                None,
                f"{found.name} stops concentrating on a spell.",
            )
        return {"character": found.name, "ended": str(current_spell) if current_spell else None}

    return ctx.world.with_hook_context(action)


def resolve_hazard(
    hazard: str,
    targets: Sequence[str],
    ctx: ToolContext,
    save: str = "",
    dc: int = 0,
    damage: str = "",
    half_on_save: bool = False,
    condition: str = "",
    stat: str = "",
    cause: str = "",
    save_modifier: int = 0,
) -> dict[str, Any]:
    """Resolve a D&D hazard against NPCs with d20 + save bonus against the listed DC. Player
    characters roll their own save, so naming one is refused. Use this for a trap, breath
    weapon, falling rocks, or any area effect; characters that fail take damage and the named
    condition."""
    progression = require_progression(ctx.world)
    spec = ctx.world.character
    difficulty = whole_number(dc, "dc", 1, 40) if save else 0
    if save and not difficulty:
        raise InvalidInput("a hazard save needs a DC")
    if half_on_save and not (save and damage):
        raise InvalidInput("half_on_save needs a save and damage")
    if not save and not damage and not condition:
        raise InvalidInput("a hazard needs a save, damage, or a condition")
    stat_id = _ability_id(stat) if stat else "hp"
    if stat_id != "hp":
        raise InvalidInput("D&D hazards take hit points; leave stat out or give hp")
    amount_formula = damage.strip()
    if amount_formula and not (amount_formula.isdigit() or is_formula(amount_formula)):
        raise InvalidInput("hazard damage must be dice such as 1d6, or a whole number")
    names = [str(target).strip() for target in targets if str(target).strip()]
    if not names:
        raise InvalidInput("a hazard needs at least one target")
    reason = cause.strip() or hazard
    results: list[dict[str, Any]] = []
    for target in names:
        found = _read_character(target, ctx)
        if found.kind == PC_KIND:
            raise InvalidInput(
                f"{found.name} is a player character; call player_save then apply the result"
            )
        result: dict[str, Any] = {"target": found.name}
        passed = False
        if save:
            stat = _ability_id(save)
            bonus = _save_bonus(found, spec, progression, stat) + save_modifier
            bonus += condition_modifier(found, getattr(ctx.world, "conditions", None), "save")
            natural = ctx.world.rng.randint(1, 20)
            total = natural + bonus
            passed = total >= difficulty
            result["save"] = {
                "stat": stat,
                "roll": natural,
                "bonus": bonus,
                "total": total,
                "dc": difficulty,
                "passed": passed,
            }
        if (not save or not passed) and condition:
            ctx.world.add_condition(found.name, condition, f"{reason}: {hazard}")
            result["condition"] = condition
        if damage and (not save or not passed or half_on_save):
            rolled = (
                int(amount_formula)
                if amount_formula.isdigit()
                else roll_total(amount_formula, ctx.world.rng, plus="sum")
            )
            taken = rolled // 2 if save and passed and half_on_save else rolled
            if taken:
                ctx.world.harm_character(found.name, "hp", taken, f"{reason}: {hazard}")
            result["damage"] = taken
        results.append(result)
    return {"hazard": hazard, "results": results}


def spell_attack(
    name: str,
    spell: str,
    target: str,
    ctx: ToolContext,
    roll: int = 0,
    roll_mode: str = "normal",
) -> dict[str, Any]:
    """Roll a spell attack: d20 plus proficiency bonus and the class's spellcasting ability
    modifier against the target's Armor Class. Use this for Fire Bolt, Guiding Bolt, Eldritch
    Blast, and any other spell that says “spell attack”; apply the spell's damage with
    harm_character after a hit."""
    progression = require_progression(ctx.world)
    spec = ctx.world.character
    found = _read_character(name, ctx)
    feature = _find_spell(spec, spell)
    rules = _class_rules(found, progression)
    if rules.casting == "none":
        raise InvalidInput(f"{found.name}'s class cannot cast spells")
    if not _knows_spell(found, feature, progression, spec):
        raise InvalidInput(f"{found.name} does not know {feature.name}")
    if feature.rank > 0 and not _is_prepared(found, feature.id, progression, spec):
        raise InvalidInput(f"{found.name} has not prepared {feature.name}")
    struck = _read_character(target, ctx)
    if struck.name == found.name:
        raise InvalidInput("a spell attack needs a target other than the caster")
    mode = _roll_mode(roll_mode)
    if roll:
        natural = whole_number(roll, "roll", 1, 20)
    else:
        _, natural = roll_mode_values("1d20", ctx.world.rng, mode=mode)
    modifier = _modifier(found, spec, rules.ability or "int")
    bonus = proficiency_bonus(_stored_level(found)) + modifier
    total = natural + bonus
    hit = total >= struck.armor
    result = {
        "character": found.name,
        "spell": feature.name,
        "target": struck.name,
        "roll": natural,
        "bonus": bonus,
        "total": total,
        "target_ac": struck.armor,
        "hit": hit,
        "roll_mode": mode,
    }

    def action(hc: HookContext) -> dict[str, Any]:
        db, campaign_id = hc.session()
        record(
            db,
            campaign_id,
            SpellAttackMade(
                name=found.name,
                spell=feature.name,
                target=struck.name,
                roll=natural,
                bonus=bonus,
                total=total,
                target_ac=struck.armor,
                hit=hit,
            ),
        )
        record(db, campaign_id, EncounterAttacked(name=found.name))
        db.commit()
        return result

    return ctx.world.with_hook_context(action)


def use_class_resource(
    character: str, resource: str, ctx: ToolContext, amount: int = 1, cause: str = ""
) -> dict[str, Any]:
    """Spend uses from a class resource pool, such as Rages, Focus Points, or Sorcery Points."""
    spec = ctx.world.character
    found = _read_character(character, ctx)
    if resource not in RESOURCE_COUNTERS:
        raise InvalidInput("resource must be one of: " + ", ".join(sorted(RESOURCE_COUNTERS)))
    if resource not in spec.counters or resource not in found.counters:
        raise InvalidInput(f"{found.name} has no {resource} pool")
    spent = whole_number(amount, "amount", 1, 100)
    if int(found.counters.get(resource, 0)) < spent:
        raise InvalidInput(f"{found.name} has no {resource} uses left")
    result = ctx.world.adjust_counter(character, resource, -spent)
    return {
        "character": found.name,
        "resource": resource,
        "spent": spent,
        "remaining": result["value"],
        "cause": cause or f"spending {resource}",
    }


def use_feature(character: str, feature: str, ctx: ToolContext, cause: str = "") -> dict[str, Any]:
    """Use a class or species feature, spending its resource counter and turn resource."""
    spec = ctx.world.character
    found = _read_character(character, ctx)
    wanted = feature.strip().casefold()
    matches = [
        source
        for source in spec.features.values()
        if source.id.casefold() == wanted or source.name.casefold() == wanted
    ]
    if not matches:
        raise InvalidInput(f"there is no feature named {feature!r}")
    if len(matches) > 1:
        raise InvalidInput(f"{feature!r} is ambiguous; give the feature id")
    source = matches[0]
    if source.id not in found.features:
        raise InvalidInput(f"{found.name} does not have {source.name}")
    remaining: int | None = None
    if source.resource_counter:
        current = int(found.counters.get(source.resource_counter, 0))
        if current < 1:
            raise InvalidInput(
                f"{found.name} has no {source.resource_counter.replace('_', ' ')} left"
            )
        changed = ctx.world.adjust_counter(character, source.resource_counter, -1)
        remaining = int(changed["value"])
    _spend_turn_resource(found, source, ctx, cause=cause or f"using {source.name}")
    if source.active_mark:

        def mark(hc: HookContext) -> None:
            hc.set_mark(
                found.name,
                source.active_mark,
                True,
                f"{found.name} is using {source.name}.",
            )

        ctx.world.with_hook_context(mark)
    return {
        "character": found.name,
        "feature": source.id,
        "feature_name": source.name,
        "resource": source.resource_counter,
        "remaining": remaining,
        "turn_resource": source.turn_resource,
        "active_mark": source.active_mark,
    }


def cast_spell(
    name: str,
    spell: str,
    ctx: ToolContext,
    slot_level: int = 0,
    ritual: bool = False,
) -> dict[str, Any]:
    """Cast a known spell, spending a slot of the chosen level or the spell's own. A prepared
    spell with the Ritual tag can be cast as a ritual instead, taking 10 minutes and no slot."""
    progression = require_progression(ctx.world)
    spec = ctx.world.character
    found = _read_character(name, ctx)
    feature = _find_spell(spec, spell)
    if not _knows_spell(found, feature, progression):
        raise InvalidInput(f"{found.name} does not know {feature.name}")
    rank = feature.rank
    rules = _class_rules(found, progression)
    is_ritual = "| ritual" in (feature.description or "").casefold()
    if rank == 0:
        if ritual:
            raise InvalidInput(f"{feature.name} is a cantrip, not a ritual")
        _spend_cast_resource(found, feature, ctx)
        return _cast_result(found, feature, 0, ctx, spec, rules)
    # A wizard's Ritual Adept casts an unprepared ritual straight from the spellbook.
    adept_ritual = ritual and rules.id == "wizard" and is_ritual
    if not adept_ritual and not _is_prepared(found, feature.id, progression):
        raise InvalidInput(f"{found.name} has not prepared {feature.name}")
    if ritual:
        if not is_ritual:
            raise InvalidInput(f"{feature.name} has no Ritual tag")
        if slot_level:
            raise InvalidInput("a ritual cannot be cast at a higher level")
        _set_concentration(ctx, found, feature, spec)
        return _cast_result(found, feature, 0, ctx, spec, rules, ritual=True)
    chosen_slot = whole_number(slot_level, "slot_level", 0, 9) if slot_level else rank
    if chosen_slot < rank:
        raise InvalidInput(
            f"{feature.name} is level {rank}; a level {chosen_slot} slot cannot cast it"
        )
    available = {index: found.counters.get(f"slot_{index}", 0) for index in range(1, 10)}
    used = 0
    if available.get(chosen_slot, 0) > 0:
        used = chosen_slot
    elif rules.casting == "pact":
        candidates = [index for index, count in available.items() if count > 0 and index >= rank]
        if candidates:
            used = max(candidates)
    if not used:
        # A lineage spell or similar feature can be cast once without a slot between long rests.
        free_feature = _free_cast_feature(found, spec, feature.id)
        free_used = [str(item) for item in found.marks.get(FREE_CAST_MARK) or []]
        if free_feature is not None and feature.id not in free_used:
            _spend_cast_resource(found, feature, ctx)

            def mark_free(hc: HookContext) -> None:
                hc.set_mark(
                    found.name,
                    FREE_CAST_MARK,
                    [*free_used, feature.id],
                    f"{found.name} casts {feature.name} without a spell slot.",
                )

            ctx.world.with_hook_context(mark_free)
            return _cast_result(found, feature, 0, ctx, spec, rules)
        # Magic Initiate's level 1 spell can be cast once without a slot between long rests.
        magic_spells = found.feature_choices.get("feat-magic-initiate:spell", [])
        if feature.id in magic_spells and found.marks.get(MAGIC_INITIATE_MARK) != feature.id:
            _spend_cast_resource(found, feature, ctx)

            def mark(hc: HookContext) -> None:
                hc.set_mark(
                    found.name,
                    MAGIC_INITIATE_MARK,
                    feature.id,
                    f"{found.name} casts {feature.name} with Magic Initiate.",
                )

            ctx.world.with_hook_context(mark)
            return _cast_result(found, feature, 0, ctx, spec, rules)
        raise InvalidInput(
            f"{found.name} has no spell slot of level {chosen_slot} or higher available"
        )
    _spend_cast_resource(found, feature, ctx)
    ctx.world.adjust_counter(name, f"slot_{used}", -1)
    _set_concentration(ctx, found, feature, spec)
    return _cast_result(found, feature, used, ctx, spec, rules)


def prepare_spells(character: str, spells: Sequence[str], ctx: ToolContext) -> dict[str, Any]:
    """Prepare the leveled spells a caster can cast today. A wizard prepares from the spellbook;
    a cleric, druid, paladin, or ranger prepares from the class list; a bard, sorcerer, or warlock
    prepares from the spells they know. Cantrips are always available and take no slot in the
    list."""
    progression = require_progression(ctx.world)
    spec = ctx.world.character
    chosen: list[str] = []
    for spell in spells:
        feature = _find_spell(spec, str(spell))
        if feature.rank == 0:
            raise InvalidInput(f"{feature.name} is a cantrip and is always available")
        chosen.append(feature.id)
    if len(set(chosen)) != len(chosen):
        raise InvalidInput("a prepared spell may not be listed twice")

    def check(hc: HookContext) -> dict[str, Any]:
        found = hc.character(character)
        rules = _class_rules(found, progression)
        if rules.casting == "none":
            raise InvalidInput(f"{found.name}'s class cannot cast spells")
        level = _level(found)
        limit = rules.spells[level - 1] if level <= len(rules.spells) else 0
        max_rank = _max_rank_for(rules, progression, level)
        for spell_id in chosen:
            feature = spec.features[spell_id]
            if feature.rank > max_rank:
                raise InvalidInput(
                    f"{feature.name} is level {feature.rank}; {found.name} has no slot of that "
                    "level"
                )
            if spell_id in found.features:
                continue
            if rules.id not in {"cleric", "druid", "paladin", "ranger"}:
                raise InvalidInput(f"{found.name} does not know {feature.name}")
            class_spells = progression.spell_lists.get(rules.id, {}).get(feature.rank, ())
            if spell_id not in class_spells:
                raise InvalidInput(f"{feature.name} is not a {rules.id} spell")
        if len(chosen) > limit:
            raise InvalidInput(
                f"{found.name} may prepare {limit} spells at level {level}, not {len(chosen)}"
            )
        return {"limit": limit, "level": level}

    allowed = ctx.world.with_hook_context(check)
    for spell_id in chosen:
        _learn(ctx, character, spell_id, cause="preparing spells")
    names = ", ".join(spec.features[spell_id].name for spell_id in chosen) or "no spells"

    def write(hc: HookContext) -> dict[str, Any]:
        found = hc.character(character)
        hc.set_mark(
            found.name,
            PREPARED_MARK,
            chosen,
            f"{found.name} prepares {names}.",
        )
        return {
            "character": found.name,
            "prepared": chosen,
            "limit": allowed["limit"],
            "level": allowed["level"],
        }

    return ctx.world.with_hook_context(write)


def _knows_spell(
    found: Any,
    feature: Feature,
    progression: DndProgression | None = None,
    spec: CharacterSpec | None = None,
) -> bool:
    """Whether a character knows a spell, including a class spell that is always prepared."""
    if found.features.get(feature.id, 0) > 0:
        return True
    return progression is not None and _is_prepared(found, feature.id, progression, spec)


def _is_prepared(
    found: Any,
    spell_id: str,
    progression: DndProgression | None = None,
    spec: CharacterSpec | None = None,
) -> bool:
    """Whether a leveled spell is prepared. A sheet recorded before preparation existed has no
    mark, so its known spells stay castable until the player prepares a list. Class and
    subclass always-prepared spells are always castable."""
    if spec is not None:
        for feature_id in found.features:
            feature = spec.features.get(feature_id)
            if feature is not None and feature.always_prepared and spell_id in feature.learn_spells:
                return True
    always = [str(item) for item in found.marks.get(ALWAYS_PREPARED_MARK) or []]
    if spell_id in always:
        return True
    if progression is not None:
        class_id = found.templates.get("class", "")
        rules = progression.classes.get(class_id)
        if rules is not None:
            level = _level(found)
            if any(
                spell_id in spells
                for spell_level, spells in rules.always_prepared.items()
                if spell_level <= level
            ):
                return True
        # A Circle of the Land druid only has the spells of the land chosen for the day.
        if any(
            feature_id.startswith("subclass-circle-of-the-land") for feature_id in found.features
        ):
            current_land = str(found.marks.get(LAND_MARK, ""))
            for subclass in progression.subclasses.values():
                for land, land_levels in subclass.land_spells.items():
                    if any(spell_id in spells for spells in land_levels.values()):
                        return land == current_land
    prepared = found.marks.get(PREPARED_MARK)
    if prepared is None:
        return True
    return spell_id in [str(item) for item in prepared]


def _set_concentration(ctx: ToolContext, found: Any, feature: Feature, spec: CharacterSpec) -> None:
    """Record the concentration spell being maintained, ending any earlier one."""
    if "duration: concentration" not in (feature.description or "").casefold():
        return

    def action(hc: HookContext) -> None:
        current = hc.character(found.name).marks.get(CONCENTRATION_MARK)
        if current and current != feature.id:
            old = spec.features.get(str(current))
            hc.set_mark(
                found.name,
                CONCENTRATION_MARK,
                None,
                f"{found.name} stops concentrating on {old.name if old else current}.",
            )
        if current != feature.id:
            hc.set_mark(
                found.name,
                CONCENTRATION_MARK,
                feature.id,
                f"{found.name} concentrates on {feature.name}.",
            )

    ctx.world.with_hook_context(action)


def _spend_turn_resource(
    found: Any, feature: Feature, ctx: ToolContext, *, cause: str = ""
) -> None:
    """Spend a feature's turn resource when the character is taking a turn."""
    if not feature.turn_resource:
        return

    def read(hc: HookContext) -> str | None:
        encounter = hc.state().encounter
        return None if encounter is None else encounter.acting

    acting = ctx.world.with_hook_context(read)
    if acting is not None and name_key(str(acting)) == name_key(found.name):
        ctx.world.use_turn_resource(  # type: ignore[attr-defined]
            found.name, feature.turn_resource, cause=cause or f"using {feature.name}"
        )


def _spend_cast_resource(found: Any, feature: Feature, ctx: ToolContext) -> None:
    """Spend a spell's Action, Bonus Action, or Reaction when the caster is taking a turn."""
    _spend_turn_resource(found, feature, ctx, cause=f"casting {feature.name}")


def _free_cast_feature(found: Any, spec: CharacterSpec, spell_id: str) -> Feature | None:
    """The held feature that lets this spell be cast once without a slot."""
    for feature_id in found.features:
        source = spec.features.get(feature_id)
        if source is not None and source.free_cast and spell_id in source.learn_spells:
            return source
    return None


def _spellcasting_ability(
    found: Any, feature: Feature, spec: CharacterSpec, rules: DndClass
) -> str:
    """The ability a spell uses: Magic Initiate's chosen ability for the spells that feat
    taught, otherwise the class's spellcasting ability."""
    magic_spells = [
        *found.feature_choices.get("feat-magic-initiate:cantrips", []),
        *found.feature_choices.get("feat-magic-initiate:spell", []),
    ]
    if feature.id in magic_spells:
        chosen = found.feature_choices.get("feat-magic-initiate:ability", [])
        if chosen and chosen[0] in ABILITY_ORDER:
            return str(chosen[0])
    for feature_id in found.features:
        source = spec.features.get(feature_id)
        if source is None or feature.id not in source.learn_spells:
            continue
        chosen = found.feature_choices.get(f"{source.id}:ability", [])
        if chosen and chosen[0] in ABILITY_ORDER:
            return str(chosen[0])
    # A lineage's later spell features inherit the ability chosen for that lineage.
    for key, values in found.feature_choices.items():
        source_id = key.split(":", 1)[0]
        source = spec.features.get(source_id)
        if (
            key.endswith(":ability")
            and values
            and values[0] in ABILITY_ORDER
            and source is not None
            and source.tag.startswith("lineage-")
        ):
            return str(values[0])
    return rules.ability or "int"


def _cast_result(
    found: Any,
    feature: Feature,
    slot_level: int,
    ctx: ToolContext,
    spec: CharacterSpec,
    rules: DndClass,
    *,
    ritual: bool = False,
) -> dict[str, Any]:
    ability = _spellcasting_ability(found, feature, spec, rules)
    modifier = _modifier(found, spec, ability)
    bonus = proficiency_bonus(_stored_level(found))
    remaining = found.counters.get(f"slot_{slot_level}", 0) - 1 if slot_level else None
    return {
        "character": found.name,
        "spell": feature.name,
        "id": feature.id,
        "level": feature.rank,
        "slot_level": slot_level or None,
        "slots_left": max(0, remaining) if remaining is not None else None,
        "ritual": ritual,
        "spell_attack_bonus": bonus + modifier,
        "spell_save_dc": 8 + bonus + modifier,
        "description": feature.description,
        "concentration": "duration: concentration" in (feature.description or "").casefold(),
    }


def _remember_prepared(ctx: ToolContext, character: str, spell_id: str) -> None:
    """Add a newly learned leveled spell to the prepared list when it has room, and start a
    list for a caster who has none yet."""
    spec = ctx.world.character
    feature = spec.features.get(spell_id)
    if feature is None or feature.rank == 0:
        return
    progression = require_progression(ctx.world)
    found = _read_character(character, ctx)
    rules = _class_rules(found, progression)
    if rules.casting == "none":
        return
    level = _level(found)
    limit = rules.spells[level - 1] if level <= len(rules.spells) else 0
    if limit <= 0:
        return
    stored = found.marks.get(PREPARED_MARK)
    if stored is None:
        known = [
            found_id
            for found_id, count in found.features.items()
            if count > 0
            and (candidate := spec.features.get(found_id)) is not None
            and candidate.feature_type == "spell"
            and candidate.rank > 0
        ]
        prepared = known[:limit]
    else:
        prepared = [str(item) for item in stored]
        if spell_id not in prepared and len(prepared) < limit:
            prepared.append(spell_id)
    if prepared == [str(item) for item in stored or []]:
        return

    def action(hc: HookContext) -> None:
        current = hc.character(character)
        names = ", ".join(spec.features[item].name for item in prepared) or "no spells"
        hc.set_mark(
            current.name,
            PREPARED_MARK,
            prepared or None,
            f"{current.name} has {names} prepared.",
        )

    ctx.world.with_hook_context(action)


def learn_spell(name: str, spell: str, ctx: ToolContext) -> dict[str, Any]:
    """Learn a spell or cantrip without spending a pick, for a feature that grants one."""
    spec = ctx.world.character
    feature = _find_spell(spec, spell)
    result = _learn(ctx, name, feature.id, cause="a learned spell")
    _remember_prepared(ctx, name, feature.id)
    return result


def learn_feature(name: str, feature: str, ctx: ToolContext, cause: str = "") -> dict[str, Any]:
    """Learn a feature by id or name, for a species or background that grants a choice."""
    spec = ctx.world.character
    wanted = feature.strip().casefold()
    matches = [
        found
        for found in spec.features.values()
        if found.id.casefold() == wanted or found.name.casefold() == wanted
    ]
    if not matches:
        raise InvalidInput(f"there is no feature named {feature!r}")
    if len(matches) > 1:
        raise InvalidInput(f"{feature!r} is ambiguous; give the feature id")
    return _learn(ctx, name, matches[0].id, cause=cause or "a granted feature")


def _find_spell(spec: CharacterSpec, spell: str) -> Feature:
    wanted = spell.strip().casefold()
    wanted_id = wanted if wanted.startswith("spell-") else f"spell-{wanted}"
    matches = [
        feature
        for feature in spec.features.values()
        if feature.feature_type == "spell"
        and (feature.id.casefold() in (wanted, wanted_id) or feature.name.casefold() == wanted)
    ]
    if not matches:
        raise InvalidInput(f"there is no spell named {spell!r}")
    if len(matches) > 1:
        raise InvalidInput(f"{spell!r} is ambiguous; give the spell id")
    return matches[0]


# --- rest hook ----------------------------------------------------------------------------


def after_rest(
    hc: HookContext,
    name: str,
    length: str,
    start: int,
    end: int,
    interrupted: bool,
) -> None:
    """Refill spell slots and Hit Point Dice when a rest finishes."""
    if interrupted:
        return
    rule_data = hc.rule_data
    progression = rule_data.get("progression")
    if not isinstance(progression, DndProgression):
        return
    character = hc.character(name)
    if character.kind != PC_KIND:
        return
    rules = progression.classes.get(class_id(character))
    if rules is None:
        return
    db, _campaign_id = hc.session()
    level = _level(character)
    resources = _resource_targets(character, rules, level)
    if length == "short":
        if rules.slot_table == "pact":
            count, slot_level = progression.pact_slots[level - 1]
            _fill_counter(hc, character, f"slot_{slot_level}", count, maximum=4)
        for counter in ("rage", "channel_divinity", "wild_shape", "second_wind"):
            target = resources.get(counter, 0)
            if target and character.counters.get(counter, 0) < target:
                _fill_counter(
                    hc,
                    character,
                    counter,
                    min(target, int(character.counters.get(counter, 0)) + 1),
                    maximum=100,
                )
        for counter in ("action_surge", "focus_points"):
            if counter in resources:
                _fill_counter(hc, character, counter, resources[counter], maximum=100)
        return
    if length != "long":
        return
    if character.marks.get(MAGIC_INITIATE_MARK):
        hc.set_mark(
            character.name,
            MAGIC_INITIATE_MARK,
            None,
            f"{character.name}'s Magic Initiate spell is ready again.",
        )
    if character.marks.get(FREE_CAST_MARK):
        hc.set_mark(
            character.name,
            FREE_CAST_MARK,
            None,
            f"{character.name}'s free lineage spells are ready again.",
        )
    if character.marks.get("raging"):
        hc.set_mark(
            character.name,
            "raging",
            None,
            f"{character.name}'s Rage ends over the long rest.",
        )
    if character.marks.get(CONCENTRATION_MARK):
        hc.set_mark(
            character.name,
            CONCENTRATION_MARK,
            None,
            f"{character.name} stops concentrating on a spell over the long rest.",
        )
    targets = {index: 0 for index in range(1, 10)}
    if rules.slot_table == "pact":
        count, slot_level = progression.pact_slots[level - 1]
        targets[slot_level] = count
    elif rules.slot_table != "none":
        for index, count in enumerate(progression.slots[rules.slot_table][level - 1], start=1):
            targets[index] = count
    for index, target in targets.items():
        _fill_counter(hc, character, f"slot_{index}", target, maximum=4)
    _fill_counter(hc, character, "hit_dice", level, maximum=20)
    for counter in ("death_saves", "death_successes"):
        _fill_counter(hc, character, counter, 0, maximum=3)
    _fill_counter(
        hc,
        character,
        "giant_ancestry",
        proficiency_bonus(level) if "giant_ancestry" in character.counters else 0,
        maximum=6,
    )
    for counter, target in resources.items():
        _fill_counter(hc, character, counter, target, maximum=100)
    db.commit()


def _fill_counter(
    hc: HookContext, character: Any, counter: str, target: int, *, maximum: int
) -> None:
    """Set a counter to a rest's target, keeping it within its catalogue maximum."""
    target = min(target, maximum)
    current_value = character.counters.get(counter)
    if current_value is None or current_value == target:
        return
    db, campaign_id = hc.session()
    label = counter.replace("_", " ").title()
    record(
        db,
        campaign_id,
        CounterChanged(
            name=character.name,
            counter=counter,
            label=label,
            before=current_value,
            after=target,
            cause="a rest",
        ),
    )


# --- helpers ------------------------------------------------------------------------------


def _read_character(name: str, ctx: ToolContext) -> Any:
    holder: dict[str, Any] = {}

    def read(hc: HookContext) -> None:
        holder["character"] = hc.character(name)

    ctx.world.with_hook_context(read)
    return holder["character"]


def _learn(ctx: ToolContext, character: str, feature_id: str, *, cause: str) -> dict[str, Any]:
    spec = ctx.world.character
    feature = spec.features.get(feature_id)
    if feature is None:
        raise InvalidInput(f"there is no feature named {feature_id!r}")

    def action(hc: HookContext) -> bool:
        found = hc.character(character)
        if found.features.get(feature.id, 0) and not feature.repeatable:
            return False
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
        db.commit()
        return True

    learned = ctx.world.with_hook_context(action)
    for spell_id in feature.learn_spells:
        if spell_id != feature.id and spell_id in spec.features:
            _learn(ctx, character, spell_id, cause=f"{cause} ({feature.name})")
    return {
        "character": character,
        "feature": feature.id,
        "feature_name": feature.name,
        "learned": bool(learned),
    }


def _ability_id(stat: str) -> str:
    wanted = stat.strip().casefold()
    for name, ident in ABILITY_NAMES.items():
        if wanted in (ident.casefold(), ident[:3].casefold(), name):
            return name
    if wanted.startswith("save_"):
        return _ability_id(wanted[5:])
    raise InvalidInput(
        f"{stat!r} is not an ability; use one of {', '.join(ABILITY_NAMES.values())}"
    )


def _skill_id(spec: CharacterSpec, skill: str) -> str:
    wanted = skill.strip().casefold()
    for skill_id, rules in spec.skills.items():
        if wanted in (skill_id.casefold(), rules.name.casefold()):
            return skill_id
    raise InvalidInput(f"there is no skill named {skill!r}")


def _roll_mode(value: str) -> str:
    mode = (value or "normal").strip().casefold()
    if mode not in ROLL_MODES:
        raise InvalidInput(f"roll mode must be one of {', '.join(ROLL_MODES)}")
    return mode


def _xp_tracks(value: object, max_level: int, label: str) -> dict[str, tuple[int, ...]]:
    data = _object(value, label)
    tracks: dict[str, tuple[int, ...]] = {}
    for name, values in data.items():
        track = tuple(_whole(item, f"{label}[{name!r}]", 0, 1_000_000_000) for item in values)
        if len(track) != max_level:
            raise ContentError(f"{label}[{name!r}]: needs {max_level} levels")
        if track[0] != 0 or any(left >= right for left, right in pairwise(track)):
            raise ContentError(f"{label}[{name!r}]: thresholds must start at 0 and rise")
        tracks[str(name)] = track
    if not tracks:
        raise ContentError(f"{label}: must name at least one track")
    return tracks


def _slot_tables(
    value: object, max_level: int, label: str
) -> dict[str, tuple[tuple[int, ...], ...]]:
    data = _object(value, label)
    tables: dict[str, tuple[tuple[int, ...], ...]] = {}
    for name, rows in data.items():
        if name not in SLOT_TABLES:
            raise ContentError(f"{label}: {name!r} is not a slot table")
        parsed = []
        for index, row in enumerate(rows, start=1):
            counts = tuple(_whole(count, f"{label}[{name!r}][{index}]", 0, 9) for count in row)
            if len(counts) != 9:
                raise ContentError(f"{label}[{name!r}][{index}]: needs nine spell levels")
            parsed.append(counts)
        if len(parsed) != max_level:
            raise ContentError(f"{label}[{name!r}]: needs {max_level} levels")
        tables[name] = tuple(parsed)
    return tables


def _pact_table(value: object, max_level: int, label: str) -> tuple[tuple[int, int], ...]:
    rows = []
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, Sequence) or len(raw) != 2:
            raise ContentError(f"{label}[{index}]: needs [slot count, slot level]")
        rows.append(
            (
                _whole(raw[0], f"{label}[{index}] count", 0, 4),
                _whole(raw[1], f"{label}[{index}] level", 0, 9),
            )
        )
    if len(rows) != max_level:
        raise ContentError(f"{label}: needs {max_level} levels")
    return tuple(rows)


def _feature_levels(value: object, max_level: int, label: str) -> Mapping[int, tuple[str, ...]]:
    data = _object(value, label)
    levels: dict[int, tuple[str, ...]] = {}
    for key, features in data.items():
        try:
            number = int(key)
        except (TypeError, ValueError):
            raise ContentError(f"{label}: level keys must be whole numbers") from None
        level = _whole(number, f"{label} level", 1, max_level)
        levels[level] = _texts(features, f"{label}[{key!r}]")
    return MappingProxyType(levels)


def _per_level(value: object, max_level: int, label: str) -> tuple[int, ...]:
    if not isinstance(value, Sequence) or len(value) != max_level:
        raise ContentError(f"{label}: needs {max_level} levels")
    return tuple(_whole(item, f"{label}[{index}]", 0, 30) for index, item in enumerate(value, 1))


def _resource_levels(value: object, max_level: int, label: str) -> tuple[int, ...]:
    """A resource pool's size at each level, such as a Lay On Hands pool of 5 per level."""
    if not isinstance(value, Sequence) or len(value) != max_level:
        raise ContentError(f"{label}: needs {max_level} levels")
    return tuple(_whole(item, f"{label}[{index}]", 0, 100) for index, item in enumerate(value, 1))


def _levels(value: object, max_level: int, label: str) -> frozenset[int]:
    if not isinstance(value, list):
        raise ContentError(f"{label}: must be a list of levels")
    return frozenset(_whole(item, label, 1, max_level) for item in value)


def _object(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContentError(f"{label}: must be an object")
    return value


def _keys(data: Mapping[str, Any], expected: set[str], label: str) -> None:
    missing = sorted(expected - set(data))
    unknown = sorted(set(data) - expected)
    if missing:
        raise ContentError(f"{label}: missing {', '.join(missing)}")
    if unknown:
        raise ContentError(f"{label}: unexpected fields {', '.join(unknown)}")


def _text(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ContentError(f"{label}: must be text")
    if not allow_empty and not value.strip():
        raise ContentError(f"{label}: must not be empty")
    return value.strip()


def _texts(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ContentError(f"{label}: must be a list of names")
    return tuple(item.strip() for item in value)


def _whole(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ContentError(f"{label}: must be a whole number from {minimum} to {maximum}")
    return value
