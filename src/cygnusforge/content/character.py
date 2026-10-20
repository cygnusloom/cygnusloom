"""Character options a rule set defines, and the JSON schema derived from them.

A character has four kinds of option. Stats are numbers rolled with a dice formula. Templates,
such as a class or background, are chosen by kind and grant features. Features are items,
traits, spells, or feats. Fields are text, or a choice from a table. The core rule set defines
the options every rule set shares, such as appearance and biography."""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cygnusforge.content.definitions import ContentError, Definition, required_text
from cygnusforge.rules.dice import FORMULA, is_formula, roll_total

FEATURE_TYPES = ("item", "trait", "spell", "feat")


@dataclass(frozen=True)
class TableOption:
    """An entry in a table. A named entry has a `name` the player reads as a title, above its
    `description`. Other entries are a description only. A character stores the `value`: the
    name when there is one, otherwise the description."""

    name: str | None
    description: str

    @property
    def value(self) -> str:
        return self.name or self.description


@dataclass(frozen=True)
class Stat:
    id: str
    name: str
    formula: str
    # Stats that share a swap group may trade values: one swap per group at creation.
    swap_group: str | None
    low: int
    high: int


@dataclass(frozen=True)
class Feature:
    id: str
    name: str
    feature_type: str
    description: str
    bulky: bool
    petty: bool


@dataclass(frozen=True)
class Granted:
    """A feature a template gives, with its count: a whole number or a dice formula."""

    feature: str
    quantity: str


@dataclass(frozen=True)
class Template:
    id: str
    kind: str
    name: str
    description: str
    features: tuple[Granted, ...]
    tables: tuple[str, ...]
    # Suggested character names for this template. They feed the name dice, not the schema.
    names: tuple[str, ...]


@dataclass(frozen=True)
class Field:
    """A text field when `table` is None, otherwise a choice from that table."""

    id: str
    name: str
    description: str
    table: str | None


@dataclass(frozen=True)
class CharacterSpec:
    stats: Mapping[str, Stat]
    templates: Mapping[str, Template]
    features: Mapping[str, Feature]
    fields: Mapping[str, Field]
    tables: Mapping[str, tuple[TableOption, ...]]
    table_names: Mapping[str, str]

    def kinds(self) -> list[str]:
        return sorted({t.kind for t in self.templates.values()})


def build_character(
    definitions: list[Definition],
    tables: Mapping[str, tuple[TableOption, ...]],
    table_names: Mapping[str, str],
    template: str,
) -> CharacterSpec:
    """Build the character spec from a rule set's definitions. `tables` are already loaded."""
    stats = _each(definitions, "stat", lambda d: _stat(d, template))
    features = _each(definitions, "feature", lambda d: _feature(d, template))
    fields = _each(definitions, "character_field", lambda d: _field(d, tables, template))
    templates = _each(definitions, "template", lambda d: _template(d, features, tables, template))
    field_ids = set(fields)
    for item in templates.values():
        for table in item.tables:
            if table in field_ids:
                raise ContentError(
                    f"{template}: template {item.id!r} table {table!r} clashes with a field"
                )
    return CharacterSpec(
        stats=stats,
        templates=templates,
        features=features,
        fields=fields,
        tables=tables,
        table_names=table_names,
    )


def _each(definitions: list[Definition], kind: str, make: Any) -> dict[str, Any]:
    return {d.id: make(d) for d in definitions if d.type == kind}


def _stat(definition: Definition, template: str) -> Stat:
    label = f"{template}: stat {definition.id}"
    formula = required_text(definition.data, "formula", label)
    match = FORMULA.fullmatch(formula)
    if match is None:
        raise ContentError(f"{label}: 'formula' must be a dice formula such as '3d6'")
    swap = definition.data.get("swap_group")
    if swap is not None and (not isinstance(swap, str) or not swap.strip()):
        raise ContentError(f"{label}: 'swap_group' must be non-empty text")
    count, sides = int(match.group(1)), int(match.group(2))
    return Stat(
        id=definition.id,
        name=required_text(definition.data, "name", label),
        formula=formula,
        swap_group=swap,
        low=count,
        high=count * sides,
    )


def _feature(definition: Definition, template: str) -> Feature:
    label = f"{template}: feature {definition.id}"
    data = definition.data
    kind = required_text(data, "feature_type", label)
    if kind not in FEATURE_TYPES:
        raise ContentError(f"{label}: 'feature_type' must be one of {', '.join(FEATURE_TYPES)}")
    description = data.get("description", "")
    if not isinstance(description, str):
        raise ContentError(f"{label}: 'description' must be text")
    bulky = data.get("bulky", False)
    if not isinstance(bulky, bool):
        raise ContentError(f"{label}: 'bulky' must be true or false")
    petty = data.get("petty", False)
    if not isinstance(petty, bool):
        raise ContentError(f"{label}: 'petty' must be true or false")
    return Feature(
        id=definition.id,
        name=required_text(data, "name", label),
        feature_type=kind,
        description=description,
        bulky=bulky,
        petty=petty,
    )


def _field(definition: Definition, tables: Mapping[str, object], template: str) -> Field:
    label = f"{template}: character_field {definition.id}"
    table = definition.data.get("table")
    if table is not None:
        table = required_text(definition.data, "table", label)
        if table not in tables:
            raise ContentError(f"{label}: unknown table {table!r}")
    description = definition.data.get("description", "")
    if not isinstance(description, str):
        raise ContentError(f"{label}: 'description' must be text")
    return Field(
        id=definition.id,
        name=required_text(definition.data, "name", label),
        description=description,
        table=table,
    )


def _template(
    definition: Definition,
    features: Mapping[str, Feature],
    tables: Mapping[str, Sequence[TableOption]],
    template: str,
) -> Template:
    label = f"{template}: template {definition.id}"
    data = definition.data
    granted = data.get("features", [])
    if not isinstance(granted, list):
        raise ContentError(f"{label}: 'features' must be a list")
    grants = tuple(_granted(item, features, label) for item in granted)
    table_ids = data.get("tables", [])
    if not isinstance(table_ids, list) or not all(isinstance(t, str) for t in table_ids):
        raise ContentError(f"{label}: 'tables' must be a list of table ids")
    for table in table_ids:
        if table not in tables:
            raise ContentError(f"{label}: unknown table {table!r}")
    names = _names(data.get("names"), tables, label)
    return Template(
        id=definition.id,
        kind=required_text(data, "kind", label),
        name=required_text(data, "name", label),
        description=required_text(data, "description", label),
        features=grants,
        tables=tuple(table_ids),
        names=names,
    )


def _names(
    table: object, tables: Mapping[str, Sequence[TableOption]], label: str
) -> tuple[str, ...]:
    """The names a template suggests, from a table of plain names. The table is not a choice."""
    if table is None:
        return ()
    if not isinstance(table, str) or table not in tables:
        raise ContentError(f"{label}: 'names' must be the id of a table")
    return tuple(option.value for option in tables[table])


def _granted(item: object, features: Mapping[str, Feature], label: str) -> Granted:
    if not isinstance(item, dict):
        raise ContentError(f"{label}: each feature must be an object")
    feature = required_text(item, "feature", label)
    if feature not in features:
        raise ContentError(f"{label}: unknown feature {feature!r}")
    quantity = item.get("quantity", "1")
    if isinstance(quantity, int) and not isinstance(quantity, bool):
        quantity = str(quantity)
    if not isinstance(quantity, str) or not (_is_count(quantity) or is_formula(quantity)):
        raise ContentError(f"{label}: 'quantity' must be a whole number or a dice formula")
    return Granted(feature=feature, quantity=quantity)


def _is_count(text: str) -> bool:
    return text.isdigit() and text[0] != "0"


def character_schema(spec: CharacterSpec) -> dict[str, Any]:
    """The JSON schema for a character's options. Every part is optional: a GM may give any
    combination, and anything left out is rolled or left blank."""
    properties: dict[str, Any] = {}
    if spec.stats:
        properties["stats"] = _object(
            {
                s.id: {
                    "type": "integer",
                    "minimum": s.low,
                    "maximum": s.high,
                    "description": s.name,
                }
                for s in spec.stats.values()
            }
        )
    if spec.templates:
        properties["templates"] = _object(
            {
                kind: {
                    "type": "string",
                    "enum": [t.id for t in spec.templates.values() if t.kind == kind],
                    "description": f"The {kind} of the character",
                }
                for kind in spec.kinds()
            }
        )
    if spec.features:
        properties["features"] = {
            "type": "array",
            "items": {"type": "string", "enum": list(spec.features)},
            "uniqueItems": True,
            "description": "Features the character has besides any a template grants",
        }
    fields = {f.id: _field_schema(f, spec.tables) for f in spec.fields.values()}
    for template in spec.templates.values():
        for table in template.tables:
            fields[table] = _choice_schema(spec.table_names.get(table, table), spec.tables[table])
    properties["fields"] = _object(fields)
    return {
        "type": "object",
        "description": "Character options. Anything left out is rolled or left blank.",
        "properties": properties,
        "additionalProperties": False,
    }


def _field_schema(field: Field, tables: Mapping[str, Sequence[TableOption]]) -> dict[str, Any]:
    description = field.description or field.name
    if field.table is None:
        return {"type": "string", "description": description}
    return _choice_schema(description, tables[field.table])


def _choice_schema(description: str, options: Sequence[TableOption]) -> dict[str, Any]:
    """A choice from a table. When some options are named, each is listed with its description,
    so the GM can choose by what the option does."""
    schema: dict[str, Any] = {
        "type": "string",
        "enum": [option.value for option in options],
        "description": description,
    }
    if any(option.name is not None for option in options):
        lines = [
            f"- {option.name}: {option.description}"
            if option.name is not None
            else f"- {option.description}"
            for option in options
        ]
        schema["description"] = description + "\n" + "\n".join(lines)
    return schema


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "additionalProperties": False}


NO_CHARACTER = CharacterSpec(
    stats={}, templates={}, features={}, fields={}, tables={}, table_names={}
)


class CharacterError(ValueError):
    """A character a player or the GM chose breaks a rule the spec sets."""


@dataclass(frozen=True)
class Sheet:
    """A character with every option resolved. This is what an event stores."""

    stats: dict[str, int]
    templates: dict[str, str]
    # Feature id to how many the character has.
    features: dict[str, int]
    fields: dict[str, str]


def roll_stats(spec: CharacterSpec, rng: random.Random) -> dict[str, int]:
    return {s.id: roll_total(s.formula, rng) for s in spec.stats.values()}


def check_swaps(spec: CharacterSpec, rolled: Mapping[str, int], chosen: Mapping[str, int]) -> None:
    """A player keeps the rolled stats, except that each swap group may swap one pair of values.
    Stats outside a group must keep their rolled value."""
    groups: dict[str | None, list[str]] = {}
    for stat in spec.stats.values():
        groups.setdefault(stat.swap_group, []).append(stat.id)
    for group, ids in groups.items():
        before = [rolled[i] for i in ids]
        after = [chosen[i] for i in ids]
        if group is None:
            if before != after:
                names = ", ".join(ids)
                raise CharacterError(f"stats {names} cannot be changed from their rolled values")
            continue
        if sorted(before) != sorted(after):
            raise CharacterError(f"stats in group {group!r} must be the rolled values, reordered")
        if sum(b != a for b, a in zip(before, after)) > 2:
            raise CharacterError(f"stats in group {group!r} allow one swap")


def resolve(spec: CharacterSpec, submitted: Mapping[str, Any], rng: random.Random) -> Sheet:
    """Fill in what a GM or player left out, and roll what is random. `submitted` must already
    match `character_schema(spec)`. Stats left out are rolled; text left out is blank; a table
    left out is rolled; a template's features are added with their rolled counts."""
    given_stats = submitted.get("stats", {})
    given_templates = submitted.get("templates", {})
    given_features = submitted.get("features", [])
    given_fields = submitted.get("fields", {})
    stats = {
        s.id: given_stats[s.id] if s.id in given_stats else roll_total(s.formula, rng)
        for s in spec.stats.values()
    }
    templates = dict(given_templates)
    features = {feature: 1 for feature in given_features}
    fields: dict[str, str] = {}
    for template_id in templates.values():
        template = spec.templates[template_id]
        for granted in template.features:
            count = (
                int(granted.quantity)
                if granted.quantity.isdigit()
                else roll_total(granted.quantity, rng)
            )
            features[granted.feature] = features.get(granted.feature, 0) + count
        for table in template.tables:
            fields[table] = given_fields.get(table) or rng.choice(spec.tables[table]).value
    for field_spec in spec.fields.values():
        if field_spec.table is None:
            fields[field_spec.id] = given_fields.get(field_spec.id, "")
        elif field_spec.id in given_fields:
            fields[field_spec.id] = given_fields[field_spec.id]
        else:
            fields[field_spec.id] = rng.choice(spec.tables[field_spec.table]).value
    return Sheet(stats=stats, templates=templates, features=features, fields=fields)


def describe(spec: CharacterSpec, sheet: Sheet) -> str:
    """The character as the GM and the player read it, one line per part. Empty parts are left
    out."""
    lines: list[str] = []
    if sheet.stats:
        lines.append(
            "Stats: " + ", ".join(f"{spec.stats[s].name} {v}" for s, v in sheet.stats.items())
        )
    for kind in spec.kinds():
        if kind in sheet.templates:
            template = spec.templates[sheet.templates[kind]]
            lines.append(f"{kind.capitalize()}: {template.name}")
    if sheet.features:
        parts = []
        for feature, count in sheet.features.items():
            name = spec.features[feature].name
            parts.append(name if count == 1 else f"{name} x{count}")
        lines.append("Features: " + ", ".join(parts))
    for field_id, value in sheet.fields.items():
        if not value:
            continue
        if field_id in spec.fields:
            label = spec.fields[field_id].name
        else:
            label = spec.table_names.get(field_id, field_id)
        lines.append(f"{label}: {value}")
    return "\n".join(lines)
