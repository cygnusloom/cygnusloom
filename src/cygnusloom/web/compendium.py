"""The standalone compendium page: pick a rule set and a world that fits it."""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import Response

from cygnusloom.auth.deps import page_user
from cygnusloom.services.compendium import compendium_view
from cygnusloom.web.rendering import render

router = APIRouter(prefix="/compendium")


@router.get("")
def compendium_page(request: Request, rule_set: str = "", world_template: str = "") -> Response:
    """The compendium for a valid rule set and world. A request naming a rule set or world that
    does not fit it falls back to the first valid pair."""
    page_user(request)
    library = request.app.state.content
    if rule_set not in library.rule_sets:
        rule_set = next(iter(library.rule_sets), "")
    worlds = list(library.offered_worlds(rule_set))
    for module in library.offered_modules(rule_set, kind="campaign"):
        world = module.world_template
        if world.has_setting and all(existing.id != world.id for existing in worlds):
            worlds.append(world)
    if world_template not in {world.id for world in worlds}:
        world_template = worlds[0].id if worlds else ""
    recommended_ids = library.recommended_world_ids(rule_set)
    compendium = None
    if rule_set and world_template:
        compendium = compendium_view(library, rule_set, world_template)
    chosen_rule_set = library.rule_sets.get(rule_set)
    # The world picker shows the worlds recommended for the chosen rule set first, then the rest.
    # Its script regroups the options when the rule set changes, so every world is rendered.
    world_templates = list(library.world_templates.values())
    story_modules = library.offered_modules(rule_set, kind="mini")
    return render(
        request,
        "compendium.html",
        story_modules=story_modules,
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
