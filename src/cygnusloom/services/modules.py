"""Mini module cards and text.

A mini module is a small story seed with no world of its own. Full adventures are worlds. The
engine never tracks plot progress: storylines and notes remain the record. These helpers only
decide which seeds fit a campaign and what the GM reads when one is used.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from cygnusloom.content.library import (
    TABLE_GENRES,
    ModuleTemplate,
    table_genre_offered,
)
from cygnusloom.db.models import Campaign
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
        module_tags = {
            value.casefold() for values in module.tags.values() for value in values
        }
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


def module_text(module: ModuleTemplate) -> str:
    """The module's GM notes and prose, capped for a tool result."""
    parts = []
    if module.gm_notes:
        parts.append(f"GM notes: {module.gm_notes}")
    parts.append(module.body)
    return _cap("\n\n".join(parts), MAX_BODY_CHARS)


def module_state_text(
    modules: Mapping[str, ModuleTemplate],
    campaign: Campaign,
    state: CampaignState,
    *,
    default_genres: Sequence[str] = (),
) -> str:
    """The small state-prompt note that says how many mini modules are available."""
    available = available_modules(
        modules,
        campaign,
        state,
        default_genres=default_genres,
    )
    if not available:
        return ""
    return (
        f"Mini modules available: {len(available)}. Call list_modules when the story needs a new "
        "thread, then use_module to bring one in."
    )
