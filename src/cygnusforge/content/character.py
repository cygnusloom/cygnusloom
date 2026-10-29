"""Character options a rule set defines, and the JSON schema derived from them.

A character has four kinds of option. Stats are numbers rolled with a dice formula, or assigned
from a fixed pool of values shared by their swap group. Templates,
such as a class or background, are chosen by kind and grant features. Features are items,
traits, spells, or feats. Fields are text, or a choice from a table. The core rule set defines
the options every rule set shares, such as appearance and biography."""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
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
    # The group an option belongs to, such as "core". Fields can roll by group or restrict groups.
    group: str | None = None

    @property
    def value(self) -> str:
        return self.name or self.description


@dataclass(frozen=True)
class Stat:
    id: str
    name: str
    # The dice a stat is rolled with, or None when it is assigned from `values`.
    formula: str | None
    # Stats that share a swap group may trade values. A rolled group allows one swap; a group
    # with `values` may be assigned in any order.
    swap_group: str | None
    low: int
    high: int
    # The pool a stat with no formula is assigned from. Every stat in its group shares the pool.
    values: tuple[int, ...] = ()


@dataclass(frozen=True)
class Feature:
    """A feature a character can have. An `item` is carried as an inventory item of `size`, with
    `uses` if it is consumed and `armor` added to the character's armor while carried. A `spell`
    is a power the character can cast; how it is cast, and from what, is set by the rule set. A
    feature with no `uses` has none to spend."""

    id: str
    name: str
    feature_type: str
    description: str
    size: str
    uses: int
    armor: int
    # The weapon's damage dice, such as "1d6", or "1d6+1d6" for two weapons (keep the higher). Empty when the feature is not a weapon.
    damage: str = ""
    # The ids of rules attached to this feature. Their text reaches the GM while the item is carried.
    rules: tuple[str, ...] = ()
    # The rule set's label for the feature, such as the Year a spell belongs to. A template's
    # `choose` picks features by it.
    tag: str = ""


@dataclass(frozen=True)
class Granted:
    """A feature a template gives, with its count: a whole number or a dice formula."""

    feature: str
    quantity: str


@dataclass(frozen=True)
class Choose:
    """Features a template lets the character pick: `count` of `feature_type` whose tag is the
    value of the field `tag_field` or an earlier option of that field's table. A pick left out is
    rolled from the same pool."""

    count: int
    feature_type: str
    tag_field: str


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
    # Bonuses added to stats, by stat id, when the template is chosen.
    stats: Mapping[str, int] = field(default_factory=dict)
    # Amounts added to counters' starting values, by counter id, when the template is chosen.
    counters: Mapping[str, int] = field(default_factory=dict)
    # Features the character picks when the template is chosen.
    choose: Choose | None = None


@dataclass(frozen=True)
class Restriction:
    """Limits a field's options to `groups` while the field `field_id` takes one of `values`."""

    field_id: str
    values: tuple[str, ...]
    groups: tuple[str, ...]


@dataclass(frozen=True)
class Field:
    """A text field when `table` is None, otherwise a choice from that table. A `group_roll` field
    rolls a group first and then an option within it. A `restriction` limits its options. A `knows`
    field names a feature type: a character knows every feature of that type whose tag is an
    earlier option of the table than the field's value."""

    id: str
    name: str
    description: str
    table: str | None
    group_roll: bool = False
    restriction: Restriction | None = None
    knows: str | None = None


@dataclass(frozen=True)
class CounterRef:
    """A counter a template can give a starting value to, with the bounds it keeps."""

    id: str
    label: str
    initial: int
    minimum: int | None
    maximum: int | None


@dataclass(frozen=True)
class CharacterSpec:
    stats: Mapping[str, Stat]
    templates: Mapping[str, Template]
    features: Mapping[str, Feature]
    fields: Mapping[str, Field]
    tables: Mapping[str, tuple[TableOption, ...]]
    table_names: Mapping[str, str]
    counters: Mapping[str, CounterRef] = field(default_factory=dict)

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
    _check_groups(stats, template)
    features = _each(definitions, "feature", lambda d: load_feature(d, template))
    fields = _each(
        definitions, "character_field", lambda d: _field(d, tables, "character_field", template)
    )
    templates = _each(definitions, "template", lambda d: _template(d, features, tables, template))
    field_ids = set(fields)
    for item in templates.values():
        for table in item.tables:
            if table in field_ids:
                raise ContentError(
                    f"{template}: template {item.id!r} table {table!r} clashes with a field"
                )
    _check_fields(fields, tables, template)
    _check_pick_fields(templates, fields, template)
    feature_types = {feature.feature_type for feature in features.values()}
    for item in fields.values():
        if item.knows is not None and item.knows not in feature_types:
            raise ContentError(
                f"{template}: character_field {item.id!r} knows {item.knows!r}, "
                "which is no feature type"
            )
    return CharacterSpec(
        stats=stats,
        templates=templates,
        features=features,
        fields=fields,
        tables=tables,
        table_names=table_names,
    )


def _check_pick_fields(
    templates: Mapping[str, Template], fields: Mapping[str, Field], template: str
) -> None:
    for item in templates.values():
        if item.choose is None:
            continue
        gate = fields.get(item.choose.tag_field)
        if gate is None or gate.table is None:
            raise ContentError(
                f"{template}: template {item.id!r} 'choose' needs a table field named "
                f"{item.choose.tag_field!r}"
            )


def _check_fields(
    fields: Mapping[str, Field], tables: Mapping[str, tuple[TableOption, ...]], template: str
) -> None:
    for item in fields.values():
        label = f"{template}: character_field {item.id}"
        if item.group_roll and not all(option.group for option in tables[item.table or ""]):
            raise ContentError(f"{label}: 'group_roll' needs every option to have a 'group'")
        restriction = item.restriction
        if restriction is None:
            continue
        gate = fields.get(restriction.field_id)
        if item.table is None or gate is None or gate.table is None:
            raise ContentError(
                f"{label}: 'restricted_by' needs a table field named {restriction.field_id!r}"
            )
        if gate.restriction is not None:
            raise ContentError(f"{label}: the field {gate.id!r} it names may not be restricted")
        if not set(restriction.values) <= {option.value for option in tables[gate.table]}:
            raise ContentError(f"{label}: 'restricted_by' values must be options of {gate.id!r}")
        if not set(restriction.groups) <= {option.group for option in tables[item.table]}:
            raise ContentError(f"{label}: 'restricted_by' groups must be groups of its options")


def _each(definitions: list[Definition], kind: str, make: Any) -> dict[str, Any]:
    return {d.id: make(d) for d in definitions if d.type == kind}


def _stat(definition: Definition, template: str) -> Stat:
    label = f"{template}: stat {definition.id}"
    name = required_text(definition.data, "name", label)
    swap = definition.data.get("swap_group")
    if swap is not None and (not isinstance(swap, str) or not swap.strip()):
        raise ContentError(f"{label}: 'swap_group' must be non-empty text")
    if "values" in definition.data:
        return _assigned_stat(definition, label, name, swap)
    formula = required_text(definition.data, "formula", label)
    match = FORMULA.fullmatch(formula)
    if match is None:
        raise ContentError(f"{label}: 'formula' must be a dice formula such as '3d6'")
    count, sides = int(match.group(1)), int(match.group(2))
    return Stat(
        id=definition.id,
        name=name,
        formula=formula,
        swap_group=swap,
        low=count,
        high=count * sides,
    )


def _assigned_stat(definition: Definition, label: str, name: str, swap: str | None) -> Stat:
    if "formula" in definition.data:
        raise ContentError(f"{label}: give 'formula' or 'values', not both")
    values = definition.data["values"]
    if (
        not isinstance(values, list)
        or not values
        or not all(isinstance(v, int) and not isinstance(v, bool) for v in values)
    ):
        raise ContentError(f"{label}: 'values' must be a non-empty list of whole numbers")
    if swap is None:
        raise ContentError(f"{label}: a stat with 'values' needs a 'swap_group' to share them")
    return Stat(
        id=definition.id,
        name=name,
        formula=None,
        swap_group=swap,
        low=min(values),
        high=max(values),
        values=tuple(values),
    )


def _check_groups(stats: Mapping[str, Stat], template: str) -> None:
    """Every stat in a swap group must be rolled, or every one assigned from the same pool with a
    pool as big as the group."""
    groups: dict[str | None, list[Stat]] = {}
    for stat in stats.values():
        if stat.swap_group is not None:
            groups.setdefault(stat.swap_group, []).append(stat)
    for group, members in groups.items():
        assigned = {bool(m.values) for m in members}
        if len(assigned) > 1:
            raise ContentError(f"{template}: stats in swap group {group!r} mix rolled and assigned")
        if not members[0].values:
            continue
        pool = sorted(members[0].values)
        if any(sorted(m.values) != pool for m in members):
            raise ContentError(f"{template}: stats in swap group {group!r} must share one pool")
        if len(members) != len(pool):
            raise ContentError(
                f"{template}: swap group {group!r} has {len(members)} stats for {len(pool)} values"
            )


def load_feature(definition: Definition, template: str) -> Feature:
    label = f"{template}: feature {definition.id}"
    data = definition.data
    kind = required_text(data, "feature_type", label)
    if kind not in FEATURE_TYPES:
        raise ContentError(f"{label}: 'feature_type' must be one of {', '.join(FEATURE_TYPES)}")
    description = data.get("description", "")
    if not isinstance(description, str):
        raise ContentError(f"{label}: 'description' must be text")
    size = data.get("size", "normal")
    if not isinstance(size, str) or not size:
        raise ContentError(f"{label}: 'size' must name an item size")
    uses = data.get("uses", 0)
    if isinstance(uses, bool) or not isinstance(uses, int) or uses < 0:
        raise ContentError(f"{label}: 'uses' must be a whole number, 0 or more")
    armor = data.get("armor", 0)
    if isinstance(armor, bool) or not isinstance(armor, int) or armor < 0:
        raise ContentError(f"{label}: 'armor' must be a whole number, 0 or more")
    damage = data.get("damage", "")
    if not isinstance(damage, str) or (damage and not is_formula(damage)):
        raise ContentError(f"{label}: 'damage' must be a dice formula such as 'd6'")
    tag = data.get("tag", "")
    if not isinstance(tag, str):
        raise ContentError(f"{label}: 'tag' must be text")
    return Feature(
        id=definition.id,
        name=required_text(data, "name", label),
        feature_type=kind,
        description=description,
        size=size,
        uses=uses,
        armor=armor,
        damage=damage,
        rules=rule_ids(data, label),
        tag=tag,
    )


def rule_ids(data: Mapping[str, Any], label: str) -> tuple[str, ...]:
    """The rule ids attached to an entity: a list of text, or none."""
    rules = data.get("rules", [])
    if not isinstance(rules, list) or not all(isinstance(r, str) and r.strip() for r in rules):
        raise ContentError(f"{label}: 'rules' must be a list of rule ids")
    if len(set(rules)) != len(rules):
        raise ContentError(f"{label}: 'rules' names a rule more than once")
    return tuple(rules)


def _field(definition: Definition, tables: Mapping[str, object], kind: str, template: str) -> Field:
    label = f"{template}: {kind} {definition.id}"
    table = definition.data.get("table")
    if table is not None:
        table = required_text(definition.data, "table", label)
        if table not in tables:
            raise ContentError(f"{label}: unknown table {table!r}")
    description = definition.data.get("description", "")
    if not isinstance(description, str):
        raise ContentError(f"{label}: 'description' must be text")
    group_roll = definition.data.get("group_roll", False)
    if not isinstance(group_roll, bool):
        raise ContentError(f"{label}: 'group_roll' must be true or false")
    if group_roll and table is None:
        raise ContentError(f"{label}: 'group_roll' needs a 'table'")
    knows = definition.data.get("knows")
    if knows is not None and (not isinstance(knows, str) or table is None):
        raise ContentError(f"{label}: 'knows' needs a table and the name of a feature type")
    return Field(
        id=definition.id,
        name=required_text(definition.data, "name", label),
        description=description,
        table=table,
        group_roll=group_roll,
        restriction=_restriction(definition.data.get("restricted_by"), label),
        knows=knows,
    )


def _restriction(data: object, label: str) -> Restriction | None:
    if data is None:
        return None
    if not isinstance(data, dict) or set(data) != {"field", "values", "groups"}:
        raise ContentError(f"{label}: 'restricted_by' needs 'field', 'values', and 'groups'")
    values, groups = data["values"], data["groups"]
    for key, items in (("values", values), ("groups", groups)):
        if not isinstance(items, list) or not items or not all(isinstance(i, str) for i in items):
            raise ContentError(f"{label}: 'restricted_by' {key} must be a non-empty list of text")
    return Restriction(
        field_id=required_text(data, "field", label),
        values=tuple(values),
        groups=tuple(groups),
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
        stats=_amounts(data.get("stats", {}), "stats", label),
        counters=_amounts(data.get("counters", {}), "counters", label, minimum=0),
        choose=_choose(data.get("choose"), label),
    )


def _choose(data: object, label: str) -> Choose | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ContentError(f"{label}: 'choose' must be an object")
    count = data.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ContentError(f"{label}: 'choose' count must be a whole number, 1 or more")
    kind = required_text(data, "feature_type", f"{label}: 'choose'")
    if kind not in FEATURE_TYPES:
        raise ContentError(
            f"{label}: 'choose' feature_type must be one of {', '.join(FEATURE_TYPES)}"
        )
    tag_field = required_text(data, "tag_field", f"{label}: 'choose'")
    return Choose(count=count, feature_type=kind, tag_field=tag_field)


def _amounts(value: object, key: str, label: str, minimum: int | None = None) -> dict[str, int]:
    """A map of ids to whole numbers, such as a template's stat bonuses."""
    if not isinstance(value, dict):
        raise ContentError(f"{label}: {key!r} must map ids to whole numbers")
    amounts: dict[str, int] = {}
    for item_id, amount in value.items():
        if not isinstance(item_id, str) or not isinstance(amount, int) or isinstance(amount, bool):
            raise ContentError(f"{label}: {key!r} must map ids to whole numbers")
        if minimum is not None and amount < minimum:
            raise ContentError(f"{label}: {key!r} amounts must be at least {minimum}")
        amounts[item_id] = amount
    return amounts


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
    combination, and anything left out is rolled or left blank. A field that takes a table value
    lists its values with `choice_options`, not here; `check_choices` checks them."""
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
            "description": _features_description(spec),
        }
    fields = {
        f.id: {"type": "string", "description": f.description or f.name}
        for f in spec.fields.values()
    }
    for template in spec.templates.values():
        for table in template.tables:
            fields[table] = {"type": "string", "description": spec.table_names.get(table, table)}
    properties["fields"] = _object(fields)
    return {
        "type": "object",
        "description": (
            "Character options. Anything left out is rolled or left blank. A field that takes "
            "a table value must use one of the values from the character_options lookup."
        ),
        "properties": properties,
        "additionalProperties": False,
    }


def option_fields(spec: CharacterSpec) -> dict[str, tuple[str, str]]:
    """The fields that take a table value, each with its label and table. A template's table is
    keyed by the table's id, as it is in the character's fields."""
    found = {
        f.id: (f.description or f.name, f.table)
        for f in spec.fields.values()
        if f.table is not None
    }
    for template in spec.templates.values():
        for table in template.tables:
            found[table] = (spec.table_names.get(table, table), table)
    return found


def choice_options(spec: CharacterSpec, field: str | None = None) -> dict[str, Any]:
    """The fields that take a table value when `field` is None. Otherwise, the values that field
    can take, with a description for each option that has a name, and any restriction on them."""
    fields = option_fields(spec)
    if field is None:
        return {"fields": [{"field": key, "name": label} for key, (label, _) in fields.items()]}
    if field not in fields:
        return {"error": f"no field {field!r}; the fields with choices are {', '.join(fields)}"}
    result: dict[str, Any] = {
        "field": field,
        "options": [_option_entry(option) for option in spec.tables[fields[field][1]]],
    }
    restriction = spec.fields[field].restriction if field in spec.fields else None
    if restriction is not None:
        values = ", ".join(repr(value) for value in restriction.values)
        groups = ", ".join(repr(group) for group in restriction.groups)
        result["note"] = (
            f"While {restriction.field_id!r} is {values}, only options in {groups} are allowed."
        )
    return result


def _option_entry(option: TableOption) -> dict[str, str]:
    if option.name is None:
        entry = {"value": option.description}
    else:
        entry = {"value": option.name, "description": option.description}
    if option.group is not None:
        entry["group"] = option.group
    return entry


def allowed_options(
    spec: CharacterSpec, field_spec: Field, fields: Mapping[str, Any]
) -> tuple[TableOption, ...]:
    """The options a field may take. A restriction applies while the field it names takes one of
    its values, or while that field is not yet known, since then it applies whatever that is."""
    options = spec.tables[field_spec.table or ""]
    restriction = field_spec.restriction
    if restriction is None:
        return options
    gate = fields.get(restriction.field_id)
    if gate is not None and gate not in restriction.values:
        return options
    return tuple(option for option in options if option.group in restriction.groups)


def check_choices(spec: CharacterSpec, submitted: Mapping[str, Any]) -> None:
    """Raise CharacterError when a field that takes a table value is given a value that is not
    one of the table's options, or one a restriction rules out, or when a template's picks are
    not features of its tag. `submitted` is the character as given: its fields, templates, and
    features."""
    _check_field_values(spec, submitted.get("fields", {}))
    _check_picks(spec, submitted)


def _check_picks(spec: CharacterSpec, submitted: Mapping[str, Any]) -> None:
    fields = submitted.get("fields", {})
    given = submitted.get("features", [])
    for template_id in submitted.get("templates", {}).values():
        template = spec.templates[template_id]
        if template.choose is None:
            continue
        kind = template.choose.feature_type
        picks = [feature for feature in given if spec.features[feature].feature_type == kind]
        if not picks:
            continue
        tag_field = template.choose.tag_field
        if not fields.get(tag_field):
            raise CharacterError(f"give {tag_field!r} to pick the {kind}s for {template.name}")
        tag = fields[tag_field]
        known = knowable_tags(spec, tag_field, tag)
        for feature in picks:
            if spec.features[feature].tag not in known:
                raise CharacterError(
                    f"{spec.features[feature].name!r} is not a {kind} from {tag!r} or earlier"
                )
        if len(picks) > template.choose.count:
            raise CharacterError(
                f"{template.name} picks {template.choose.count} {kind}s, not {len(picks)}"
            )


def _check_field_values(spec: CharacterSpec, fields: Mapping[str, Any]) -> None:
    """The table-value checks for `check_choices`."""
    tables = option_fields(spec)
    for key, value in fields.items():
        if key not in tables:
            continue
        field_spec = spec.fields.get(key)
        if field_spec is not None:
            allowed = allowed_options(spec, field_spec, fields)
        else:
            allowed = spec.tables[tables[key][1]]
        if value in [option.value for option in allowed]:
            continue
        restriction = field_spec.restriction if field_spec is not None else None
        if restriction is not None and value in [
            option.value for option in spec.tables[tables[key][1]]
        ]:
            raise CharacterError(
                f"{value!r} cannot be {key!r} while {restriction.field_id!r} is one of "
                f"{list(restriction.values)!r}"
            )
        raise CharacterError(
            f"{key!r} must be one of the values from character_options for {key!r}"
        )


def _features_description(spec: CharacterSpec) -> str:
    picks = [
        f"{template.name}: pick {template.choose.count} {template.choose.feature_type}(s) for "
        f"the {template.choose.tag_field} given, or they are rolled"
        for template in spec.templates.values()
        if template.choose is not None
    ]
    base = "Features the character has besides any a template grants"
    return "; ".join([base, *picks])


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "additionalProperties": False}


@dataclass(frozen=True)
class CampaignSpec:
    """Options every character in a campaign shares, chosen once when the campaign is created."""

    fields: Mapping[str, Field]
    tables: Mapping[str, tuple[TableOption, ...]]
    table_names: Mapping[str, str]


def build_campaign(
    definitions: list[Definition],
    tables: Mapping[str, tuple[TableOption, ...]],
    table_names: Mapping[str, str],
    template: str,
) -> CampaignSpec:
    fields = _each(
        definitions, "campaign_field", lambda d: _field(d, tables, "campaign_field", template)
    )
    return CampaignSpec(fields=fields, tables=tables, table_names=table_names)


def campaign_schema(spec: CampaignSpec) -> dict[str, Any]:
    """The JSON schema for a campaign's options. Every option is required: the campaign cannot
    start until each one has a value."""
    properties = {f.id: _field_schema(f, spec.tables) for f in spec.fields.values()}
    return {
        "type": "object",
        "description": "Campaign options, chosen once for the whole campaign.",
        "properties": properties,
        "required": list(properties),
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


NO_CHARACTER = CharacterSpec(
    stats={}, templates={}, features={}, fields={}, tables={}, table_names={}
)

NO_CAMPAIGN = CampaignSpec(fields={}, tables={}, table_names={})


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
    # Counter id to its starting value, for the counters the templates set.
    counters: dict[str, int] = field(default_factory=dict)


def fixed_stats(spec: CharacterSpec) -> dict[str, int]:
    """The starting value of each stat that is assigned from a pool: its group's pool in stat
    order."""
    fixed: dict[str, int] = {}
    groups: dict[str | None, list[Stat]] = {}
    for stat in spec.stats.values():
        if stat.formula is None:
            groups.setdefault(stat.swap_group, []).append(stat)
    for members in groups.values():
        for stat, value in zip(members, members[0].values):
            fixed[stat.id] = value
    return fixed


def roll_stats(spec: CharacterSpec, rng: random.Random, *, plus: str) -> dict[str, int]:
    fixed = fixed_stats(spec)
    return {
        s.id: fixed[s.id] if s.formula is None else roll_total(s.formula, rng, plus=plus)
        for s in spec.stats.values()
    }


def check_swaps(spec: CharacterSpec, rolled: Mapping[str, int], chosen: Mapping[str, int]) -> None:
    """A player keeps the rolled stats, except that each swap group may swap one pair of values.
    Stats outside a group must keep their rolled value."""
    groups: dict[str | None, list[str]] = {}
    assigned = {s.id for s in spec.stats.values() if s.formula is None}
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
        if ids[0] in assigned:
            continue
        if sum(b != a for b, a in zip(before, after)) > 2:
            raise CharacterError(f"stats in group {group!r} allow one swap")


def resolve(
    spec: CharacterSpec, submitted: Mapping[str, Any], rng: random.Random, *, plus: str
) -> Sheet:
    """Fill in what a GM or player left out, and roll what is random. `submitted` must already
    match `character_schema(spec)` and pass `check_choices`. Stats left out are rolled; text left
    out is blank; a table left out is rolled; a template's features are added with their rolled
    counts."""
    given_stats = submitted.get("stats", {})
    given_templates = submitted.get("templates", {})
    given_features = submitted.get("features", [])
    given_fields = submitted.get("fields", {})
    fixed = fixed_stats(spec)
    stats = {
        s.id: given_stats[s.id]
        if s.id in given_stats
        else fixed[s.id]
        if s.formula is None
        else roll_total(s.formula, rng, plus=plus)
        for s in spec.stats.values()
    }
    templates = dict(given_templates)
    features = {feature: 1 for feature in given_features}
    fields: dict[str, str] = {}
    restricted: list[Field] = []
    counter_amounts: dict[str, int] = {}
    for template_id in templates.values():
        template = spec.templates[template_id]
        for stat_id, bonus in template.stats.items():
            stats[stat_id] += bonus
        for counter_id, amount in template.counters.items():
            counter_amounts[counter_id] = counter_amounts.get(counter_id, 0) + amount
        for granted in template.features:
            count = (
                int(granted.quantity)
                if granted.quantity.isdigit()
                else roll_total(granted.quantity, rng, plus=plus)
            )
            features[granted.feature] = features.get(granted.feature, 0) + count
        for table in template.tables:
            fields[table] = given_fields.get(table) or rng.choice(spec.tables[table]).value
    for field_spec in spec.fields.values():
        if field_spec.table is None:
            fields[field_spec.id] = given_fields.get(field_spec.id, "")
        elif field_spec.restriction is not None:
            restricted.append(field_spec)
        else:
            fields[field_spec.id] = _field_value(spec, field_spec, given_fields, fields, rng)
    for field_spec in restricted:
        fields[field_spec.id] = _field_value(spec, field_spec, given_fields, fields, rng)
    for template_id in templates.values():
        choose = spec.templates[template_id].choose
        if choose is not None:
            _pick_features(spec, choose, fields, features, rng)
    counters = {
        counter_id: _within(spec.counters[counter_id], spec.counters[counter_id].initial + amount)
        for counter_id, amount in counter_amounts.items()
    }
    return Sheet(
        stats=stats, templates=templates, features=features, fields=fields, counters=counters
    )


def _pick_features(
    spec: CharacterSpec,
    choose: Choose,
    fields: Mapping[str, str],
    features: dict[str, int],
    rng: random.Random,
) -> None:
    """Roll the picks a template's `choose` leaves to chance: features of its type whose tag is
    knowable from the value of the gate field, skipping any the character already has. An empty
    pool yields no picks."""
    held = [
        feature
        for feature in features
        if spec.features[feature].feature_type == choose.feature_type
    ]
    needed = max(0, choose.count - len(held))
    known = knowable_tags(spec, choose.tag_field, fields.get(choose.tag_field, ""))
    pool = sorted(
        feature.id
        for feature in spec.features.values()
        if feature.feature_type == choose.feature_type
        and feature.tag in known
        and feature.id not in features
    )
    for feature in rng.sample(pool, min(needed, len(pool))):
        features[feature] = 1


def _field_value(
    spec: CharacterSpec,
    field_spec: Field,
    given_fields: Mapping[str, Any],
    fields: Mapping[str, Any],
    rng: random.Random,
) -> str:
    """The value of a table field: the one given, or else rolled. A group roll picks a group
    first, so the group picked is as likely as any other."""
    options = allowed_options(spec, field_spec, fields)
    if field_spec.id in given_fields:
        value = given_fields[field_spec.id]
        if field_spec.restriction is not None and value not in [o.value for o in options]:
            raise CharacterError(f"{value!r} is not allowed for {field_spec.id!r} here")
        return value
    if field_spec.group_roll:
        groups = list(dict.fromkeys(option.group for option in options))
        group = rng.choice(groups)
        options = tuple(option for option in options if option.group == group)
    return rng.choice(options).value


def _within(counter: CounterRef, value: int) -> int:
    if counter.minimum is not None:
        value = max(counter.minimum, value)
    if counter.maximum is not None:
        value = min(counter.maximum, value)
    return value


def tag_order(spec: CharacterSpec, tag_field: str) -> tuple[str, ...]:
    """The options of the table field `tag_field`, in table order: the tags a pick can be from."""
    table = spec.fields[tag_field].table
    return () if table is None else tuple(option.value for option in spec.tables[table])


def knowable_tags(spec: CharacterSpec, tag_field: str, value: str) -> tuple[str, ...]:
    """The tags a character knows by a value of `tag_field`: that value and every option before it
    in table order. Empty when the value is not one of the options."""
    order = tag_order(spec, tag_field)
    if value not in order:
        return ()
    return order[: order.index(value) + 1]


def _known_features(spec: CharacterSpec, field_id: str, value: str) -> tuple[str, str, str] | None:
    """The feature type a field's value knows, its table's name for the field's options, and the
    labels of the earlier options, or None when the field knows nothing."""
    field_spec = spec.fields.get(field_id)
    if field_spec is None or field_spec.knows is None or field_spec.table is None:
        return None
    options = spec.tables[field_spec.table]
    values = [option.value for option in options]
    if value not in values:
        return None
    earlier = [option.value for option in options[: values.index(value)]]
    if not earlier:
        return None
    return (
        field_spec.knows,
        spec.table_names.get(field_spec.table, field_spec.name),
        ", ".join(earlier),
    )


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
    for counter_id, value in sheet.counters.items():
        if counter_id in spec.counters:
            lines.append(f"{spec.counters[counter_id].label}: {value}")
    for field_id, value in sheet.fields.items():
        known = _known_features(spec, field_id, value)
        if known:
            lines.append(f"Knows every {known[0]} tagged with an earlier {known[1]}: {known[2]}")
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
