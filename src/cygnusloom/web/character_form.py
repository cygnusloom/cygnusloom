"""Turn a rule set's character spec and campaign options into the creation form, and a submitted
form back into their arguments. The form names are `stat-<id>`, `template-<kind>`, `field-<id>`
for the character, and `campaign-<id>` for the campaign options, which share one form."""

from __future__ import annotations

from typing import Any

from starlette.datastructures import FormData

from cygnusloom.content.character import (
    CampaignSpec,
    CharacterSpec,
    Restriction,
    TableOption,
    Template,
    feature_next_ranks,
    feature_pick_options,
    kind_label,
    skill_pick_options,
)
from cygnusloom.services.common import InvalidInput
from cygnusloom.web.forms import form_text

STAT_PREFIX = "stat-"
TEMPLATE_PREFIX = "template-"
FIELD_PREFIX = "field-"
PICK_PREFIX = "pick-"
SKILL_PICK_PREFIX = PICK_PREFIX + "skill-"
FEATURE_PICK_PREFIX = PICK_PREFIX + "feature-"
FEATURE_REPEAT_PREFIX = "repeat-feature-"
SKILL_REPLACEMENT_PREFIX = "skill-replacement-"
CAMPAIGN_PREFIX = "campaign-"
PRONOUNS_FIELD = "pronouns"
PRONOUNS_OTHER_FIELD = "pronouns-other"
PRONOUN_CHOICES = ("he/him", "she/her", "they/them")
OTHER_PRONOUNS = "other"


KIND_HINTS = {
    "background": "What the character did before adventuring. It grants training and quick skills.",
    "class": "The character's calling. It sets starting strengths, hit points, and features.",
    "class2": "The other half of a paired class, adding its own strengths and features.",
    "tradition": "The magical tradition the character follows. It sets the arts and spells they learn.",
    "tradition2": "An optional second tradition for a character who studies two kinds of magic.",
    "kit": "The gear the character starts with, ready to carry and wear.",
}


def creation_view(spec: CharacterSpec, rolled: dict[str, int]) -> dict[str, Any]:
    """What the creation form shows. A stat in a swap group may trade values with the rest of
    its group; other stats keep their roll. A derived stat is computed, so it is not shown."""
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
            "options": [
                {"value": value, "modifier": stat.modifier(value)}
                for value in (
                    sorted(group_values[stat.swap_group])
                    if stat.swap_group is not None
                    else [rolled[stat.id]]
                )
            ],
            "modifier": stat.modifier(rolled[stat.id]) if stat.modifier_bands else None,
            "modifier_bands": [list(band) for band in stat.modifier_bands],
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
            "skill_grants": dict(template.skills),
            "grants": _template_grants(spec, template),
            "summary": " · ".join(_template_grants(spec, template)),
            "compatible_with": {
                kind: list(template_ids) for kind, template_ids in template.compatible_with.items()
            },
            "template_requirements": [
                {
                    "kinds": list(requirement.kinds),
                    "distinct": requirement.distinct,
                    "missing_message": requirement.missing_message,
                    "duplicate_message": requirement.duplicate_message,
                }
                for requirement in template.template_requirements
            ],
            "choice_key": template.choice_key or template.id,
        }
        for template in spec.templates.values()
    ]
    kinds = [
        {
            "kind": kind,
            "label": kind_label(kind),
            "hint": KIND_HINTS.get(kind),
            "optional": kind in spec.optional_kinds,
            "starting_silver": (
                {
                    "label": spec.starting_silver.label,
                    "description": spec.starting_silver.description,
                }
                if spec.starting_silver is not None and spec.starting_silver.kind == kind
                else None
            ),
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
            "options": _option_views(spec, spec.tables[field.table]),
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
        "skill_progression": (
            {
                "first_level": spec.skill_progression.first_level,
                "creation_maximum": spec.skill_progression.creation_maximum,
            }
            if spec.skill_progression is not None
            else None
        ),
        "skill_replacements": _skill_replacements_view(spec),
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
            "options": _option_views(spec, spec.tables[field.table]),
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
    selected_template_ids = {kind: form_text(form, TEMPLATE_PREFIX + kind) for kind in spec.kinds()}
    for kind in spec.kinds():
        offered = [
            template
            for template in spec.templates.values()
            if template.kind == kind and _template_is_compatible(template, selected_template_ids)
        ]
        if not offered:
            continue
        label = kind_label(kind)
        template_id = form_text(form, TEMPLATE_PREFIX + kind)
        if not template_id:
            starting_silver = spec.starting_silver
            if kind in spec.optional_kinds or (
                starting_silver is not None and starting_silver.kind == kind
            ):
                continue
            raise InvalidInput(f"Choose a {label.lower()} for your character")
        if template_id not in {template.id for template in offered}:
            if kind in spec.optional_kinds:
                continue
            raise InvalidInput(f"That {label.lower()} cannot be chosen with your other choices")
        templates[kind] = template_id
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
    feature_repeats: dict[str, list[str]] = {}
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
            chosen = []
            repeats = []
            for index in range(pick.count):
                value = form_text(
                    form, f"{FEATURE_PICK_PREFIX}{template.id}-{pick.id}-{index}"
                ) or form_text(form, f"{FEATURE_PICK_PREFIX}{pick.id}-{index}")
                mode = form_text(form, f"{FEATURE_REPEAT_PREFIX}{template.id}-{pick.id}-{index}")
                if mode not in ("", "repeat"):
                    raise InvalidInput(f"Invalid repeat choice for {pick.id}")
                if mode == "repeat" and not value:
                    raise InvalidInput(f"Choose a feature to repeat for {pick.id}")
                if value:
                    chosen.append(value)
                    if mode == "repeat":
                        repeats.append(value)
            if chosen:
                feature_picks[pick.id] = chosen
            if repeats:
                feature_repeats[pick.id] = repeats
    submitted: dict[str, Any] = {"fields": fields}
    if stats:
        submitted["stats"] = stats
    if spec.templates:
        submitted["templates"] = templates
    if features:
        submitted["features"] = features
    if skill_picks:
        submitted["skill_picks"] = skill_picks
    if feature_picks:
        submitted["feature_picks"] = feature_picks
    if feature_repeats:
        submitted["feature_repeats"] = feature_repeats
    progression = spec.skill_progression
    if progression is not None and progression.creation_replacements is not None:
        replacements: dict[str, list[str]] = {}
        for kind in ("roll", "feature"):
            values = form.getlist(SKILL_REPLACEMENT_PREFIX + kind)
            if not all(isinstance(value, str) for value in values):
                raise InvalidInput(f"{kind.capitalize()} skill replacements must be text")
            choices = [value for value in values if isinstance(value, str)]
            if any(choices):
                replacements[kind] = choices
        if replacements:
            submitted["skill_replacements"] = replacements
    return submitted


def _template_is_compatible(template: Template, selected: dict[str, str]) -> bool:
    """A template is offered only when every template kind it names has an allowed selection."""
    return all(selected.get(kind) in allowed for kind, allowed in template.compatible_with.items())


def _picks_view(spec: CharacterSpec, template: Template) -> dict[str, Any] | None:
    """The features a template lets the player pick, grouped by their tag."""
    if template.choose is None:
        return None
    tag_field = spec.fields[template.choose.tag_field]
    groups: dict[str, list[dict[str, Any]]] = {}
    for feature in spec.features.values():
        if feature.feature_type == template.choose.feature_type:
            groups.setdefault(feature.tag, []).append(
                {
                    "id": feature.id,
                    "name": feature.name,
                    "detail": feature.description,
                    "rank": feature.rank,
                }
            )
    return {
        "count": template.choose.count,
        "feature_type": template.choose.feature_type,
        "noun": template.choose.feature_type,
        "label": _plural_noun(template.choose.feature_type, template.choose.count).title(),
        "tag_field": tag_field.id,
        "skip_tags": list(template.choose.skip_tags),
        "hint": (
            f"Pick from the {_plural_noun(template.choose.feature_type, 2)} "
            f"of your {tag_field.name}, or leave a pick blank to roll it."
        ),
        "groups": [{"tag": tag, "features": items} for tag, items in groups.items()],
    }


def _skill_picks_view(spec: CharacterSpec, template: Template) -> list[dict[str, Any]]:
    """The skill picks a template lets the player choose, each with its offered skills."""
    return [
        {
            "id": pick.id,
            "label": pick.label,
            "count": pick.count,
            "max_level": pick.max_level,
            "hint": (
                f"Pick {pick.count} skill{'s' if pick.count != 1 else ''} "
                f"({_table_label(spec, pick.table, template)}), "
                "or leave a pick blank to roll it."
            ),
            "options": [
                {
                    "value": skill_id,
                    "title": label,
                    "detail": (
                        spec.skills[skill_id].description if skill_id in spec.skills else None
                    ),
                }
                for skill_id, label in skill_pick_options(spec, pick)
            ],
        }
        for pick in template.skill_picks
    ]


def _skill_replacements_view(spec: CharacterSpec) -> list[dict[str, Any]]:
    progression = spec.skill_progression
    rules = progression.creation_replacements if progression is not None else None
    if rules is None:
        return []
    return [
        {
            "kind": kind,
            "label": label,
            "options": [
                {"value": skill.id, "title": skill.name, "detail": skill.description}
                for skill in spec.skills.values()
                if skill.id not in excluded
            ],
            "excluded": [spec.skills[skill_id].name for skill_id in excluded],
        }
        for kind, label, excluded in (
            ("roll", "Rolled or other grants", rules.roll_exclude),
            ("feature", "Feature grants", rules.feature_exclude),
        )
    ]


def _feat_picks_view(spec: CharacterSpec, template: Template) -> list[dict[str, Any]]:
    """The feature picks a template lets the player choose, each with its offered features. A
    pick's options are filtered by the pick's tag and highest rank."""
    picks = []
    for pick in template.feat_picks:
        label = pick.tag or pick.feature_type or "feature"
        noun = _pick_noun(pick)
        plural = _plural_noun(noun, pick.count)
        picks.append(
            {
                "id": pick.id,
                "count": pick.count,
                "tag": label,
                "noun": noun,
                "label": plural.title(),
                "max_rank": pick.max_rank,
                "tags_from": pick.tags_from,
                "source_hint": (
                    f"Choose {pick.tags_from.replace('_', ' ')} first; only their features are offered."
                    if pick.tags_from
                    else ""
                ),
                "advance_repeated_rank": pick.advance_repeated_rank,
                "hint": (
                    f"Pick {pick.count} {plural}"
                    + (
                        f" of rank {pick.max_rank} or less"
                        if pick.max_rank not in (None, 0)
                        else ""
                    )
                    + ", or leave a pick blank to roll it."
                    + (
                        " Choosing the same ranked feature in another pick advances it "
                        "to its next rank when available; repeatable features can instead "
                        "start a new choice."
                        if pick.advance_repeated_rank
                        else ""
                    )
                ),
                "options": [
                    {
                        "value": feature.id,
                        "title": feature.name,
                        "detail": _feature_detail(feature),
                        "tag": feature.tag,
                        "rank": feature.rank,
                        "repeatable": feature.repeatable,
                        "ordered": feature.tag in spec.ordered_feature_tags,
                        "has_next_rank": bool(feature_next_ranks(spec, pick, feature)),
                    }
                    for feature in feature_pick_options(spec, pick)
                ],
            }
        )
    return picks


def _table_view(spec: CharacterSpec, table: str) -> dict[str, Any]:
    return {
        "id": table,
        "label": spec.table_names.get(table, table),
        "options": _option_views(spec, spec.tables[table]),
    }


def _table_label(spec: CharacterSpec, table: str, template: Template) -> str:
    """A readable name for a pick's table. A rule set may name its tables; otherwise the
    template kind is clearer than the raw table id."""
    label = spec.table_names.get(table)
    if label and label != table:
        return label
    if table == "all_skills":
        return "any skill"
    return f"{kind_label(template.kind).lower()} table"


def _plural_noun(noun: str, count: int) -> str:
    """A count and noun read naturally: 1 focus, 2 foci, 1 skill, 2 skills."""
    if count == 1:
        return noun
    if noun == "focus":
        return "foci"
    if noun.endswith(("s", "x", "z", "ch", "sh")):
        return f"{noun}es"
    if noun.endswith("y") and len(noun) > 1 and noun[-2] not in "aeiou":
        return f"{noun[:-1]}ies"
    return f"{noun}s"


def _pick_noun(pick: Any) -> str:
    """The noun a pick offers, for headings and summaries: spell, potion, subject, focus, art."""
    if pick.feature_type in {"spell", "potion", "enchantment", "subject", "item"}:
        return pick.feature_type
    if pick.tag in {"focus", "origin"}:
        return pick.tag
    if pick.feature_type == "feat":
        return "art"
    return pick.feature_type or pick.tag or "feature"


def _feature_detail(feature: Any) -> str | None:
    """A feature's description, plus a note when taking it asks the player for a choice."""
    detail = feature.description or ""
    if feature.picks:
        labels = ", ".join(pick.label.lower() for pick in feature.picks)
        note = f"Choose its {labels} when you take it."
        detail = f"{detail} {note}".strip()
    return detail or None


def _template_grants(spec: CharacterSpec, template: Template) -> list[str]:
    """The short lines a player needs to compare a template with the others: what it adds to
    stats, which skills and features it gives, what it lets them pick, and what it carries."""
    grants: list[str] = []
    skills = [
        f"{spec.skills[skill_id].name} {level}"
        for skill_id, level in template.skills.items()
        if skill_id in spec.skills
    ]
    if skills:
        grants.append(", ".join(skills))
    stats = [
        f"{spec.stats[stat_id].name} {delta:+d}"
        for stat_id, delta in template.stats.items()
        if delta and stat_id in spec.stats
    ]
    if stats:
        grants.append(", ".join(stats))
    features = [
        spec.features[granted.feature].name
        for granted in template.features
        if granted.feature in spec.features
    ]
    if features:
        grants.append(", ".join(features))
    if template.choose is not None:
        count = template.choose.count
        noun = _plural_noun(template.choose.feature_type, count)
        grants.append(f"{count} {noun} of your choice")
    skill_choices = sum(pick.count for pick in template.skill_picks)
    if skill_choices:
        grants.append(f"{skill_choices} {_plural_noun('skill', skill_choices)} of your choice")
    feature_choices: dict[str, int] = {}
    for pick in template.feat_picks:
        noun = _pick_noun(pick)
        feature_choices[noun] = feature_choices.get(noun, 0) + pick.count
    for noun, count in feature_choices.items():
        grants.append(f"{count} {_plural_noun(noun, count)} of your choice")
    if template.grant.items:
        grants.append(f"{len(template.grant.items)} starting items")
    if template.grant.silver:
        grants.append(f"{template.grant.silver} silver")
    if template.grant.equip:
        grants.append("wearing " + ", ".join(template.grant.equip))
    return grants


def _restriction_view(restriction: Restriction | None) -> dict[str, Any] | None:
    """The gate a choice's options are restricted by. The page disables the options it rules out."""
    if restriction is None:
        return None
    return {
        "field": restriction.field_id,
        "values": list(restriction.values),
        "groups": list(restriction.groups),
    }


def _option_views(
    spec: CharacterSpec | CampaignSpec, options: tuple[TableOption, ...]
) -> list[dict[str, Any]]:
    """A choice shows a short title and a detail line. Named options keep their name as the
    title. Options that name a skill or a feature show those instead of a raw id. Other options
    show their description as the title."""
    skills = getattr(spec, "skills", {})
    features = getattr(spec, "features", {})
    views = []
    for option in options:
        title = option.name or option.description
        detail = option.description if option.name is not None else None
        if option.name is not None:
            if title == title.lower():
                title = title[:1].upper() + title[1:]
            prefix = f"{option.name}: "
            if detail and detail.lower().startswith(prefix.lower()):
                detail = detail[len(prefix) :].strip()
        elif option.value in skills:
            title = skills[option.value].name
            detail = skills[option.value].description
        elif option.value in features:
            title = features[option.value].name
            detail = features[option.value].description
        views.append(
            {
                "value": option.value,
                "title": title,
                "detail": detail,
                "summary": detail,
                "group": option.group,
            }
        )
    return views
