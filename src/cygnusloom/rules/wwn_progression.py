"""Typed, validated data for Worlds Without Number numeric progression charts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise
from types import MappingProxyType
from typing import Any, Protocol

from cygnusloom.content.character import CharacterSpec
from cygnusloom.content.definitions import ContentError

_CASTING_TABLES = ("full_mage", "partial_mage", "dual_mage")
_NO_TRADITION = "no_tradition"
_PROGRESSION_FIELDS = (
    "type",
    "id",
    "python_module",
    "configure",
    "classes",
    "xp",
    "casting",
    "casting_classes",
    "traditions",
    "art_levels",
    "skill_level_minimums",
)


class _WorldWithRuleData(Protocol):
    @property
    def rule_data(self) -> object: ...


@dataclass(frozen=True)
class ClassTable:
    """One class's hit die, attack bonuses, focus levels, and skill-point modifier."""

    hit_die: int
    hp_constant: int
    attack: tuple[int, ...]
    focus_levels: frozenset[int]
    quick_learner: bool


@dataclass(frozen=True)
class CastingRow:
    """A spellcaster's spell limits at one character level."""

    max_spell_level: int
    casts: int
    prepared: int


@dataclass(frozen=True)
class WWNProgression:
    """Immutable WWN progression charts parsed from a ``rule_data/progression`` definition."""

    max_level: int
    max_skill_level: int
    class_tables: Mapping[str, ClassTable]
    xp_fast: tuple[int, ...]
    xp_slow: tuple[int, ...]
    casting_tables: Mapping[str, Mapping[int, CastingRow]]
    casting_class_tables: Mapping[str, str]
    tradition_tags: Mapping[str, str]
    tradition_effort_stats: Mapping[str, str]
    art_levels_full: Mapping[str, frozenset[int]]
    art_levels_partial: Mapping[str, frozenset[int]]
    skill_level_minimums: Mapping[int, int]

    @property
    def full_mage_classes(self) -> frozenset[str]:
        return frozenset(
            class_id
            for class_id, table_id in self.casting_class_tables.items()
            if table_id == "full_mage"
        )

    @property
    def partial_mage_classes(self) -> frozenset[str]:
        return frozenset(
            class_id
            for class_id, table_id in self.casting_class_tables.items()
            if table_id == "partial_mage"
        )

    @property
    def dual_mage_classes(self) -> frozenset[str]:
        return frozenset(
            class_id
            for class_id, table_id in self.casting_class_tables.items()
            if table_id == "dual_mage"
        )


def parse_wwn_progression(data: Mapping[str, Any], label: str = "progression") -> WWNProgression:
    """Parse one ``rule_data/progression`` mapping, such as ``Definition.data``.

    The caller supplies the label used in ``ContentError`` messages. Parsing is pure: it performs
    no file access and keeps no global state.
    """
    if not isinstance(label, str) or not label.strip():
        raise ContentError("WWN progression: label must be a non-empty string")
    label = label.strip()
    definition = _object(data, label)
    _exact_keys(definition, _PROGRESSION_FIELDS, label)
    if _text(_required(definition, "type", label), f"{label}.type") != "rule_data":
        raise ContentError(f"{label}: type must be 'rule_data'")
    if _text(_required(definition, "id", label), f"{label}.id") != "progression":
        raise ContentError(f"{label}: id must be 'progression'")
    if (
        _text(_required(definition, "python_module", label), f"{label}.python_module")
        != "wwn_progression"
    ):
        raise ContentError(f"{label}: python_module must be 'wwn_progression'")
    if (
        _text(_required(definition, "configure", label), f"{label}.configure")
        != "configure_progression"
    ):
        raise ContentError(f"{label}: configure must be 'configure_progression'")

    xp = _object(_required(definition, "xp", label), f"{label}.xp")
    _exact_keys(xp, ("fast", "slow"), f"{label}.xp")
    xp_fast = _integers(_required(xp, "fast", f"{label}.xp"), f"{label}.xp.fast", minimum=0)
    xp_slow = _integers(_required(xp, "slow", f"{label}.xp"), f"{label}.xp.slow", minimum=0)
    if not xp_fast or len(xp_fast) != len(xp_slow):
        raise ContentError(f"{label}.xp: tracks must have the same non-zero number of levels")
    if xp_fast[0] != 0 or xp_slow[0] != 0:
        raise ContentError(f"{label}.xp: both tracks must start at zero for level 1")
    for track_name, track in (("fast", xp_fast), ("slow", xp_slow)):
        if any(left >= right for left, right in pairwise(track)):
            raise ContentError(f"{label}.xp.{track_name}: thresholds must increase at every level")
    max_level = len(xp_fast)

    classes = _array(_required(definition, "classes", label), f"{label}.classes")
    if not classes:
        raise ContentError(f"{label}.classes: must contain at least one class")
    class_tables: dict[str, ClassTable] = {}
    for index, class_value in enumerate(classes):
        class_label = f"{label}.classes[{index}]"
        class_data = _object(class_value, class_label)
        _exact_keys(
            class_data,
            ("id", "hit_die", "hp_constant", "attack", "focus_levels", "quick_learner"),
            class_label,
        )
        class_id = _text(_required(class_data, "id", class_label), f"{class_label}.id")
        if class_id in class_tables:
            raise ContentError(f"{class_label}: duplicate class id {class_id!r}")
        attack = _integers(
            _required(class_data, "attack", class_label),
            f"{class_label}.attack",
            minimum=0,
        )
        if len(attack) != max_level:
            raise ContentError(
                f"{class_label}.attack: has {len(attack)} levels; expected {max_level}"
            )
        focus_levels = _integers(
            _required(class_data, "focus_levels", class_label),
            f"{class_label}.focus_levels",
            minimum=1,
        )
        if len(focus_levels) != len(set(focus_levels)) or any(
            level > max_level for level in focus_levels
        ):
            raise ContentError(
                f"{class_label}.focus_levels: levels must be unique and between 1 and {max_level}"
            )
        quick_learner = _required(class_data, "quick_learner", class_label)
        if not isinstance(quick_learner, bool):
            raise ContentError(f"{class_label}.quick_learner: must be true or false")
        class_tables[class_id] = ClassTable(
            hit_die=_integer(
                _required(class_data, "hit_die", class_label),
                f"{class_label}.hit_die",
                minimum=1,
            ),
            hp_constant=_integer(
                _required(class_data, "hp_constant", class_label),
                f"{class_label}.hp_constant",
            ),
            attack=attack,
            focus_levels=frozenset(focus_levels),
            quick_learner=quick_learner,
        )

    casting_data = _object(_required(definition, "casting", label), f"{label}.casting")
    _exact_keys(casting_data, _CASTING_TABLES, f"{label}.casting")
    casting_tables: dict[str, Mapping[int, CastingRow]] = {}
    for table_id in _CASTING_TABLES:
        table_label = f"{label}.casting.{table_id}"
        rows = _array(_required(casting_data, table_id, f"{label}.casting"), table_label)
        if len(rows) != max_level:
            raise ContentError(f"{table_label}: has {len(rows)} levels; expected {max_level}")
        table: dict[int, CastingRow] = {}
        for expected_level, row_value in enumerate(rows, start=1):
            row_label = f"{table_label}[{expected_level - 1}]"
            row = _object(row_value, row_label)
            _exact_keys(row, ("level", "max_spell_level", "casts", "prepared"), row_label)
            level = _integer(_required(row, "level", row_label), f"{row_label}.level", minimum=1)
            if level != expected_level:
                raise ContentError(f"{row_label}.level: expected {expected_level}, got {level}")
            table[level] = CastingRow(
                max_spell_level=_integer(
                    _required(row, "max_spell_level", row_label),
                    f"{row_label}.max_spell_level",
                    minimum=0,
                ),
                casts=_integer(_required(row, "casts", row_label), f"{row_label}.casts", minimum=0),
                prepared=_integer(
                    _required(row, "prepared", row_label), f"{row_label}.prepared", minimum=0
                ),
            )
        casting_tables[table_id] = MappingProxyType(table)

    class_casting_data = _object(
        _required(definition, "casting_classes", label), f"{label}.casting_classes"
    )
    casting_class_tables: dict[str, str] = {}
    for class_value, table_value in class_casting_data.items():
        class_id = _text(class_value, f"{label}.casting_classes class id")
        table_id = _text(table_value, f"{label}.casting_classes[{class_id!r}]")
        if class_id not in class_tables:
            raise ContentError(f"{label}.casting_classes: names no class table ({class_id!r})")
        if table_id not in casting_tables:
            raise ContentError(
                f"{label}.casting_classes[{class_id!r}]: names no casting table ({table_id!r})"
            )
        casting_class_tables[class_id] = table_id
    unused_casting_tables = set(casting_tables).difference(casting_class_tables.values())
    if unused_casting_tables:
        raise ContentError(
            f"{label}.casting_classes: no class refers to tables {sorted(unused_casting_tables)!r}"
        )

    traditions = _object(_required(definition, "traditions", label), f"{label}.traditions")
    _exact_keys(traditions, ("tags", "effort_stats"), f"{label}.traditions")
    tradition_tags = _text_mapping(
        _required(traditions, "tags", f"{label}.traditions"), f"{label}.traditions.tags"
    )
    tradition_effort_stats = _text_mapping(
        _required(traditions, "effort_stats", f"{label}.traditions"),
        f"{label}.traditions.effort_stats",
    )
    art_data = _object(_required(definition, "art_levels", label), f"{label}.art_levels")
    _exact_keys(art_data, ("full", "partial"), f"{label}.art_levels")
    art_charts: dict[str, dict[str, frozenset[int]]] = {}
    for chart_id in ("full", "partial"):
        chart_label = f"{label}.art_levels.{chart_id}"
        chart_data = _object(_required(art_data, chart_id, f"{label}.art_levels"), chart_label)
        chart: dict[str, frozenset[int]] = {}
        for tag_value, level_values in chart_data.items():
            tag = _text(tag_value, f"{chart_label} tag")
            levels = _integers(level_values, f"{chart_label}[{tag!r}]", minimum=1)
            if (
                not levels
                or len(levels) != len(set(levels))
                or any(level > max_level for level in levels)
            ):
                raise ContentError(
                    f"{chart_label}[{tag!r}]: levels must be unique and between 1 and {max_level}"
                )
            chart[tag] = frozenset(levels)
        if not chart:
            raise ContentError(f"{chart_label}: must contain at least one tradition")
        art_charts[chart_id] = chart

    known_tags = set(art_charts["partial"])
    if tradition_tags.get(_NO_TRADITION) != _NO_TRADITION:
        raise ContentError(f"{label}.traditions.tags: {_NO_TRADITION!r} must map to itself")
    referenced_tags = set(tradition_tags.values()) - {_NO_TRADITION}
    if referenced_tags != known_tags:
        raise ContentError(
            f"{label}.traditions.tags: references must match partial art charts "
            f"(unknown={sorted(referenced_tags - known_tags)!r}, "
            f"unreferenced={sorted(known_tags - referenced_tags)!r})"
        )
    if set(tradition_effort_stats) != known_tags:
        raise ContentError(
            f"{label}.traditions.effort_stats: tags must match partial art charts "
            f"(missing={sorted(known_tags - set(tradition_effort_stats))!r}, "
            f"unknown={sorted(set(tradition_effort_stats) - known_tags)!r})"
        )
    unknown_full_tags = set(art_charts["full"]) - known_tags
    if unknown_full_tags:
        raise ContentError(
            f"{label}.art_levels.full: names unknown traditions {sorted(unknown_full_tags)!r}"
        )

    skill_data = _object(
        _required(definition, "skill_level_minimums", label),
        f"{label}.skill_level_minimums",
    )
    if not skill_data:
        raise ContentError(f"{label}.skill_level_minimums: must contain skill levels")
    skill_level_minimums: dict[int, int] = {}
    for key, minimum_value in skill_data.items():
        if (
            not isinstance(key, str)
            or not key.isascii()
            or not key.isdecimal()
            or str(int(key)) != key
        ):
            raise ContentError(
                f"{label}.skill_level_minimums: keys must be canonical non-negative levels"
            )
        skill_level = int(key)
        minimum = _integer(minimum_value, f"{label}.skill_level_minimums[{key!r}]", minimum=1)
        if minimum > max_level:
            raise ContentError(
                f"{label}.skill_level_minimums[{key!r}]: character level exceeds {max_level}"
            )
        skill_level_minimums[skill_level] = minimum
    skill_levels = sorted(skill_level_minimums)
    if skill_levels != list(range(skill_levels[-1] + 1)):
        raise ContentError(
            f"{label}.skill_level_minimums: skill levels must start at 0 and be consecutive"
        )
    if any(
        previous > current
        for previous, current in pairwise(skill_level_minimums[level] for level in skill_levels)
    ):
        raise ContentError(
            f"{label}.skill_level_minimums: character-level prerequisites must not decrease"
        )

    return WWNProgression(
        max_level=max_level,
        max_skill_level=skill_levels[-1],
        class_tables=MappingProxyType(class_tables),
        xp_fast=xp_fast,
        xp_slow=xp_slow,
        casting_tables=MappingProxyType(casting_tables),
        casting_class_tables=MappingProxyType(casting_class_tables),
        tradition_tags=MappingProxyType(tradition_tags),
        tradition_effort_stats=MappingProxyType(tradition_effort_stats),
        art_levels_full=MappingProxyType(art_charts["full"]),
        art_levels_partial=MappingProxyType(art_charts["partial"]),
        skill_level_minimums=MappingProxyType(skill_level_minimums),
    )


def configure_progression(
    data: Mapping[str, Any], character: CharacterSpec, label: str
) -> WWNProgression:
    """Parse progression data and validate it against the rule set's character definitions."""
    progression = parse_wwn_progression(data, label)
    if not isinstance(character, CharacterSpec):
        raise ContentError(f"{label}: progression configurator needs a CharacterSpec")

    class_ids = {
        template.id for template in character.templates.values() if template.kind == "class"
    }
    missing_classes = sorted(class_ids - set(progression.class_tables))
    if missing_classes:
        raise ContentError(
            f"{label}: class templates have no progression table: {missing_classes!r}"
        )
    unknown_classes = sorted(set(progression.class_tables) - class_ids)
    if unknown_classes:
        raise ContentError(f"{label}: class tables have no class template: {unknown_classes!r}")

    tradition_ids = {
        template.id
        for template in character.templates.values()
        if template.kind in {"tradition", "tradition2"}
    }
    missing_traditions = sorted(tradition_ids - set(progression.tradition_tags))
    if missing_traditions:
        raise ContentError(
            f"{label}: tradition templates have no tag mapping: {missing_traditions!r}"
        )
    valid_tradition_keys = tradition_ids | set(progression.tradition_tags.values())
    unknown_traditions = sorted(set(progression.tradition_tags) - valid_tradition_keys)
    if unknown_traditions:
        raise ContentError(
            f"{label}: tradition mappings name no template or tag: {unknown_traditions!r}"
        )

    missing_effort_stats = sorted(
        set(progression.tradition_effort_stats.values()) - set(character.stats)
    )
    if missing_effort_stats:
        raise ContentError(
            f"{label}: effort mappings name no character stats: {missing_effort_stats!r}"
        )

    skill_progression = character.skill_progression
    if skill_progression is None:
        raise ContentError(f"{label}: progression requires a skill_progression definition")
    chart_minimum = min(progression.skill_level_minimums)
    chart_maximum = max(progression.skill_level_minimums)
    if skill_progression.first_level != chart_minimum or skill_progression.maximum != chart_maximum:
        raise ContentError(
            f"{label}: skill chart bounds {chart_minimum}..{chart_maximum} do not match "
            f"skill_progression {skill_progression.first_level}..{skill_progression.maximum}"
        )

    level_counter = character.counters.get("level")
    if (
        level_counter is not None
        and level_counter.maximum is not None
        and level_counter.maximum != progression.max_level
    ):
        raise ContentError(
            f"{label}: progression maximum level {progression.max_level} does not match "
            f"the level counter maximum {level_counter.maximum}"
        )
    return progression


def require_wwn_progression(world: _WorldWithRuleData) -> WWNProgression:
    """Resolve validated progression data from a world without keeping a module-level copy."""
    try:
        rule_data = world.rule_data
    except AttributeError as exc:
        raise ContentError("WWN world has no rule_data mapping") from exc
    if not isinstance(rule_data, Mapping):
        raise ContentError("WWN world rule_data must be a mapping")
    if "progression" not in rule_data:
        raise ContentError("WWN world is missing rule_data['progression']")
    progression = rule_data["progression"]
    if not isinstance(progression, WWNProgression):
        raise ContentError(
            "WWN world rule_data['progression'] must be a WWNProgression "
            f"(got {type(progression).__name__})"
        )
    return progression


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContentError(f"{label}: must be an object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ContentError(f"{label}: must be a list")
    return value


def _required(data: Mapping[str, Any], key: str, label: str) -> Any:
    if key not in data:
        raise ContentError(f"{label}: missing required field {key!r}")
    return data[key]


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContentError(f"{label}: must be a non-empty string")
    return value


def _integer(value: Any, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContentError(f"{label}: must be a whole number")
    if minimum is not None and value < minimum:
        raise ContentError(f"{label}: must be at least {minimum}")
    return value


def _integers(value: Any, label: str, *, minimum: int | None = None) -> tuple[int, ...]:
    items = _array(value, label)
    return tuple(
        _integer(item, f"{label}[{index}]", minimum=minimum) for index, item in enumerate(items)
    )


def _exact_keys(data: Mapping[str, Any], expected: tuple[str, ...], label: str) -> None:
    missing = [key for key in expected if key not in data]
    extra = [key for key in data if key not in expected]
    if missing:
        raise ContentError(f"{label}: missing {', '.join(repr(key) for key in missing)}")
    if extra:
        raise ContentError(f"{label}: unexpected fields {', '.join(repr(key) for key in extra)}")


def _text_mapping(value: Any, label: str) -> dict[str, str]:
    data = _object(value, label)
    if not data:
        raise ContentError(f"{label}: must contain at least one entry")
    result: dict[str, str] = {}
    for key_value, text_value in data.items():
        key = _text(key_value, f"{label} key")
        result[key] = _text(text_value, f"{label}[{key!r}]")
    return result
