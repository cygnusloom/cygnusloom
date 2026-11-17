"""Module cards, briefs, and the small amount of runtime bookkeeping modules need.

Modules are human-written story material. The engine never tracks plot progress: storylines and
notes remain the record. These helpers only decide what the GM can see, what is used, and which
campaign modules the owner has attached.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from cygnusloom.content.definitions import ContentError
from cygnusloom.content.library import (
    TABLE_GENRES,
    ContentLibrary,
    ModuleTemplate,
    table_genre_offered,
)
from cygnusloom.db.models import Campaign
from cygnusloom.services.common import InvalidInput, session_scope
from cygnusloom.services.events import (
    CampaignState,
    ModuleAttached,
    ModuleDetached,
    load_state,
    record,
)

# A module brief is shown every turn while its campaign module is active, so it stays compact.
MAX_BRIEF_CHARS = 2400
# A module's own prose is read only when it is used or looked up.
MAX_BODY_CHARS = 10000


def active_campaign_module_ids(campaign: Campaign, state: CampaignState) -> tuple[str, ...]:
    """The starting module and every attached campaign module, in order, without duplicates."""
    ids: list[str] = []
    if campaign.module:
        ids.append(campaign.module)
    for module_id in state.modules:
        if module_id not in ids:
            ids.append(module_id)
    return tuple(ids)


def active_campaign_modules(
    modules: Mapping[str, ModuleTemplate], campaign: Campaign, state: CampaignState
) -> tuple[ModuleTemplate, ...]:
    found: list[ModuleTemplate] = []
    for module_id in active_campaign_module_ids(campaign, state):
        module = modules.get(module_id)
        if module is not None and module.kind == "campaign":
            found.append(module)
    return tuple(found)


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


def available_mini_modules(
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
        if module.world is not None and module.world != campaign.world_template:
            continue
        if module.recommended_worlds and campaign.world_template not in module.recommended_worlds:
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
    """The public card the GM can browse without reading the module's material."""
    return {
        "id": module.id,
        "name": module.name,
        "kind": module.kind,
        "blurb": module.blurb or module.description,
        "tags": {category: list(values) for category, values in module.tags.items()},
        "repeatable": module.repeatable,
    }


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def module_brief(module: ModuleTemplate) -> str:
    """A compact block for the state prompt: what the module is, and where its material lives."""
    lines = [
        f"Module: {module.name} ({module.kind})",
        f"Description: {module.description}",
    ]
    if module.gm_notes:
        lines.append(f"GM notes: {module.gm_notes}")
    if module.start is not None:
        lines.append(f"Opening hooks: {module.start.hooks}")
    if module.body:
        lines.append(f'Call lookup_module("{module.id}") for the module text.')
    if module.world_template.has_setting:
        lines.append(
            "Its places, people, and rules are ordinary world entries: use the normal lookup "
            "tools rather than re-reading the module."
        )
    return _cap("\n".join(lines), MAX_BRIEF_CHARS)


def module_text(module: ModuleTemplate) -> str:
    """The module's brief plus its own prose. A campaign module often has no body; its material
    is the embedded world's locations, NPCs, and rules."""
    parts = [module_brief(module)]
    if module.body:
        parts.append(module.body)
    return _cap("\n\n".join(parts), MAX_BODY_CHARS)


def module_state_text(
    modules: Mapping[str, ModuleTemplate],
    campaign: Campaign,
    state: CampaignState,
    *,
    default_genres: Sequence[str] = (),
) -> str:
    """The compact Module block for the state and opening prompts. It is never part of the
    narration step's prompt."""
    blocks = [
        module_brief(module)
        for module in active_campaign_modules(modules, campaign, state)
    ]
    mini = available_mini_modules(
        modules,
        campaign,
        state,
        default_genres=default_genres,
    )
    if mini:
        blocks.append(
            f"Mini modules available: {len(mini)}. Call list_modules when the story needs a new "
            "thread, then use_module to bring one in."
        )
    return _cap("\n\n".join(blocks), 6000)


def _check_campaign_module(module: ModuleTemplate, campaign: Campaign) -> None:
    if module.kind != "campaign":
        raise InvalidInput("that is a mini module; use it through the GM instead")
    if module.rule_set is not None and module.rule_set != campaign.rule_set:
        raise InvalidInput(
            f"that module is built for the {module.rule_set} rule set, not {campaign.rule_set}"
        )
    if module.recommended_rule_sets and campaign.rule_set not in module.recommended_rule_sets:
        raise InvalidInput("that module is not built for this campaign's rule set")
    if module.world is not None and module.world != campaign.world_template:
        raise InvalidInput("that module is built for a different world")
    if module.recommended_worlds and campaign.world_template not in module.recommended_worlds:
        raise InvalidInput("that module is not built for this campaign's world")


def set_attached_modules(
    engine: Any,
    library: ContentLibrary,
    owner_id: int,
    campaign_id: int,
    module_ids: Sequence[str],
) -> None:
    """Attach and detach campaign modules in one owner action. The starting module stays."""
    from cygnusloom.services.campaigns import get_owned_campaign

    campaign = get_owned_campaign(engine, owner_id, campaign_id)
    wanted: list[str] = []
    for module_id in module_ids:
        module = library.modules.get(module_id)
        if module is None:
            raise InvalidInput(f"there is no module named {module_id!r}")
        _check_campaign_module(module, campaign)
        try:
            library.world_content_for(module.world_template, campaign.rule_set)
        except ContentError as error:
            raise InvalidInput(
                f"that module does not work with this campaign's rule set: {error}"
            ) from error
        if module_id not in wanted:
            wanted.append(module_id)
    with session_scope(engine) as db:
        state = load_state(db, campaign_id)
        if state.turn is not None or state.encounter is not None:
            raise InvalidInput("story modules cannot change during a turn or an encounter")
        current = set(state.modules)
        target = set(wanted)
        for module_id in wanted:
            if module_id not in current:
                record(db, campaign_id, ModuleAttached(module=module_id))
        for module_id in current - target:
            if module_id == campaign.module:
                continue
            record(db, campaign_id, ModuleDetached(module=module_id))
        db.commit()


def campaign_module_options(
    library: ContentLibrary, campaign: Campaign
) -> tuple[ModuleTemplate, ...]:
    """Campaign modules the owner may attach, including the starting one."""
    offered = list(
        library.offered_modules(
            campaign.rule_set, kind="campaign", world=campaign.world_template
        )
    )
    starting = library.modules.get(campaign.module)
    if starting is not None and all(module.id != starting.id for module in offered):
        offered.insert(0, starting)
    return tuple(offered)
