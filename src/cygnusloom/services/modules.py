"""Module cards, briefs, and the small amount of runtime bookkeeping modules need.

Modules are human-written story material. The engine never tracks plot progress: storylines and
notes remain the record. These helpers only decide what the GM can see, what is used, and which
campaign modules the owner has attached.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from cygnusloom.content.definitions import ContentError
from cygnusloom.content.library import (
    TABLE_GENRES,
    ContentLibrary,
    ModuleCast,
    ModuleClue,
    ModuleEnding,
    ModuleFront,
    ModuleHook,
    ModulePlace,
    ModuleScene,
    ModuleStoryline,
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
# It still has to name every card the GM can look up, including the cast and storylines.
MAX_BRIEF_CHARS = 3200
MAX_SECTION_CHARS = 3500
MAX_MATERIAL_CHARS = 7000


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


def _npc_name(module: ModuleTemplate, npc_id: str) -> str:
    for npc in module.world_template.npcs:
        if npc.id == npc_id:
            return npc.name
    return npc_id


def _place_name(module: ModuleTemplate, place_id: str) -> str:
    for place in module.world_template.locations:
        if place.id == place_id:
            return place.name
    return place_id


def _cards(names: Iterable[tuple[str, str]]) -> str:
    return "; ".join(f"{ident} ({name})" for ident, name in names)


def module_brief(module: ModuleTemplate) -> str:
    """A compact block for the state prompt: enough to know what the module is and what cards
    exist, without reading the material itself."""
    lines = [
        f"Module: {module.name} ({module.kind})",
        f"Premise: {module.premise or module.description}",
    ]
    if module.truth:
        lines.append(f"Truth: {module.truth}")
    if module.tone:
        lines.append(f"Tone: {module.tone}")
    if module.themes:
        lines.append("Themes: " + ", ".join(module.themes))
    if module.gm_notes:
        lines.append(f"GM notes: {module.gm_notes}")
    if module.start is not None:
        lines.append(f"Opening hooks: {module.start.hooks}")
    if module.scenes:
        lines.append("Scenes: " + _cards((s.id, s.name) for s in module.scenes.values()))
    if module.clues:
        lines.append("Clues: " + _cards((c.id, c.name) for c in module.clues.values()))
    if module.cast:
        lines.append("Cast: " + _cards((c.npc, _npc_name(module, c.npc)) for c in module.cast))
    if module.places:
        lines.append(
            "Places: "
            + _cards((p.location, _place_name(module, p.location)) for p in module.places)
        )
    if module.fronts:
        lines.append("Fronts: " + _cards((f.id, f.name) for f in module.fronts.values()))
    if module.endings:
        lines.append("Endings: " + _cards((e.id, e.name) for e in module.endings.values()))
    if module.storylines:
        lines.append(
            "Storyline seeds: "
            + _cards((s.id, s.name) for s in module.storylines.values())
        )
    lines.append(
        f'Use lookup_module("{module.id}", "scene:<id>") for one card, or "cast:<id>", '
        '"place:<id>", "clue:<id>", "front:<id>", "ending:<id>", or "storyline:<id>".'
    )
    return _cap("\n".join(lines), MAX_BRIEF_CHARS)


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _scene_text(scene: ModuleScene) -> str:
    lines = [f"Scene: {scene.name} ({scene.id})", scene.summary]
    if scene.goal:
        lines.append(f"Goal: {scene.goal}")
    if scene.setup:
        lines.append(f"Setup: {scene.setup}")
    if scene.location:
        lines.append(f"Location: {scene.location}")
    if scene.cast:
        lines.append("Cast: " + ", ".join(scene.cast))
    if scene.clues:
        lines.append("Clues: " + ", ".join(scene.clues))
    if scene.complications:
        lines.append("Complications: " + "; ".join(scene.complications))
    if scene.outcomes:
        lines.append("Possible outcomes: " + "; ".join(scene.outcomes))
    if scene.if_ignored:
        lines.append(f"If ignored: {scene.if_ignored}")
    if scene.next:
        lines.append("Suggested next: " + ", ".join(scene.next))
    if scene.gm_notes:
        lines.append(f"GM notes: {scene.gm_notes}")
    return _cap("\n".join(lines), MAX_SECTION_CHARS)


def _clue_text(clue: ModuleClue) -> str:
    lines = [f"Clue: {clue.name} ({clue.id})", clue.summary]
    for label, value in (
        ("Detail", clue.detail),
        ("Found at", clue.found_at),
        ("Reveals", clue.reveals),
        ("Leads to", clue.leads_to),
        ("NPC", clue.npc),
        ("Location", clue.location),
        ("Object", clue.object),
    ):
        if value:
            lines.append(f"{label}: {value}")
    return _cap("\n".join(lines), MAX_SECTION_CHARS)


def _front_text(front: ModuleFront) -> str:
    lines = [f"Front: {front.name} ({front.id})", front.summary]
    if front.segments:
        lines.append("Segments: " + "; ".join(front.segments))
    if front.advance_on:
        lines.append("Advance when: " + "; ".join(front.advance_on))
    if front.effects:
        lines.append("Effects: " + "; ".join(front.effects))
    return _cap("\n".join(lines), MAX_SECTION_CHARS)


def _ending_text(ending: ModuleEnding) -> str:
    lines = [f"Ending: {ending.name} ({ending.id})", ending.summary]
    for label, value in (
        ("When", ending.when),
        ("Outcome", ending.outcome),
    ):
        if value:
            lines.append(f"{label}: {value}")
    if ending.rewards:
        lines.append("Rewards: " + "; ".join(ending.rewards))
    if ending.consequences:
        lines.append("Consequences: " + "; ".join(ending.consequences))
    if ending.next_module:
        lines.append(f"Next module: {ending.next_module}")
    return _cap("\n".join(lines), MAX_SECTION_CHARS)


def _storyline_text(storyline: ModuleStoryline) -> str:
    lines = [f"Storyline seed: {storyline.name} ({storyline.id})", storyline.summary]
    if storyline.plot:
        lines.append(f"Plot: {storyline.plot}")
    if storyline.characters:
        lines.append("Characters: " + ", ".join(storyline.characters))
    if storyline.locations:
        lines.append("Locations: " + ", ".join(storyline.locations))
    if storyline.objects:
        lines.append("Objects: " + ", ".join(storyline.objects))
    if storyline.notes:
        lines.append(f"Notes: {storyline.notes}")
    if storyline.scenes:
        lines.append("Scenes: " + ", ".join(storyline.scenes))
    if storyline.focus:
        lines.append("This seed is the focused thread at the opening.")
    return _cap("\n".join(lines), MAX_SECTION_CHARS)


def _hook_text(hook: ModuleHook) -> str:
    lines = [f"Hook: {hook.name} ({hook.id})", hook.text]
    if hook.location:
        lines.append(f"Location: {hook.location}")
    if hook.patron:
        lines.append(f"Patron: {hook.patron}")
    if hook.reward:
        lines.append(f"Reward: {hook.reward}")
    if hook.tags:
        lines.append("Tags: " + ", ".join(hook.tags))
    return _cap("\n".join(lines), MAX_SECTION_CHARS)


def _cast_text(module: ModuleTemplate, member: ModuleCast) -> str:
    lines = [f"Cast: {_npc_name(module, member.npc)} ({member.npc})"]
    for label, value in (
        ("Role", member.role),
        ("Wants", member.wants),
        ("Secret", member.secret),
    ):
        if value:
            lines.append(f"{label}: {value}")
    if member.knows:
        lines.append("Knows: " + ", ".join(member.knows))
    if member.appears_in:
        lines.append("Appears in: " + ", ".join(member.appears_in))
    for npc in module.world_template.npcs:
        if npc.id != member.npc:
            continue
        lines.append(f"NPC record: {npc.name} at {_place_name(module, npc.location)}")
        lines.append(npc.description)
        for label, value in (
            ("Personality", npc.personality),
            ("Accent", npc.accent),
            ("Quirks", ", ".join(npc.quirks or ())),
            ("Statblock", npc.monster or ""),
        ):
            if value:
                lines.append(f"{label}: {value}")
        break
    return _cap("\n".join(lines), MAX_SECTION_CHARS)


def _place_text(module: ModuleTemplate, spot: ModulePlace) -> str:
    lines = [f"Place: {_place_name(module, spot.location)} ({spot.location})"]
    for label, value in (("Role", spot.role), ("Secret", spot.secret)):
        if value:
            lines.append(f"{label}: {value}")
    if spot.appears_in:
        lines.append("Appears in: " + ", ".join(spot.appears_in))
    for location in module.world_template.locations:
        if location.id != spot.location:
            continue
        if location.summary:
            lines.append(f"Summary: {location.summary}")
        lines.append(location.description)
        if location.map:
            lines.append(f"Local map template: {location.map}")
        break
    return _cap("\n".join(lines), MAX_SECTION_CHARS)


def module_section(module: ModuleTemplate, section: str = "") -> str:
    """One section of a module: its brief, a card, or all material for a small mini module."""
    name = (section or "brief").strip()
    if name == "brief":
        return module_brief(module)
    if name == "all":
        parts = [module_brief(module)]
        parts.extend(_scene_text(scene) for scene in module.scenes.values())
        parts.extend(_clue_text(clue) for clue in module.clues.values())
        parts.extend(_front_text(front) for front in module.fronts.values())
        parts.extend(_ending_text(ending) for ending in module.endings.values())
        parts.extend(_storyline_text(storyline) for storyline in module.storylines.values())
        return _cap("\n\n".join(parts), MAX_MATERIAL_CHARS)
    kind, _, ident = name.partition(":")
    if not ident:
        raise InvalidInput(
            'name a section such as "scene:<id>", "cast:<id>", "place:<id>", "clue:<id>", '
            '"front:<id>", "ending:<id>", "storyline:<id>", "brief", or "all"'
        )
    if kind == "scene":
        scene = module.scenes.get(ident)
        if scene is None:
            raise InvalidInput(f"module {module.id!r} has no scene {ident!r}")
        return _scene_text(scene)
    if kind == "clue":
        clue = module.clues.get(ident)
        if clue is None:
            raise InvalidInput(f"module {module.id!r} has no clue {ident!r}")
        return _clue_text(clue)
    if kind == "front":
        front = module.fronts.get(ident)
        if front is None:
            raise InvalidInput(f"module {module.id!r} has no front {ident!r}")
        return _front_text(front)
    if kind == "ending":
        ending = module.endings.get(ident)
        if ending is None:
            raise InvalidInput(f"module {module.id!r} has no ending {ident!r}")
        return _ending_text(ending)
    if kind == "storyline":
        storyline = module.storylines.get(ident)
        if storyline is None:
            raise InvalidInput(f"module {module.id!r} has no storyline seed {ident!r}")
        return _storyline_text(storyline)
    if kind == "hook":
        hook = module.hooks.get(ident)
        if hook is None:
            raise InvalidInput(f"module {module.id!r} has no hook {ident!r}")
        return _hook_text(hook)
    if kind == "cast":
        member = next((member for member in module.cast if member.npc == ident), None)
        if member is None:
            raise InvalidInput(f"module {module.id!r} has no cast member {ident!r}")
        return _cast_text(module, member)
    if kind == "place":
        spot = next((spot for spot in module.places if spot.location == ident), None)
        if spot is None:
            raise InvalidInput(f"module {module.id!r} has no place {ident!r}")
        return _place_text(module, spot)
    raise InvalidInput(f"unknown module section kind {kind!r}")


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


def module_tags_json(module: ModuleTemplate) -> str:
    return json.dumps({category: list(values) for category, values in module.tags.items()})


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
