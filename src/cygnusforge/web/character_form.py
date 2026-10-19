"""Turn a rule set's character spec into the creation form, and a submitted form back into
character arguments. The form names are `stat-<id>`, `template-<kind>`, and `field-<id>`."""

from __future__ import annotations

from typing import Any

from starlette.datastructures import FormData

from cygnusforge.content.character import CharacterSpec
from cygnusforge.services.common import InvalidInput
from cygnusforge.web.forms import form_text

STAT_PREFIX = "stat-"
TEMPLATE_PREFIX = "template-"
FIELD_PREFIX = "field-"


def creation_view(spec: CharacterSpec, rolled: dict[str, int]) -> dict[str, Any]:
    """What the creation form shows. A stat in a swap group may take any value rolled for its
    group; other stats keep their roll."""
    group_values: dict[str, list[int]] = {}
    for stat in spec.stats.values():
        if stat.swap_group is not None:
            group_values.setdefault(stat.swap_group, []).append(rolled[stat.id])
    stats = [
        {
            "id": stat.id,
            "name": stat.name,
            "value": rolled[stat.id],
            "options": sorted(group_values[stat.swap_group])
            if stat.swap_group is not None
            else [rolled[stat.id]],
        }
        for stat in spec.stats.values()
    ]
    templates = [
        {
            "id": template.id,
            "kind": template.kind,
            "name": template.name,
            "description": template.description,
            "tables": [_table_view(spec, table) for table in template.tables],
        }
        for template in spec.templates.values()
    ]
    kinds = [
        {
            "kind": kind,
            "label": kind.capitalize(),
            "templates": [t for t in templates if t["kind"] == kind],
        }
        for kind in spec.kinds()
    ]
    choice_fields = [
        {
            "id": field.id,
            "name": field.name,
            "description": field.description,
            "options": list(spec.tables[field.table]),
        }
        for field in spec.fields.values()
        if field.table is not None
    ]
    text_fields = [
        {"id": field.id, "name": field.name, "description": field.description}
        for field in spec.fields.values()
        if field.table is None
    ]
    return {
        "stats": stats,
        "kinds": kinds,
        "choice_fields": choice_fields,
        "text_fields": text_fields,
    }


def submission_from_form(spec: CharacterSpec, form: FormData) -> dict[str, Any]:
    """The arguments a player's form stands for. Every template, table, and choice must be
    given: the player picks each one or rolls it with a dice button. Text fields may be blank.
    Groups the rule set does not define are left out, since the schema does not allow them.
    Values are checked against the schema by the caller."""
    stats: dict[str, Any] = {}
    for stat in spec.stats.values():
        raw = form_text(form, STAT_PREFIX + stat.id)
        try:
            stats[stat.id] = int(raw)
        except ValueError:
            raise InvalidInput(f"{stat.name} must be a number") from None
    templates = {}
    for kind in spec.kinds():
        template_id = form_text(form, TEMPLATE_PREFIX + kind)
        if not template_id:
            raise InvalidInput(f"Choose a {kind} for your character")
        templates[kind] = template_id
    fields = {}
    for field in spec.fields.values():
        value = form_text(form, FIELD_PREFIX + field.id)
        if value:
            fields[field.id] = value
        elif field.table is not None:
            raise InvalidInput(f"Choose {field.name}")
    for template_id in templates.values():
        template = spec.templates.get(template_id)
        if template is None:
            continue
        for table in template.tables:
            value = form_text(form, FIELD_PREFIX + table)
            if not value:
                raise InvalidInput(f"Choose {spec.table_names.get(table, table)}")
            fields[table] = value
    submitted: dict[str, Any] = {"fields": fields}
    if spec.stats:
        submitted["stats"] = stats
    if spec.templates:
        submitted["templates"] = templates
    return submitted


def _table_view(spec: CharacterSpec, table: str) -> dict[str, Any]:
    return {
        "id": table,
        "label": spec.table_names.get(table, table),
        "options": list(spec.tables[table]),
    }
