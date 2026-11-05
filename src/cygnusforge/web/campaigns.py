"""Campaign pages: list, create, and chat."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from starlette.responses import Response

from cygnusforge.auth.deps import page_user, verify_csrf
from cygnusforge.content.character import NO_CAMPAIGN, NO_CHARACTER, character_schema
from cygnusforge.db.models import User
from cygnusforge.llm.base import LLMError
from cygnusforge.services.campaigns import (
    Delta,
    Logged,
    NewCharacter,
    NextMessage,
    Prompt,
    Retract,
    Saved,
    Status,
    TurnEvent,
    begin_encounter_turn,
    campaign_spec,
    campaign_transcript,
    campaign_world,
    character_spec,
    context_usage,
    create_campaign,
    default_campaign_labels,
    end_encounter_turn,
    get_campaign,
    get_turn_context,
    get_turn_transcript,
    list_campaigns,
    list_messages,
    pending_turn,
    promote_hireling_to_character,
    resume_turn,
    roll_player_stats,
    rollback_to,
    save_creation,
    send_message,
    set_campaign_label,
    set_campaign_model,
    set_debug_currency,
    set_debug_invincibility,
    set_debug_stat,
    start_encounter_turn,
    stream_message,
)
from cygnusforge.services.campaigns import (
    delete_campaign as delete_campaign_service,
)
from cygnusforge.services.campaigns import (
    discard_pending as discard_pending_turn,
)
from cygnusforge.services.common import Conflict, InvalidInput, NotFound, session_scope
from cygnusforge.services.compendium import compendium_view
from cygnusforge.services.encounters import encounter_view
from cygnusforge.services.events import last_event_id, load_state, speaker_name
from cygnusforge.services.tools import check_arguments
from cygnusforge.services.views import map_view, scene_view, time_view
from cygnusforge.web.character_form import (
    campaign_submission_from_form,
    campaign_view,
    creation_view,
    pronouns_from_form,
    submission_from_form,
)
from cygnusforge.web.forms import form_text
from cygnusforge.web.rendering import render

router = APIRouter(prefix="/campaigns")


@router.get("")
def campaigns_page(request: Request) -> Response:
    user = page_user(request)
    campaigns = list_campaigns(request.app.state.engine, user.id)
    return render(request, "campaigns.html", campaigns=campaigns)


@router.get("/new")
def new_campaign_page(request: Request) -> Response:
    user = page_user(request)
    if not request.app.state.catalog.model_ids():
        return _new_form(
            request,
            user,
            status_code=503,
            error="No language model is configured. An administrator must add one under Admin → Models.",
        )
    return _new_form(request, user)


@router.post("/new")
async def create(request: Request) -> Response:
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    label = form_text(form, "label")
    world_template = form_text(form, "world_template")
    rule_set = form_text(form, "rule_set")
    setting = form_text(form, "setting")
    catalog = request.app.state.catalog
    models = catalog.model_ids()
    if not models:
        return _new_form(
            request,
            user,
            status_code=503,
            error="No language model is configured. An administrator must add one under Admin → Models.",
        )
    try:
        campaign = create_campaign(
            request.app.state.engine,
            request.app.state.content,
            models,
            user.id,
            label,
            world_template,
            rule_set,
            catalog.default_model_id(),
            setting=setting,
        )
    except InvalidInput as exc:
        return _new_form(
            request,
            user,
            status_code=400,
            error=str(exc),
            label=label,
            world_template=world_template,
            rule_set=rule_set,
            setting=setting,
        )
    return RedirectResponse(f"/campaigns/{campaign.id}", status_code=303)


@router.post("/{campaign_id}/label")
async def change_campaign_label(request: Request, campaign_id: int) -> Response:
    """Change the label the campaign list shows."""
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    try:
        set_campaign_label(request.app.state.engine, user.id, campaign_id, form_text(form, "label"))
    except InvalidInput as exc:
        _flash(request, str(exc), "error")
        return RedirectResponse("/campaigns", status_code=303)
    _flash(request, "Label saved.")
    return RedirectResponse("/campaigns", status_code=303)


@router.post("/{campaign_id}/delete")
async def delete_campaign(request: Request, campaign_id: int) -> Response:
    """Delete one of the player's own campaigns after they type its label to confirm."""
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    campaign = get_campaign(request.app.state.engine, user.id, campaign_id)
    if form_text(form, "confirm").strip() != campaign.name:
        _flash(request, "Type the campaign label exactly to confirm deletion.", "error")
        return RedirectResponse("/campaigns", status_code=303)
    try:
        delete_campaign_service(request.app.state.engine, user.id, campaign_id)
    except Conflict as exc:
        _flash(request, str(exc), "error")
        return RedirectResponse("/campaigns", status_code=303)
    _flash(request, "Campaign deleted.")
    return RedirectResponse("/campaigns", status_code=303)


@router.post("/{campaign_id}/character/reroll")
async def reroll_character_stats(request: Request, campaign_id: int) -> Response:
    user = page_user(request)
    await verify_csrf(request)
    try:
        _roll_stats(request, user, campaign_id)
    except InvalidInput as exc:
        return _campaign_page(request, user, campaign_id, 400, str(exc))
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.post("/{campaign_id}/hirelings/promote")
async def promote_hireling(request: Request, campaign_id: int) -> Response:
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    engine = request.app.state.engine
    try:
        promote_hireling_to_character(engine, user.id, campaign_id, form_text(form, "name"))
    except InvalidInput as exc:
        return _campaign_page(request, user, campaign_id, 400, str(exc))
    except Conflict as exc:
        return _campaign_page(request, user, campaign_id, 409, str(exc))
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.post("/{campaign_id}/creation")
async def create_campaign_setup(request: Request, campaign_id: int) -> Response:
    """Save the campaign options and the player character from the one creation form. Whichever
    parts are still pending are saved together, or neither is."""
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    engine = request.app.state.engine
    library = request.app.state.content
    key = _roll_key(campaign_id)
    try:
        campaign = get_campaign(engine, user.id, campaign_id)
        options_pending, character_pending = _creation_pending(
            campaign_world(engine, user.id, campaign_id).state,
            library.rule_sets.get(campaign.rule_set),
        )
        if not options_pending and not character_pending:
            raise InvalidInput("this campaign's options and player character are already set")
        options = None
        if options_pending:
            options = campaign_submission_from_form(campaign_spec(library, campaign), form)
        character = None
        if character_pending:
            rolled = request.session.get(key)
            if not isinstance(rolled, dict):
                _roll_stats(request, user, campaign_id)
                return _campaign_page(
                    request,
                    user,
                    campaign_id,
                    400,
                    "Your stats were rolled again. Review them and create your character.",
                )
            spec = character_spec(library, campaign)
            submitted = submission_from_form(spec, form)
            problem = check_arguments(character_schema(spec), submitted)
            if problem:
                raise InvalidInput(problem)
            character = NewCharacter(
                name=form_text(form, "name"),
                rolled=rolled,
                submitted=submitted,
                pronouns=pronouns_from_form(form),
            )
        save_creation(engine, library, user.id, campaign_id, options=options, character=character)
    except InvalidInput as exc:
        return _campaign_page(request, user, campaign_id, 400, str(exc))
    except Conflict as exc:
        return _campaign_page(request, user, campaign_id, 409, str(exc))
    if character is not None:
        request.session.pop(key, None)
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.get("/{campaign_id}")
def campaign_page(request: Request, campaign_id: int) -> Response:
    user = page_user(request)
    return _campaign_page(request, user, campaign_id)


@router.get("/{campaign_id}/turn-status")
def turn_status(request: Request, campaign_id: int) -> Response:
    """Whether the GM is still working on the campaign's open turn.

    The page polls this while it streams a reply. A stream can die without ending, or fall silent
    for minutes behind a slow provider, and then the player's composer stays disabled even though
    the turn is over. The poll sees the turn end and reloads the page.
    """
    user = page_user(request)
    engine = request.app.state.engine
    get_campaign(engine, user.id, campaign_id)
    with session_scope(engine) as db:
        turn = load_state(db, campaign_id).turn
        newest = last_event_id(db, campaign_id)
    if turn is None:
        status = "idle"
    elif turn.failed is not None:
        status = "failed"
    elif turn.prompt is not None:
        status = "prompt"
    else:
        status = "busy"
    return JSONResponse({"status": status, "event": newest})


@router.get("/{campaign_id}/map")
def map_fragment(request: Request, campaign_id: int) -> Response:
    """The map panel on its own, so the page can redraw it while the GM is still working.

    Creating an encounter takes the GM a dozen tool calls: the map, the joining sides, initiative,
    and every placement. Refreshing this fragment during the turn lets the player watch the board
    fill in instead of waiting for the finished scene.
    """
    user = page_user(request)
    engine = request.app.state.engine
    library = request.app.state.content
    campaign = get_campaign(engine, user.id, campaign_id)
    rule_set = library.rule_sets.get(campaign.rule_set)
    world = campaign_world(engine, user.id, campaign.id)
    encounter = encounter_view(
        world.state,
        rule_set.grid if rule_set else None,
        rule_set.combat if rule_set else None,
        rule_set.terrain if rule_set else None,
        rule_set.encounter_objects if rule_set else None,
    )
    return render(
        request,
        "campaign_map.html",
        world_map=map_view(world.state, rule_set.world_terrain if rule_set else None),
        time=time_view(world.state, rule_set.clock if rule_set else None),
        encounter=encounter,
        encounter_rows=_encounter_rows(encounter) if encounter is not None else [],
    )


@router.post("/{campaign_id}/model")
async def change_model(request: Request, campaign_id: int) -> Response:
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    model = form_text(form, "model")
    try:
        set_campaign_model(
            request.app.state.engine,
            request.app.state.catalog.model_ids(),
            user.id,
            campaign_id,
            model,
        )
    except InvalidInput as exc:
        return _campaign_page(request, user, campaign_id, 400, str(exc))
    except Conflict as exc:
        return _campaign_page(request, user, campaign_id, 409, str(exc))
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.post("/{campaign_id}/debug/invincibility")
async def change_debug_invincibility(request: Request, campaign_id: int) -> Response:
    settings = request.app.state.settings
    if not settings.debug_panel:
        raise NotFound("not found")
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    name = form_text(form, "name")
    active = form_text(form, "active") == "1"
    try:
        set_debug_invincibility(request.app.state.engine, user.id, campaign_id, name, active)
    except InvalidInput as exc:
        return _campaign_page(request, user, campaign_id, 400, str(exc))
    except Conflict as exc:
        return _campaign_page(request, user, campaign_id, 409, str(exc))
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.post("/{campaign_id}/debug/stat")
async def change_debug_stat(request: Request, campaign_id: int) -> Response:
    settings = request.app.state.settings
    if not settings.debug_panel:
        raise NotFound("not found")
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    name = form_text(form, "name")
    stat = form_text(form, "stat")
    try:
        remaining = int(form_text(form, "remaining"))
    except ValueError:
        return _campaign_page(request, user, campaign_id, 400, "remaining must be a whole number")
    try:
        set_debug_stat(request.app.state.engine, user.id, campaign_id, name, stat, remaining)
    except InvalidInput as exc:
        return _campaign_page(request, user, campaign_id, 400, str(exc))
    except Conflict as exc:
        return _campaign_page(request, user, campaign_id, 409, str(exc))
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.post("/{campaign_id}/debug/currency")
async def change_debug_currency(request: Request, campaign_id: int) -> Response:
    settings = request.app.state.settings
    if not settings.debug_panel:
        raise NotFound("not found")
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    name = form_text(form, "name")
    library = request.app.state.content
    campaign = get_campaign(request.app.state.engine, user.id, campaign_id)
    rule_set = library.rule_sets.get(campaign.rule_set)
    if rule_set is None or rule_set.marketplace is None:
        return _campaign_page(request, user, campaign_id, 400, "this rule set has no currency")
    try:
        amount = int(form_text(form, "amount"))
    except ValueError:
        return _campaign_page(request, user, campaign_id, 400, "amount must be a whole number")
    try:
        set_debug_currency(
            request.app.state.engine,
            user.id,
            campaign_id,
            name,
            rule_set.marketplace.currency,
            amount,
        )
    except InvalidInput as exc:
        return _campaign_page(request, user, campaign_id, 400, str(exc))
    except Conflict as exc:
        return _campaign_page(request, user, campaign_id, 409, str(exc))
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.get("/{campaign_id}/messages/{event_id}/context")
def turn_context_fragment(request: Request, campaign_id: int, event_id: int) -> Response:
    user = page_user(request)
    parts = get_turn_context(request.app.state.engine, user.id, campaign_id, event_id)
    return render(request, "turn_context.html", parts=parts)


@router.get("/{campaign_id}/messages/{event_id}/transcript")
def turn_transcript_fragment(request: Request, campaign_id: int, event_id: int) -> Response:
    user = page_user(request)
    entries = get_turn_transcript(request.app.state.engine, user.id, campaign_id, event_id)
    return render(request, "turn_transcript.html", entries=entries)


@router.post("/{campaign_id}/messages/{event_id}/rollback")
async def rollback(request: Request, campaign_id: int, event_id: int) -> Response:
    user = page_user(request)
    await verify_csrf(request)
    try:
        rollback_to(request.app.state.engine, user.id, campaign_id, event_id)
    except Conflict as exc:
        return _campaign_page(request, user, campaign_id, 409, str(exc))
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.post("/{campaign_id}/messages")
async def post_message(request: Request, campaign_id: int) -> Response:
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    text = form_text(form, "content")
    settings = request.app.state.settings
    try:
        await send_message(
            request.app.state.engine,
            request.app.state.providers,
            request.app.state.catalog.model_ids(),
            request.app.state.content,
            settings.llm_context_tokens,
            user.id,
            campaign_id,
            text,
        )
    except InvalidInput as exc:
        return _campaign_page(request, user, campaign_id, 400, str(exc), text)
    except Conflict as exc:
        return _campaign_page(request, user, campaign_id, 409, str(exc), text)
    except LLMError as exc:
        return _campaign_page(request, user, campaign_id, 502, str(exc), text)
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.post("/{campaign_id}/messages/stream")
async def post_message_stream(request: Request, campaign_id: int) -> Response:
    """Newline-delimited JSON events for one turn. Errors are reported as an event, because the
    response has already started by the time they happen."""
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    text = form_text(form, "content")
    settings = request.app.state.settings
    events = stream_message(
        request.app.state.engine,
        request.app.state.providers,
        request.app.state.catalog.model_ids(),
        request.app.state.content,
        settings.llm_context_tokens,
        user.id,
        campaign_id,
        text,
    )
    return StreamingResponse(
        _turn_lines(events),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/{campaign_id}/pending/discard")
async def discard_pending(request: Request, campaign_id: int) -> Response:
    user = page_user(request)
    await verify_csrf(request)
    try:
        discard_pending_turn(request.app.state.engine, user.id, campaign_id)
    except InvalidInput as exc:
        return _campaign_page(request, user, campaign_id, 400, str(exc))
    except Conflict as exc:
        return _campaign_page(request, user, campaign_id, 409, str(exc))
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.post("/{campaign_id}/pending/stream")
async def post_pending_stream(request: Request, campaign_id: int) -> Response:
    """Answer the GM's prompt: `kind` is "roll", "text", or "continue", and `content` is the
    player's free-form reply for "text". Streams events the same way as a new message."""
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    action = form_text(form, "kind")
    text = form_text(form, "content")
    settings = request.app.state.settings
    events = resume_turn(
        request.app.state.engine,
        request.app.state.providers,
        request.app.state.catalog.model_ids(),
        request.app.state.content,
        settings.llm_context_tokens,
        user.id,
        campaign_id,
        action,
        text,
    )
    return StreamingResponse(
        _turn_lines(events),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/{campaign_id}/encounter/turn/stream")
async def post_encounter_turn_stream(request: Request, campaign_id: int) -> Response:
    """Start an NPC's encounter turn. The GM plays it out, streamed the same way as a reply."""
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    name = form_text(form, "name")
    settings = request.app.state.settings
    events = start_encounter_turn(
        request.app.state.engine,
        request.app.state.providers,
        request.app.state.catalog.model_ids(),
        request.app.state.content,
        settings.llm_context_tokens,
        user.id,
        campaign_id,
        name,
    )
    return StreamingResponse(
        _turn_lines(events),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/{campaign_id}/encounter/turn/begin")
async def post_encounter_turn_begin(request: Request, campaign_id: int) -> Response:
    """Start a player character's turn. The player then acts and ends it themselves."""
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    name = form_text(form, "name")
    try:
        begin_encounter_turn(request.app.state.engine, user.id, campaign_id, name)
    except InvalidInput as exc:
        return _campaign_page(request, user, campaign_id, 400, str(exc))
    except Conflict as exc:
        return _campaign_page(request, user, campaign_id, 409, str(exc))
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.post("/{campaign_id}/encounter/turn/end")
async def post_encounter_turn_end(request: Request, campaign_id: int) -> Response:
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    name = form_text(form, "name")
    try:
        end_encounter_turn(request.app.state.engine, user.id, campaign_id, name)
    except InvalidInput as exc:
        return _campaign_page(request, user, campaign_id, 400, str(exc))
    except Conflict as exc:
        return _campaign_page(request, user, campaign_id, 409, str(exc))
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


async def _turn_lines(events: AsyncIterator[TurnEvent]) -> AsyncIterator[str]:
    try:
        async for event in events:
            yield _line(event) + "\n"
    except InvalidInput as exc:
        yield _line(TurnError(400, str(exc))) + "\n"
    except NotFound as exc:
        yield _line(TurnError(404, str(exc))) + "\n"
    except Conflict as exc:
        yield _line(TurnError(409, str(exc))) + "\n"
    except LLMError as exc:
        yield _line(TurnError(502, str(exc))) + "\n"


@dataclass(frozen=True)
class TurnError:
    status: int
    message: str


def _line(event: TurnEvent | TurnError) -> str:
    match event:
        case Status(text):
            payload: dict[str, object] = {"type": "status", "text": text}
        case Delta(text):
            payload = {"type": "delta", "text": text}
        case Logged(text, hidden):
            payload = {"type": "log", "text": text, "hidden": hidden}
        case Prompt(text, dice, details):
            payload = {"type": "prompt", "text": text, "dice": dice, "details": details}
        case Saved(event_id):
            payload = {"type": "saved", "event_id": event_id}
        case NextMessage():
            payload = {"type": "next_message"}
        case Retract():
            payload = {"type": "retract"}
        case TurnError(status, message):
            payload = {"type": "error", "status": status, "message": message}
    return json.dumps(payload)


def _flash(request: Request, message: str, kind: str = "info") -> None:
    request.session["flash"] = {"message": message, "kind": kind}


def _new_form(
    request: Request,
    user: User,
    status_code: int = 200,
    error: str = "",
    label: str = "",
    world_template: str = "",
    rule_set: str = "",
    setting: str = "",
) -> Response:
    library = request.app.state.content
    # The rule set comes first and decides which worlds are offered, so start from a rule set
    # and a world that fits it. A world with no rule set fits every rule set.
    if rule_set not in library.rule_sets:
        rule_set = next(iter(library.rule_sets), "")
    worlds = [
        world
        for world in library.world_templates.values()
        if world.rule_set is None or world.rule_set == rule_set
    ]
    if world_template not in {world.id for world in worlds}:
        world_template = worlds[0].id if worlds else ""
    chosen_rule_set = library.rule_sets.get(rule_set)
    chosen_world = library.world_templates.get(world_template)
    default_labels = default_campaign_labels(request.app.state.engine, library, user.id)
    return render(
        request,
        "new_campaign.html",
        status_code=status_code,
        error=error,
        campaign_label=label or default_labels.get(world_template, ""),
        campaign_setting=setting,
        world_labels=default_labels,
        world_templates=library.world_templates.values(),
        rule_sets=library.rule_sets.values(),
        selected_world=world_template,
        selected_rule_set=rule_set,
        selected_world_blurb=(
            (chosen_world.blurb or chosen_world.description) if chosen_world else ""
        ),
        selected_rule_set_blurb=(
            (chosen_rule_set.blurb or chosen_rule_set.description) if chosen_rule_set else ""
        ),
    )


def _campaign_page(
    request: Request,
    user: User,
    campaign_id: int,
    status_code: int = 200,
    error: str = "",
    draft: str = "",
) -> Response:
    engine = request.app.state.engine
    settings = request.app.state.settings
    library = request.app.state.content
    campaign = get_campaign(engine, user.id, campaign_id)
    rule_set = library.rule_sets.get(campaign.rule_set)
    inventory_rules = rule_set.inventory if rule_set is not None else None
    magic = rule_set.magic if rule_set is not None else None
    spec = rule_set.character if rule_set else NO_CHARACTER
    options = rule_set.campaign if rule_set else NO_CAMPAIGN
    world = campaign_world(engine, user.id, campaign.id)
    compendium = compendium_view(library, campaign.rule_set, campaign.world_template, world.state)
    world_map = map_view(world.state, rule_set.world_terrain if rule_set else None)
    options_pending, character_pending = _creation_pending(world.state, rule_set)
    pc = world.state.pc()
    if options_pending or character_pending:
        promotable = [
            c
            for c in world.state.characters.values()
            if c.hireling and not c.dead and c.dying_hours is None and c.dying_rounds is None
        ]
        character = None
        if character_pending and rule_set is not None:
            rolled = request.session.get(_roll_key(campaign.id))
            rolled_stats = {s.id for s in spec.stats.values() if s.derived is None}
            if not isinstance(rolled, dict) or set(rolled) != rolled_stats:
                rolled = _roll_stats(request, user, campaign.id)
            character = creation_view(spec, rolled)
        return render(
            request,
            "creation.html",
            status_code=status_code,
            campaign=campaign,
            world=library.world_templates.get(campaign.world_template),
            scene=scene_view(
                world.state,
                spec,
                inventory_rules,
                rule_set.relations if rule_set else None,
                item_links=compendium.item_links,
                ability_links=compendium.ability_links,
                creature_links=compendium.creature_links,
                place_links=compendium.place_links,
                sheet_links=compendium.sheet_links,
            ),
            world_map=world_map,
            rule_set=rule_set,
            compendium=compendium,
            show_options=options_pending,
            options=campaign_view(options) if options_pending else None,
            show_character=character_pending,
            character=character,
            fallen=pc,
            promotable=promotable,
            error=error,
        )
    messages = list_messages(engine, campaign.id)
    transcript = campaign_transcript(engine, user.id, campaign.id)
    world_content = library.world_content(campaign.world_template, campaign.rule_set)
    context = context_usage(
        campaign,
        world.state,
        messages,
        rule_set,
        options,
        settings.llm_context_tokens,
        world_content.rules,
        unattached_world_rules=world_content.unattached_rules,
    )
    encounter = encounter_view(
        world.state,
        rule_set.grid if rule_set else None,
        rule_set.combat if rule_set else None,
        rule_set.terrain if rule_set else None,
        rule_set.encounter_objects if rule_set else None,
    )
    return render(
        request,
        "campaign.html",
        status_code=status_code,
        campaign=campaign,
        world=library.world_templates.get(campaign.world_template),
        scene=scene_view(
            world.state,
            spec,
            inventory_rules,
            rule_set.relations if rule_set else None,
            item_links=compendium.item_links,
            ability_links=compendium.ability_links,
            creature_links=compendium.creature_links,
            place_links=compendium.place_links,
            sheet_links=compendium.sheet_links,
        ),
        world_map=world_map,
        time=time_view(world.state, rule_set.clock if rule_set else None),
        rule_set=rule_set,
        compendium=compendium,
        creature_links=compendium.creature_links,
        place_links=compendium.place_links,
        messages=messages,
        transcript=transcript,
        player_name=pc.name,
        acting_name=speaker_name(world.state) or pc.name,
        context=context,
        error=error,
        draft=draft,
        pending=pending_turn(engine, user.id, campaign.id),
        models=request.app.state.catalog.model_ids(),
        encounter=encounter,
        encounter_rows=_encounter_rows(encounter) if encounter is not None else [],
        debug_panel=settings.debug_panel,
        debug_currency=rule_set.marketplace.currency if rule_set and rule_set.marketplace else "",
        debug_stats=[(stat.id, stat.name) for stat in spec.stats.values()],
        pc_invincible=pc.invincible,
        item_kind_icons=magic.item_kind_icons if magic is not None else {},
    )


def _creation_pending(state: Any, rule_set: Any) -> tuple[bool, bool]:
    """Whether the campaign options and the player character still need creating. Options can
    only be pending while there is no player character, so a campaign is set up in one pass."""
    options = rule_set.campaign if rule_set else NO_CAMPAIGN
    pc = state.pc()
    return bool(options.fields) and not state.options, pc is None or pc.out_of_play


def _encounter_rows(encounter: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """The encounter map as rows of cells. Each cell has the background or marked terrain and
    object under it, and the participant standing on it, if any."""
    occupants = {
        tuple(participant["cell"]): participant
        for participant in encounter["participants"]
        if participant["cell"] is not None
    }
    marks = {(mark["column"], mark["row"]): mark for mark in encounter["marked_cells"]}
    background = encounter["background"] or {}
    rows = []
    for row in range(encounter["rows"]):
        cells = []
        for column in range(encounter["columns"]):
            mark = marks.get((column, row), {})
            # A cell with no terrain of its own stands on the background.
            ground = mark if mark.get("terrain") else background
            occupant = occupants.get((column, row))
            terrain_name = ground.get("terrain", ground.get("name", ""))
            terrain_rule = ground.get("terrain_rule", ground.get("rule", ""))
            blocks = bool(mark.get("blocks", False))
            cells.append(
                {
                    "occupant": occupant,
                    "tip": _cell_tip(
                        occupant, terrain_name, terrain_rule, mark.get("object", ""), blocks
                    ),
                    "terrain_color": ground.get("terrain_color", ground.get("color", "")),
                    "terrain_name": terrain_name,
                    "difficult": bool(ground.get("difficult", False)),
                    "object_icon": mark.get("object_icon", ""),
                    "object_color": mark.get("object_color", ""),
                    "object_solid": bool(mark.get("object_solid", False)),
                    "object_name": mark.get("object", ""),
                    "blocks": blocks,
                }
            )
        rows.append(cells)
    return rows


def _cell_tip(
    occupant: dict[str, Any] | None,
    terrain_name: str,
    terrain_rule: str,
    object_name: str = "",
    blocks: bool = False,
) -> str:
    """The hover text for a cell: the token on it, the object, and the ground under it with its
    rule effect. A blocking object replaces that rule, since nothing can enter the cell."""
    where = " on ".join(part for part in (object_name, terrain_name) if part)
    if not where:
        return occupant["name"] if occupant else ""
    rule = "Blocks movement: no one can enter this cell." if blocks else terrain_rule
    ground = f"{where}. {rule}" if rule else f"{where}."
    return f"{occupant['name']} on {ground}" if occupant else ground


def _roll_key(campaign_id: int) -> str:
    """The session key for the stats a player is choosing between, until they create the
    character. The roll is kept on the server-signed session so the client cannot pick it."""
    return f"character-roll-{campaign_id}"


def _roll_stats(request: Request, user: User, campaign_id: int) -> dict[str, int]:
    rolled = roll_player_stats(
        request.app.state.engine, request.app.state.content, user.id, campaign_id
    )
    request.session[_roll_key(campaign_id)] = rolled
    return rolled
