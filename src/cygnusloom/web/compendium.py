"""The standalone compendium page: pick a rule set, a world, and the creative sources."""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter, Request
from starlette.responses import Response

from cygnusloom.auth.deps import page_user
from cygnusloom.content.library import (
    CREATIVE_CATEGORY_HELP,
    TABLE_GENRES,
    TONE_HELP,
    TONES,
    table_genre_offered,
    tone_offered,
)
from cygnusloom.services.compendium import compendium_view
from cygnusloom.web.rendering import render

router = APIRouter(prefix="/compendium")


def _selection(
    values: Sequence[str], defaults: Sequence[str], allowed: Sequence[str]
) -> tuple[str, ...]:
    """A query-string checkbox selection, falling back to the rule set's own choice."""
    chosen = tuple(dict.fromkeys(value for value in values if value in allowed))
    return chosen or tuple(defaults)


@router.get("")
def compendium_page(request: Request, rule_set: str = "", world_template: str = "") -> Response:
    """The compendium for a valid rule set and world. A request naming a rule set or world that
    does not fit it falls back to the first valid pair. The creative-source filters default to the
    rule set's own choices, exactly as campaign creation does."""
    page_user(request)
    library = request.app.state.content
    if rule_set not in library.rule_sets:
        rule_set = next(iter(library.rule_sets), "")
    chosen_rule_set = library.rule_sets.get(rule_set)
    selected_genres = _selection(
        request.query_params.getlist("table_genres"),
        chosen_rule_set.table_genres if chosen_rule_set is not None else (),
        TABLE_GENRES,
    )
    selected_tones = _selection(
        request.query_params.getlist("table_tones"),
        chosen_rule_set.table_tones if chosen_rule_set is not None else (),
        TONES,
    )
    worlds = list(library.offered_worlds(rule_set))
    for module in library.offered_modules(rule_set, kind="campaign"):
        if module.world_template is not None and all(
            existing.id != module.world_template.id for existing in worlds
        ):
            worlds.append(module.world_template)
    if world_template not in {world.id for world in worlds}:
        world_template = worlds[0].id if worlds else ""
    recommended_ids = library.recommended_world_ids(rule_set)
    compendium = None
    if rule_set and world_template:
        compendium = compendium_view(
            library, rule_set, world_template, table_genres=selected_genres
        )
    # The world picker shows the worlds recommended for the chosen rule set first, then the rest.
    # Its script regroups the options when the rule set changes, so every world is rendered.
    world_templates = list(library.world_templates.values())
    world_templates.extend(
        module.world_template
        for module in library.offered_modules(rule_set, kind="campaign")
        if module.world_template is not None
    )
    story_modules = tuple(
        module
        for module in library.offered_modules(rule_set, kind="mini")
        if table_genre_offered(selected_genres, module.categories)
        and tone_offered(selected_tones, module.tones)
    )
    return render(
        request,
        "compendium.html",
        story_modules=story_modules,
        table_genres=library.available_table_genres,
        selected_table_genres=selected_genres,
        table_tones=chosen_rule_set.table_tones if chosen_rule_set is not None else (),
        selected_table_tones=selected_tones,
        category_help=CREATIVE_CATEGORY_HELP,
        tone_help=TONE_HELP,
        rule_sets=library.rule_sets.values(),
        recommended_worlds=[world for world in world_templates if world.id in recommended_ids],
        other_worlds=[world for world in world_templates if world.id not in recommended_ids],
        world_recommendations={
            world.id: library.recommending_rule_sets(world.id) for world in world_templates
        },
        selected_rule_set=rule_set,
        selected_rule_set_name=(chosen_rule_set.name if chosen_rule_set else ""),
        selected_world=world_template,
        compendium=compendium,
        standalone=True,
        error="",
    )
