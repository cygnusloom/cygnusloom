"""The standalone compendium page: pick a rule set and a world that fits it."""

from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import Response

from cygnusforge.auth.deps import page_user
from cygnusforge.services.compendium import compendium_view
from cygnusforge.web.rendering import render

router = APIRouter(prefix="/compendium")


@router.get("")
def compendium_page(request: Request, rule_set: str = "", world_template: str = "") -> Response:
    """The compendium for a valid rule set and world. A request naming a rule set or world that
    does not fit it falls back to the first valid pair."""
    page_user(request)
    library = request.app.state.content
    if rule_set not in library.rule_sets:
        rule_set = next(iter(library.rule_sets), "")
    worlds = [
        world
        for world in library.world_templates.values()
        if world.rule_set is None or world.rule_set == rule_set
    ]
    if world_template not in {world.id for world in worlds}:
        world_template = worlds[0].id if worlds else ""
    compendium = None
    if rule_set and world_template:
        compendium = compendium_view(library, rule_set, world_template)
    return render(
        request,
        "compendium.html",
        rule_sets=library.rule_sets.values(),
        worlds=library.world_templates.values(),
        selected_rule_set=rule_set,
        selected_world=world_template,
        compendium=compendium,
        standalone=True,
        error="",
    )
