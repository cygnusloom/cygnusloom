"""Turn a rule set's character spec and campaign options into the creation form, and a submitted
form back into their arguments. The form names are `stat-<id>`, `template-<kind>`, `field-<id>`
for the character, and `campaign-<id>` for the campaign options, which share one form."""

from __future__ import annotations

from typing import Any

from starlette.datastructures import FormData

from cygnusforge.content.character import (
    CampaignSpec,
    CharacterSpec,
    Restriction,
    TableOption,
    Template,
    feature_pick_options,
    skill_pick_options,
)
from cygnusforge.services.common import InvalidInput
from cygnusforge.web.forms import form_text

STAT_PREFIX = "stat-"
TEMPLATE_PREFIX = "template-"
FIELD_PREFIX = "field-"
PICK_PREFIX = "pick-"
SKILL_PICK_PREFIX = PICK_PREFIX + "skill-"
FEATURE_PICK_PREFIX = PICK_PREFIX + "feature-"
CAMPAIGN_PREFIX = "campaign-"
PRONOUNS_FIELD = "pronouns"
PRONOUNS_OTHER_FIELD = "pronouns-other"
PRONOUN_CHOICES = ("he/him", "she/her", "they/them")
OTHER_PRONOUNS = "other"


def creation_view(spec: CharacterSpec, rolled: dict[str, int]) -> dict[str, Any]:
    """What the creation form shows. A stat in a swap group may take any value rolled for its
    group; other stats keep their roll. A derived stat is computed, so it is not shown."""
    group_values: dict[str, list[int]] = {}
    for stat in spec.stats.values():
        if stat.derived is None and stat.swap_group is not None:
            group_values.setdefault(stat.swap_group, []).append(rolled[stat.id])
    stats = [
        {
            "id": stat.id,
            "name": stat.name,
            "value": rolled[stat.id],
            "group": stat.swap_group,
            "assigned": stat.formula is None,
            "options": sorted(group_values[stat.swap_group])
            if stat.swap_group is not None
            else [rolled[stat.id]],
        }
        for stat in spec.stats.values()
        if stat.derived is None
    ]
    templates = [
        {
            "id": template.id,
            "kind": template.kind,
            "name": template.name,
            "description": template.description,
            "tables": [_table_view(spec, table) for table in template.tables],
            "names": list(template.names),
            "picks": _picks_view(spec, template),
            "skill_picks": _skill_picks_view(spec, template),
            "feat_picks": _feat_picks_view(spec, template),
            "classes": list(template.classes),
        }
        for template in spec.templates.values()
    ]
    kinds = [
        {
            "kind": kind,
            "label": kind.capitalize(),
            "optional": kind in spec.optional_kinds,
            "templates": [t for t in templates if t["kind"] == kind],
        }
        for kind in spec.kinds()
    ]
    choice_fields = [
        {
            "id": field.id,
            "name": field.name,
            "description": field.description,
            "restriction": _restriction_view(field.restriction),
            "options": _option_views(spec.tables[field.table]),
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
        "name_suggestions": any(template.names for template in spec.templates.values()),
        "pronoun_options": _pronoun_options(),
    }


def pronouns_from_form(form: FormData) -> str:
    """The pronouns the player chose: one of the listed sets, or the text written for Other."""
    chosen = form_text(form, PRONOUNS_FIELD)
    if chosen == OTHER_PRONOUNS:
        own = form_text(form, PRONOUNS_OTHER_FIELD).strip()
        if not own:
            raise InvalidInput("Write your pronouns, or choose one from the list")
        return own
    if chosen not in PRONOUN_CHOICES:
        raise InvalidInput("Choose your pronouns")
    return chosen


def _pronoun_options() -> list[dict[str, Any]]:
    """The listed pronoun sets, then Other. Other is freeform: a random roll skips it, since it
    needs the text the player writes."""
    options: list[dict[str, Any]] = [
        {"value": value, "title": value, "freeform": False} for value in PRONOUN_CHOICES
    ]
    options.append({"value": OTHER_PRONOUNS, "title": "Other", "freeform": True})
    return options


def campaign_view(spec: CampaignSpec) -> dict[str, Any]:
    """What the campaign options form shows: each campaign option as a panel."""
    choice_fields = [
        {
            "id": field.id,
            "name": field.name,
            "description": field.description,
            "options": _option_views(spec.tables[field.table]),
        }
        for field in spec.fields.values()
        if field.table is not None
    ]
    text_fields = [
        {"id": field.id, "name": field.name, "description": field.description}
        for field in spec.fields.values()
        if field.table is None
    ]
    return {"choice_fields": choice_fields, "text_fields": text_fields}


def campaign_submission_from_form(spec: CampaignSpec, form: FormData) -> dict[str, Any]:
    """The campaign options a form stands for, keyed by option id as the schema expects.
    Every option must be given."""
    options: dict[str, Any] = {}
    for field in spec.fields.values():
        value = form_text(form, CAMPAIGN_PREFIX + field.id)
        if not value:
            raise InvalidInput(f"Choose {field.name}")
        options[field.id] = value
    return options


def submission_from_form(spec: CharacterSpec, form: FormData) -> dict[str, Any]:
    """The arguments a player's form stands for. Every template, table, and choice must be
    given: the player picks each one or rolls it with a dice button. Text fields may be blank.
    Groups the rule set does not define are left out, since the schema does not allow them.
    A blank skill or feature pick is left out, so `resolve` rolls it."""
    stats: dict[str, Any] = {}
    for stat in spec.stats.values():
        if stat.derived is not None:
            continue
        raw = form_text(form, STAT_PREFIX + stat.id)
        try:
            stats[stat.id] = int(raw)
        except ValueError:
            raise InvalidInput(f"{stat.name} must be a number") from None
    templates = {}
    class_id = form_text(form, TEMPLATE_PREFIX + "class")
    for kind in spec.kinds():
        offered = [
            template
            for template in spec.templates.values()
            if template.kind == kind and (not template.classes or class_id in template.classes)
        ]
        if not offered:
            continue
        template_id = form_text(form, TEMPLATE_PREFIX + kind)
        if not template_id:
            if kind in spec.optional_kinds:
                continue
            raise InvalidInput(f"Choose a {kind} for your character")
        if template_id not in {template.id for template in offered}:
            if kind in spec.optional_kinds:
                continue
            raise InvalidInput(f"That {kind} cannot be chosen by this class")
        templates[kind] = template_id
    if class_id == "adventurer_mage_mage" and "tradition2" not in templates:
        raise InvalidInput("Choose a second tradition for your Mage/Mage Adventurer")
    fields = {}
    for field in spec.fields.values():
        value = form_text(form, FIELD_PREFIX + field.id)
        if value:
            fields[field.id] = value
        elif field.table is not None:
            raise InvalidInput(f"Choose {field.name}")
    features: list[str] = []
    skill_picks: dict[str, list[str]] = {}
    feature_picks: dict[str, list[str]] = {}
    for template_id in templates.values():
        template = spec.templates.get(template_id)
        if template is None:
            continue
        for table in template.tables:
            value = form_text(form, FIELD_PREFIX + table)
            if not value:
                raise InvalidInput(f"Choose {spec.table_names.get(table, table)}")
            fields[table] = value
        if template.choose is not None:
            for index in range(template.choose.count):
                value = form_text(form, f"{PICK_PREFIX}{template.id}-{index}")
                if value:
                    features.append(value)
        for pick in template.skill_picks:
            chosen = [
                value
                for index in range(pick.count)
                if (
                    value := form_text(
                        form,
                        f"{SKILL_PICK_PREFIX}{template.id}-{pick.id}-{index}",
                    )
                    or form_text(form, f"{SKILL_PICK_PREFIX}{pick.id}-{index}")
                )
            ]
            if chosen:
                skill_picks[pick.id] = chosen
        for pick in template.feat_picks:
            chosen = [
                value
                for index in range(pick.count)
                if (
                    value := form_text(
                        form,
                        f"{FEATURE_PICK_PREFIX}{template.id}-{pick.id}-{index}",
                    )
                    or form_text(form, f"{FEATURE_PICK_PREFIX}{pick.id}-{index}")
                )
            ]
            if chosen:
                feature_picks[pick.id] = chosen
    submitted: dict[str, Any] = {"fields": fields}
    if spec.stats:
        submitted["stats"] = stats
    if spec.templates:
        submitted["templates"] = templates
    if features:
        submitted["features"] = features
    if skill_picks:
        submitted["skill_picks"] = skill_picks
    if feature_picks:
        submitted["feature_picks"] = feature_picks
    return submitted


def _picks_view(spec: CharacterSpec, template: Template) -> dict[str, Any] | None:
    """The spells a template lets the player pick, grouped by the tag they share (a Year). A blank
    pick is rolled from the same spells."""
    if template.choose is None:
        return None
    tag_field = spec.fields[template.choose.tag_field]
    groups: dict[str, list[dict[str, str]]] = {}
    for feature in spec.features.values():
        if feature.feature_type == template.choose.feature_type:
            groups.setdefault(feature.tag, []).append({"id": feature.id, "name": feature.name})
    return {
        "count": template.choose.count,
        "feature_type": template.choose.feature_type,
        "tag_field": tag_field.id,
        "hint": f"Pick from the spells of your {tag_field.name}, or leave a pick blank to roll it.",
        "groups": [{"tag": tag, "features": items} for tag, items in groups.items()],
    }


def _skill_picks_view(spec: CharacterSpec, template: Template) -> list[dict[str, Any]]:
    """The skill picks a template lets the player choose, each with its offered skills."""
    return [
        {
            "id": pick.id,
            "count": pick.count,
            "hint": (
                f"Pick {pick.count} skill{'s' if pick.count != 1 else ''} "
                f"({spec.table_names.get(pick.table, pick.table)}), or leave a pick blank to roll it."
            ),
            "options": [
                {"value": skill_id, "title": label}
                for skill_id, label in skill_pick_options(spec, pick)
            ],
        }
        for pick in template.skill_picks
    ]


def _feat_picks_view(spec: CharacterSpec, template: Template) -> list[dict[str, Any]]:
    """The feature picks a template lets the player choose, each with its offered features. A
    pick's options are filtered by the pick's tag and highest rank."""
    return [
        {
            "id": pick.id,
            "count": pick.count,
            "tag": pick.tag,
            "max_rank": pick.max_rank,
            "hint": (
                f"Pick {pick.count} {pick.tag}{'s' if pick.count != 1 else ''}"
                + (f" of rank {pick.max_rank} or less" if pick.max_rank not in (None, 0) else "")
                + ", or leave a pick blank to roll it."
            ),
            "options": [
                {
                    "value": feature.id,
                    "title": feature.name,
                    "detail": feature.description or None,
                    "rank": feature.rank,
                }
                for feature in feature_pick_options(spec, pick)
            ],
        }
        for pick in template.feat_picks
    ]


def _table_view(spec: CharacterSpec, table: str) -> dict[str, Any]:
    return {
        "id": table,
        "label": spec.table_names.get(table, table),
        "options": _option_views(spec.tables[table]),
    }


def _restriction_view(restriction: Restriction | None) -> dict[str, Any] | None:
    """The gate a choice's options are restricted by. The page disables the options it rules out."""
    if restriction is None:
        return None
    return {
        "field": restriction.field_id,
        "values": list(restriction.values),
        "groups": list(restriction.groups),
    }


def _option_views(options: tuple[TableOption, ...]) -> list[dict[str, Any]]:
    """A named option shows its name as the title and its description beneath, as a background
    does. Other options show their description as the title."""
    return [
        {
            "value": option.value,
            "title": option.value,
            "detail": option.description if option.name is not None else None,
            "group": option.group,
        }
        for option in options
    ]
