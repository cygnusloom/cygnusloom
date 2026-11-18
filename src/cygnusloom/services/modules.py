"""Module cards, private notes, and text.

A campaign module owns its setting and is chosen at creation; its world content is used through
`ContentLibrary.setting_template`. A mini module is a small injectable story seed with no
setting. The engine never tracks plot progress: storylines and notes remain the record. These
helpers decide which seeds fit a campaign and what the GM reads when one is used.
"""

from __future__ import annotations

import json
import random
from collections.abc import Mapping, Sequence
from typing import Any

from cygnusloom.content.character import TableOption
from cygnusloom.content.library import (
    TABLE_GENRES,
    DiceTable,
    ModuleTemplate,
    table_genre_offered,
)
from cygnusloom.db.models import Campaign
from cygnusloom.rules.dice import roll_total
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import CampaignState

MAX_BODY_CHARS = 10000


def _campaign_genres(stored: str, default: Sequence[str]) -> tuple[str, ...]:
    """The campaign's genre selection, falling back to the rule set's own choice."""
    text = (stored or "").strip()
    if not text:
        return tuple(default)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return tuple(default)
    if not isinstance(value, list):
        return tuple(default)
    chosen = tuple(
        dict.fromkeys(item for item in value if isinstance(item, str) and item in TABLE_GENRES)
    )
    return chosen or tuple(default)


def available_modules(
    modules: Mapping[str, ModuleTemplate],
    campaign: Campaign,
    state: CampaignState,
    *,
    default_genres: Sequence[str] = (),
    tags: Sequence[str] = (),
    setting: str = "",
) -> tuple[ModuleTemplate, ...]:
    """The mini modules this campaign may draw on, filtered by its genres and supplied tags."""
    genres = _campaign_genres(campaign.table_genres, default_genres)
    wanted = {tag.casefold() for tag in tags if tag.strip()}
    setting_key = setting.strip().casefold()
    available: list[ModuleTemplate] = []
    for module in modules.values():
        if module.kind != "mini":
            continue
        if module.rule_set is not None and module.rule_set != campaign.rule_set:
            continue
        if (
            module.rule_set is None
            and module.recommended_rule_sets
            and campaign.rule_set not in module.recommended_rule_sets
        ):
            continue
        if module.id in state.used_modules and not module.repeatable:
            continue
        if not table_genre_offered(genres, module.tag_values("genres")):
            continue
        module_tags = {value.casefold() for values in module.tags.values() for value in values}
        if wanted and not wanted.intersection(module_tags):
            continue
        if setting_key and setting_key not in module_tags:
            continue
        available.append(module)
    return tuple(available)


def module_card(module: ModuleTemplate) -> dict[str, Any]:
    """The public card the GM can browse without reading the module's prose."""
    return {
        "id": module.id,
        "name": module.name,
        "blurb": module.blurb or module.description,
        "tags": {category: list(values) for category, values in module.tags.items()},
        "repeatable": module.repeatable,
    }


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def resolve_rolls(
    rolls: Mapping[str, str],
    character_tables: Mapping[str, Sequence[TableOption]],
    dice_tables: Mapping[str, DiceTable],
    rng: random.Random,
    *,
    plus: str,
) -> dict[str, str]:
    """Roll a mini module's placeholders once, without recording separate table events.

    A table may be a character table, such as the core `names` table, or a dice or creative
    table. Character-table results are kept distinct, so two names in one seed never collide."""
    values: dict[str, str] = {}
    taken: set[str] = set()
    for token, table_id in rolls.items():
        if table_id in character_tables:
            options = [option.value for option in character_tables[table_id]]
            if not options:
                raise InvalidInput(f"the table {table_id!r} has no options to roll")
            remaining = [option for option in options if option not in taken] or options
            value = rng.choice(remaining)
        elif table_id in dice_tables:
            table = dice_tables[table_id]
            value = table.row_for(roll_total(table.dice, rng, plus=plus)).result
        else:
            raise InvalidInput(
                f"the module asks for a table named {table_id!r}, which this campaign does not have"
            )
        values[token] = value
        taken.add(value)
    return values


def _fill_rolls(text: str, rolls: Mapping[str, str]) -> str:
    for token, value in rolls.items():
        text = text.replace("{{" + token + "}}", value)
    return text


def module_text(module: ModuleTemplate, rolls: Mapping[str, str] | None = None) -> str:
    """The module's GM notes and prose, with rolled placeholders filled, capped for a tool
    result."""
    values = rolls or {}
    parts = []
    if module.gm_notes:
        parts.append(f"GM notes: {_fill_rolls(module.gm_notes, values)}")
    parts.append(_fill_rolls(module.body, values))
    return _cap("\n\n".join(parts), MAX_BODY_CHARS)


def module_state_text(
    modules: Mapping[str, ModuleTemplate],
    campaign: Campaign,
    state: CampaignState,
    *,
    default_genres: Sequence[str] = (),
) -> str:
    """The campaign module's private notes plus the mini-module availability note."""
    blocks: list[str] = []
    campaign_module = modules.get(campaign.world_template)
    if campaign_module is not None and campaign_module.kind == "campaign":
        notes = campaign_module.gm_notes
        if campaign_module.body:
            notes = f"{notes}\n\n{campaign_module.body}".strip()
        if notes:
            blocks.append(_cap(f"Campaign module notes: {notes}", 3000))
    available = available_modules(
        modules,
        campaign,
        state,
        default_genres=default_genres,
    )
    if available:
        blocks.append(
            f"Mini modules available: {len(available)}. Call list_modules when the story needs a "
            "new thread, then use_module to bring one in."
        )
    return "\n\n".join(blocks)
