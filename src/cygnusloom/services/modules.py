"""Module cards, private notes, and text.

A campaign module owns its setting and is chosen at creation; its world content is used through
`ContentLibrary.setting_template`. A mini module is a small injectable story seed with no
setting. The engine never tracks plot progress: storylines and notes remain the record. These
helpers decide which seeds fit a campaign and what the GM reads when one is used.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from cygnusloom.content.library import (
    CREATIVE_CATEGORY_HELP,
    LOCATION_DENSITY_HELP,
    TABLE_GENRES,
    TONE_HELP,
    TONES,
    ModuleTemplate,
    table_genre_offered,
    tone_offered,
)
from cygnusloom.db.models import Campaign
from cygnusloom.services.common import InvalidInput
from cygnusloom.services.events import CampaignState

MAX_BODY_CHARS = 10000


def _campaign_selection(
    stored: str, default: Sequence[str], allowed: Sequence[str]
) -> tuple[str, ...]:
    """A campaign's JSON-string selection, falling back to the rule set's own choice."""
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
        dict.fromkeys(item for item in value if isinstance(item, str) and item in allowed)
    )
    return chosen or tuple(default)


def available_modules(
    modules: Mapping[str, ModuleTemplate],
    campaign: Campaign,
    state: CampaignState,
    *,
    default_genres: Sequence[str] = TABLE_GENRES,
    default_tones: Sequence[str] = TONES,
    category: str = "",
    tone: str = "",
) -> tuple[ModuleTemplate, ...]:
    """The mini modules this campaign may draw on, filtered by its categories and tones."""
    categories = _campaign_selection(campaign.table_genres, default_genres, TABLE_GENRES)
    tones = _campaign_selection(campaign.table_tones, default_tones, TONES)
    if category and category not in TABLE_GENRES:
        raise InvalidInput(f"there is no creative category named {category!r}")
    if tone and tone not in TONES:
        raise InvalidInput(f"there is no module tone named {tone!r}")
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
        if not table_genre_offered(categories, module.categories):
            continue
        if not tone_offered(tones, module.tones):
            continue
        if category and not table_genre_offered((category,), module.categories):
            continue
        if tone and tone not in module.tones:
            continue
        available.append(module)
    return tuple(available)


def module_card(module: ModuleTemplate) -> dict[str, Any]:
    """The public card the GM can browse without reading the module's prose."""
    return {
        "id": module.id,
        "name": module.name,
        "blurb": module.blurb or module.description,
        "categories": list(module.categories),
        "category_notes": [CREATIVE_CATEGORY_HELP[value] for value in module.categories],
        "tones": list(module.tones),
        "tone_notes": [TONE_HELP[value] for value in module.tones],
        "location_density": list(module.location_density),
        "location_density_notes": [
            LOCATION_DENSITY_HELP[value] for value in module.location_density
        ],
        "repeatable": module.repeatable,
    }


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def module_text(module: ModuleTemplate) -> str:
    """The module's private GM text, capped for a tool result."""
    return _cap(module.body, MAX_BODY_CHARS)


def module_state_text(
    modules: Mapping[str, ModuleTemplate],
    campaign: Campaign,
    state: CampaignState,
    *,
    default_genres: Sequence[str] = TABLE_GENRES,
    default_tones: Sequence[str] = TONES,
) -> str:
    """The campaign module's private notes plus the mini-module availability note."""
    blocks: list[str] = []
    campaign_module = modules.get(campaign.world_template)
    if campaign_module is not None and campaign_module.kind == "campaign" and campaign_module.body:
        blocks.append(_cap(f"Campaign module notes: {campaign_module.body}", 3000))
    available = available_modules(
        modules,
        campaign,
        state,
        default_genres=default_genres,
        default_tones=default_tones,
    )
    if available:
        blocks.append(
            f"Mini modules available: {len(available)}. Call list_modules when the story needs a "
            "new thread, then use_module to bring one in."
        )
    return "\n\n".join(blocks)
