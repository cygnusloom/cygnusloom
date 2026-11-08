"""Typed, validated data for Worlds Without Number numeric progression charts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from types import MappingProxyType
from typing import Any, Protocol

from cygnusloom.content.character import CharacterSpec
from cygnusloom.content.definitions import ContentError

_CASTING_TABLES = ("full_mage", "partial_mage", "dual_mage")
_NO_TRADITION = "no_tradition"
_CLASS_KINDS = frozenset({"class", "class2"})
_PROGRESSION_FIELDS = (
    "type",
    "id",
    "python_module",
    "configure",
    "classes",
    "class_pairing",
    "xp",
    "casting",
    "casting_classes",
    "traditions",
    "art_levels",
    "skill_costs",
    "skill_minimums",
    "attribute_costs",
    "attribute_minimums",
    "attribute_maximum",
)
# How two partial class charts combine into an Adventurer's chart, and the policies each field
# accepts. The data names one policy per field.
_CLASS_PAIRING_POLICIES: Mapping[str, tuple[str, ...]] = {
    "hit_die": ("best",),
    "hp_constant": ("best",),
    "attack": ("best",),
    "focus_levels": ("union",),
    "quick_learner": ("any",),
}


class _WorldWithRuleData(Protocol):
    @property
    def rule_data(self) -> object: ...


@dataclass(frozen=True)
class ClassTable:
    """One class's hit die, attack bonuses, focus levels, and skill-point modifier.

    A class may also grant a magical tradition of its own and choose which art chart it uses.
    """

    hit_die: int
    hp_constant: int
    attack: tuple[int, ...]
    focus_levels: frozenset[int]
    quick_learner: bool
    # The tradition tag this class grants, such as "healer". Empty when the class grants none.
    tradition: str = ""
    # The art chart the class uses: "full" for a full Mage, "partial" for every other class.
    art_chart: str = "partial"


@dataclass(frozen=True)
class ClassPairing:
    """How two class charts combine, as the progression data configures it.

    Each field names the policy the data chose: ``best`` takes the higher number, ``union``
    keeps every focus level from either chart, and ``any`` is true when either chart is.
    """

    hit_die: str
    hp_constant: str
    attack: str
    focus_levels: str
    quick_learner: str


@dataclass(frozen=True)
class CastingRow:
    """A spellcaster's spell limits at one character level."""

    max_spell_level: int
    casts: int
    prepared: int


@dataclass(frozen=True)
class CastingTable:
    """One casting chart's rows and the spell picks its class gains at each level."""

    rows: Mapping[int, CastingRow]
    # The spells a caster of this class learns when a new spell level opens.
    spell_picks: int
    # Whether the class is a full caster, which the rules use for the harder spell progression.
    full_caster: bool

    def __getitem__(self, level: int) -> CastingRow:
        return self.rows[level]

    def __len__(self) -> int:
        return len(self.rows)

    def get(self, level: int, default: CastingRow | None = None) -> CastingRow | None:
        return self.rows.get(level, default)


@dataclass(frozen=True)
class WWNProgression:
    """Immutable WWN progression charts parsed from a ``rule_data/progression`` definition."""

    max_level: int
    max_skill_level: int
    class_tables: Mapping[str, ClassTable]
    class_pairing: ClassPairing
    xp: Mapping[str, tuple[int, ...]]
    casting_tables: Mapping[str, CastingTable]
    casting_class_tables: Mapping[str, str]
    tradition_tags: Mapping[str, str]
    tradition_effort_stats: Mapping[str, str]
    art_levels_full: Mapping[str, frozenset[int]]
    art_levels_partial: Mapping[str, frozenset[int]]
    skill_costs: Mapping[int, int]
    skill_minimums: Mapping[int, int]
    attribute_costs: Mapping[int, int]
    attribute_minimums: Mapping[int, int]
    attribute_maximum: int

    @property
    def full_mage_classes(self) -> frozenset[str]:
        return frozenset(
            class_id
            for class_id, table_id in self.casting_class_tables.items()
            if self.casting_tables[table_id].full_caster
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

    xp_data = _object(_required(definition, "xp", label), f"{label}.xp")
    if not xp_data:
        raise ContentError(f"{label}.xp: must name at least one experience track")
    xp: dict[str, tuple[int, ...]] = {}
    for track_name, values in xp_data.items():
        if not isinstance(track_name, str) or not track_name.strip():
            raise ContentError(f"{label}.xp: a track name must be non-empty text")
        track = _integers(values, f"{label}.xp.{track_name}", minimum=0)
        if not track:
            raise ContentError(f"{label}.xp.{track_name}: must list at least one level")
        if track[0] != 0:
            raise ContentError(f"{label}.xp.{track_name}: must start at zero for level 1")
        if any(left >= right for left, right in pairwise(track)):
            raise ContentError(f"{label}.xp.{track_name}: thresholds must increase at every level")
        xp[track_name.strip()] = track
    level_counts = {len(track) for track in xp.values()}
    if len(level_counts) != 1:
        raise ContentError(f"{label}.xp: every track must list the same number of levels")
    max_level = level_counts.pop()

    classes = _array(_required(definition, "classes", label), f"{label}.classes")
    if not classes:
        raise ContentError(f"{label}.classes: must contain at least one class")
    class_tables: dict[str, ClassTable] = {}
    for index, class_value in enumerate(classes):
        class_label = f"{label}.classes[{index}]"
        class_data = _object(class_value, class_label)
        unknown = set(class_data) - {
            "id",
            "hit_die",
            "hp_constant",
            "attack",
            "focus_levels",
            "quick_learner",
            "tradition",
            "art_chart",
        }
        if unknown:
            raise ContentError(
                f"{class_label}: unexpected fields {', '.join(repr(key) for key in sorted(unknown))}"
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
        tradition = class_data.get("tradition", "")
        if not isinstance(tradition, str):
            raise ContentError(f"{class_label}.tradition: must be text")
        tradition = tradition.strip()
        art_chart = class_data.get("art_chart", "partial")
        if art_chart not in ("full", "partial"):
            raise ContentError(f"{class_label}.art_chart: must be 'full' or 'partial'")
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
            tradition=tradition,
            art_chart=art_chart,
        )

    pairing_label = f"{label}.class_pairing"
    pairing_data = _object(_required(definition, "class_pairing", label), pairing_label)
    _exact_keys(pairing_data, tuple(_CLASS_PAIRING_POLICIES), pairing_label)
    class_pairing = ClassPairing(
        **{
            field: _pairing_policy(
                _required(pairing_data, field, pairing_label), field, pairing_label
            )
            for field in _CLASS_PAIRING_POLICIES
        }
    )

    casting_data = _object(_required(definition, "casting", label), f"{label}.casting")
    _exact_keys(casting_data, _CASTING_TABLES, f"{label}.casting")
    casting_tables: dict[str, CastingTable] = {}
    for table_id in _CASTING_TABLES:
        table_label = f"{label}.casting.{table_id}"
        table_data = _object(_required(casting_data, table_id, f"{label}.casting"), table_label)
        _exact_keys(table_data, ("rows", "spell_picks", "full_caster"), table_label)
        rows = _array(_required(table_data, "rows", table_label), f"{table_label}.rows")
        if len(rows) != max_level:
            raise ContentError(f"{table_label}: has {len(rows)} levels; expected {max_level}")
        table: dict[int, CastingRow] = {}
        for expected_level, row_value in enumerate(rows, start=1):
            row_label = f"{table_label}.rows[{expected_level - 1}]"
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
        full_caster = _required(table_data, "full_caster", table_label)
        if not isinstance(full_caster, bool):
            raise ContentError(f"{table_label}.full_caster: must be true or false")
        casting_tables[table_id] = CastingTable(
            rows=MappingProxyType(table),
            spell_picks=_integer(
                _required(table_data, "spell_picks", table_label),
                f"{table_label}.spell_picks",
                minimum=0,
            ),
            full_caster=full_caster,
        )

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

    skill_costs = _keyed_integers(
        _required(definition, "skill_costs", label),
        f"{label}.skill_costs",
        first=0,
        maximum=max_level,
    )
    skill_minimums = _keyed_integers(
        _required(definition, "skill_minimums", label),
        f"{label}.skill_minimums",
        first=0,
        maximum=max_level,
    )
    if set(skill_costs) != set(skill_minimums):
        raise ContentError(f"{label}: skill_costs and skill_minimums must name the same levels")
    # A character level is a prerequisite, so it cannot fall as the skill level rises.
    skill_levels = sorted(skill_minimums)
    if any(
        previous > current
        for previous, current in pairwise(skill_minimums[level] for level in skill_levels)
    ):
        raise ContentError(
            f"{label}.skill_minimums: character-level prerequisites must not decrease"
        )
    attribute_costs = _keyed_integers(
        _required(definition, "attribute_costs", label),
        f"{label}.attribute_costs",
        first=1,
        maximum=max_level,
    )
    attribute_minimums = _keyed_integers(
        _required(definition, "attribute_minimums", label),
        f"{label}.attribute_minimums",
        first=1,
        maximum=max_level,
    )
    if set(attribute_costs) != set(attribute_minimums):
        raise ContentError(
            f"{label}: attribute_costs and attribute_minimums must name the same boosts"
        )
    attribute_maximum = _integer(
        _required(definition, "attribute_maximum", label),
        f"{label}.attribute_maximum",
        minimum=1,
    )
    known_class_traditions = {table.tradition for table in class_tables.values() if table.tradition}
    unknown_class_traditions = sorted(
        tradition
        for tradition in known_class_traditions
        if tradition_tags.get(tradition, tradition) not in known_tags
    )
    if unknown_class_traditions:
        raise ContentError(
            f"{label}.classes: traditions name no art chart {unknown_class_traditions!r}"
        )

    return WWNProgression(
        max_level=max_level,
        max_skill_level=skill_levels[-1],
        class_tables=MappingProxyType(class_tables),
        class_pairing=class_pairing,
        xp=MappingProxyType(xp),
        casting_tables=MappingProxyType(casting_tables),
        casting_class_tables=MappingProxyType(casting_class_tables),
        tradition_tags=MappingProxyType(tradition_tags),
        tradition_effort_stats=MappingProxyType(tradition_effort_stats),
        art_levels_full=MappingProxyType(art_charts["full"]),
        art_levels_partial=MappingProxyType(art_charts["partial"]),
        skill_costs=MappingProxyType(skill_costs),
        skill_minimums=MappingProxyType(skill_minimums),
        attribute_costs=MappingProxyType(attribute_costs),
        attribute_minimums=MappingProxyType(attribute_minimums),
        attribute_maximum=attribute_maximum,
    )


def configure_progression(
    data: Mapping[str, Any], character: CharacterSpec, label: str
) -> WWNProgression:
    """Parse progression data and validate it against the rule set's character definitions."""
    progression = parse_wwn_progression(data, label)
    if not isinstance(character, CharacterSpec):
        raise ContentError(f"{label}: progression configurator needs a CharacterSpec")

    class_ids = {
        template.id for template in character.templates.values() if template.kind in _CLASS_KINDS
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
    chart_minimum = min(progression.skill_minimums)
    chart_maximum = max(progression.skill_minimums)
    if skill_progression.first_level != chart_minimum or skill_progression.maximum != chart_maximum:
        raise ContentError(
            f"{label}: skill chart bounds {chart_minimum}..{chart_maximum} do not match "
            f"skill_progression {skill_progression.first_level}..{skill_progression.maximum}"
        )
    _check_class_attack_encoding(progression, character, label)

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


def combine_class_tables(tables: Sequence[ClassTable], policy: ClassPairing) -> ClassTable:
    """Merge class charts into one, following the progression's parsed pairing policy."""
    if not tables:
        raise ContentError("class pairing: needs at least one class table")
    levels = len(tables[0].attack)
    if any(len(table.attack) != levels for table in tables):
        raise ContentError("class pairing: attack charts must have the same number of levels")
    return ClassTable(
        hit_die=_paired_number(policy.hit_die, "hit_die", [table.hit_die for table in tables]),
        hp_constant=_paired_number(
            policy.hp_constant, "hp_constant", [table.hp_constant for table in tables]
        ),
        attack=tuple(
            _paired_number(policy.attack, "attack", [table.attack[level] for table in tables])
            for level in range(levels)
        ),
        focus_levels=_paired_levels(policy.focus_levels, tables),
        quick_learner=_paired_flag(policy.quick_learner, tables),
        tradition=next((table.tradition for table in tables if table.tradition), ""),
        art_chart="full" if any(table.art_chart == "full" for table in tables) else "partial",
    )


def _check_class_attack_encoding(
    progression: WWNProgression, character: CharacterSpec, label: str
) -> None:
    """Creation adds a template's ``attack_bonus`` to the stat's base; it must match the chart.

    The class charts are the authority. A template delta that disagrees with the chart's level-1
    attack would make a newly created character differ from the same character after a level-up,
    so it is rejected at load time.
    """
    attack = character.stats.get("attack_bonus")
    if attack is None or attack.derived is not None:
        return
    base = attack.low
    for class_id, table in progression.class_tables.items():
        template = character.templates.get(class_id)
        if template is None or template.kind != "class":
            continue
        actual = base + template.stats.get("attack_bonus", 0)
        if actual != table.attack[0]:
            raise ContentError(
                f"{label}: class {class_id!r} starts at attack bonus {actual} from its template; "
                f"the chart's level-1 attack is {table.attack[0]}"
            )
    for primary_id, second_id in _class_pair_ids(character, progression):
        primary = character.templates[primary_id]
        second = character.templates[second_id]
        combined = combine_class_tables(
            [progression.class_tables[primary_id], progression.class_tables[second_id]],
            progression.class_pairing,
        )
        actual = base + primary.stats.get("attack_bonus", 0) + second.stats.get("attack_bonus", 0)
        if actual != combined.attack[0]:
            raise ContentError(
                f"{label}: the {primary_id}/{second_id} pairing starts at attack bonus {actual}; "
                f"the combined chart's level-1 attack is {combined.attack[0]}"
            )


def _class_pair_ids(character: CharacterSpec, progression: WWNProgression) -> list[tuple[str, str]]:
    """Every class/class2 pairing the templates allow, for chart cross-checks."""
    primaries = [
        template.id
        for template in character.templates.values()
        if template.kind == "class" and template.id in progression.class_tables
    ]
    seconds = [
        template.id
        for template in character.templates.values()
        if template.kind == "class2" and template.id in progression.class_tables
    ]
    pairs: list[tuple[str, str]] = []
    for primary_id in primaries:
        primary = character.templates[primary_id]
        for second_id in seconds:
            second = character.templates[second_id]
            allowed = second.compatible_with.get("class")
            if allowed is not None and primary_id not in allowed:
                continue
            required = primary.compatible_with.get("class2")
            if required is not None and second_id not in required:
                continue
            if _duplicate_choice_keys(primary_id, second_id, character):
                continue
            pairs.append((primary_id, second_id))
    return pairs


def _duplicate_choice_keys(primary_id: str, second_id: str, character: CharacterSpec) -> bool:
    first = character.templates[primary_id]
    second = character.templates[second_id]
    for template in (first, second):
        for requirement in template.template_requirements:
            if (
                requirement.distinct
                and set(requirement.kinds) == {"class", "class2"}
                and first.choice_key == second.choice_key
            ):
                return True
    return False


def _paired_number(policy: str, field: str, values: Sequence[int]) -> int:
    if policy == "best":
        return max(values)
    raise _unknown_pairing(field, policy)


def _paired_levels(policy: str, tables: Sequence[ClassTable]) -> frozenset[int]:
    if policy == "union":
        return frozenset().union(*(table.focus_levels for table in tables))
    raise _unknown_pairing("focus_levels", policy)


def _paired_flag(policy: str, tables: Sequence[ClassTable]) -> bool:
    if policy == "any":
        return any(table.quick_learner for table in tables)
    raise _unknown_pairing("quick_learner", policy)


def _unknown_pairing(field: str, policy: str) -> ContentError:
    return ContentError(f"class pairing: {field} policy {policy!r} is not supported")


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


def _pairing_policy(value: Any, field: str, label: str) -> str:
    """One class-pairing policy: text naming a policy the field accepts."""
    policy = _text(value, f"{label}.{field}")
    allowed = _CLASS_PAIRING_POLICIES[field]
    if policy not in allowed:
        options = ", ".join(repr(option) for option in allowed)
        raise ContentError(f"{label}.{field}: must be one of {options}")
    return policy


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


def _keyed_integers(value: Any, label: str, *, first: int, maximum: int) -> dict[int, int]:
    """A map keyed by consecutive step numbers, such as a skill level or boost number."""
    data = _object(value, label)
    if not data:
        raise ContentError(f"{label}: must contain at least one entry")
    result: dict[int, int] = {}
    for key, raw in data.items():
        if (
            not isinstance(key, str)
            or not key.isascii()
            or not key.isdecimal()
            or str(int(key)) != key
        ):
            raise ContentError(f"{label}: keys must be canonical whole numbers")
        step = int(key)
        if step < first:
            raise ContentError(f"{label}: keys must start at {first}")
        number = _integer(raw, f"{label}[{key!r}]", minimum=1)
        if number > maximum:
            raise ContentError(f"{label}[{key!r}]: value exceeds {maximum}")
        result[step] = number
    steps = sorted(result)
    if steps != list(range(first, steps[-1] + 1)):
        raise ContentError(f"{label}: keys must start at {first} and be consecutive")
    return result
