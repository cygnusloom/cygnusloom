"""Campaign pages: list, create, and chat."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import (
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import Response

from cygnusloom import CAMPAIGN_MAJOR_VERSION
from cygnusloom.auth.deps import page_user, verify_csrf
from cygnusloom.content.character import NO_CAMPAIGN, NO_CHARACTER, character_schema
from cygnusloom.db.models import User
from cygnusloom.llm.base import LLMError
from cygnusloom.services import campaign_files
from cygnusloom.services import credits as credit_service
from cygnusloom.services import level_up as level_up_service
from cygnusloom.services.accounts import public_name, set_show_internal_thinking
from cygnusloom.services.campaigns import (
    Delta,
    Logged,
    NewCharacter,
    NextMessage,
    Prompt,
    Retract,
    Saved,
    Status,
    TurnEvent,
    append_turn_reply,
    begin_encounter_turn,
    campaign_spec,
    campaign_transcript,
    campaign_world,
    character_spec,
    clear_debug_injection,
    clear_turn_reply,
    context_usage,
    create_campaign,
    default_campaign_labels,
    end_encounter_turn,
    get_campaign,
    get_owned_campaign,
    get_turn_context,
    get_turn_transcript,
    is_playable,
    list_messages,
    pending_turn,
    promote_hireling_to_character,
    resume_turn,
    roll_player_stats,
    rollback_to,
    save_creation,
    set_campaign_inventory_space,
    set_campaign_label,
    set_campaign_model,
    set_debug_credits,
    set_debug_currency,
    set_debug_injection,
    set_debug_invincibility,
    set_debug_stat,
    set_turn_activity,
    start_encounter_turn,
    stream_message,
    turn_busy,
    turn_progress,
    update_campaign_settings,
    visible_campaigns,
)
from cygnusloom.services.campaigns import (
    delete_campaign as delete_campaign_service,
)
from cygnusloom.services.campaigns import (
    discard_pending as discard_pending_turn,
)
from cygnusloom.services.common import Conflict, InvalidInput, NotFound, session_scope
from cygnusloom.services.compendium import compendium_view
from cygnusloom.services.encounters import encounter_view, local_map_view
from cygnusloom.services.events import (
    PC_KIND,
    CampaignState,
    last_event_id,
    load_state,
    name_key,
    speaker_name,
)
from cygnusloom.services.factions import faction_views
from cygnusloom.services.membership import (
    add_member,
    create_invite,
    deactivate_invite,
    list_invites,
    list_members,
    list_removed_members,
    merge_debug_member,
    remove_member,
)
from cygnusloom.services.music import music_view
from cygnusloom.services.tools import check_arguments
from cygnusloom.services.views import map_view, scene_view, story_view, time_view
from cygnusloom.web.character_form import (
    campaign_submission_from_form,
    campaign_view,
    creation_view,
    pronouns_from_form,
    submission_from_form,
)
from cygnusloom.web.desktop import optional_server_store
from cygnusloom.web.forms import form_text
from cygnusloom.web.rendering import render, render_fragment
from cygnusloom.web.turn_jobs import TurnJob, TurnJobRegistry
from cygnusloom.web.typing import Typer, TypingRegistry

router = APIRouter(prefix="/campaigns")


def _setup_redirect(request: Request) -> RedirectResponse | None:
    """Single-player starts in the wizard instead of a dead campaign form."""
    if request.app.state.settings.single_player and not request.app.state.catalog.model_ids():
        return RedirectResponse("/setup", status_code=303)
    return None


def _campaign_context_tokens(request: Request, user_id: int, campaign_id: int) -> int:
    """The prompt budget of the model this campaign is using, read at turn start.

    Campaign playability is left to the turn service, which reports a blocked campaign as the
    same conflict the page already understands.
    """
    campaign = get_campaign(request.app.state.engine, user_id, campaign_id, playable=False)
    return request.app.state.catalog.context_tokens(campaign.model)


@router.get("")
def campaigns_page(request: Request) -> Response:
    user = page_user(request)
    setup = _setup_redirect(request)
    if setup is not None:
        return setup
    campaigns = visible_campaigns(request.app.state.engine, user.id)
    store = optional_server_store(request.app.state.settings)
    servers = store.list() if store is not None else []
    # The desktop app chooses a new local port each launch, so this choice lives in a
    # cookie: cookies ignore the port and survive restarts of the app.
    play_tab = request.cookies.get("cygnus_play_tab", "device")
    if play_tab not in ("device", "server"):
        play_tab = "device"
    return render(
        request,
        "campaigns.html",
        campaigns=campaigns,
        servers=servers,
        play_tab=play_tab,
    )


@router.get("/new")
def new_campaign_page(request: Request) -> Response:
    user = page_user(request)
    setup = _setup_redirect(request)
    if setup is not None:
        return setup
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
    setup = _setup_redirect(request)
    if setup is not None:
        return setup
    form = await request.form()
    label = form_text(form, "label")
    world_template = form_text(form, "world_template")
    rule_set = form_text(form, "rule_set")
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
        )
    return RedirectResponse(f"/campaigns/{campaign.id}", status_code=303)


@router.post("/import")
async def import_campaign_file(request: Request) -> Response:
    """Rebuild a campaign from a JSON file exported by this or another server."""
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    upload = form.get("file")
    if not hasattr(upload, "read"):
        _flash(request, "Choose a Cygnus Loom campaign .json file.", "error")
        return RedirectResponse("/campaigns", status_code=303)
    data = await upload.read(campaign_files.MAX_IMPORT_BYTES + 1)
    if len(data) > campaign_files.MAX_IMPORT_BYTES:
        limit = campaign_files.MAX_IMPORT_BYTES // (1024 * 1024)
        _flash(request, f"That file is larger than {limit} MB.", "error")
        return RedirectResponse("/campaigns", status_code=303)
    try:
        result = campaign_files.import_campaign(
            request.app.state.engine,
            request.app.state.content,
            request.app.state.catalog.model_ids(),
            request.app.state.catalog.default_model_id(),
            user.id,
            data,
        )
    except (Conflict, InvalidInput) as exc:
        _flash(request, f"Could not import the campaign: {exc}", "error")
        return RedirectResponse("/campaigns", status_code=303)
    message = f"Imported {result.campaign.name}."
    if result.model_replaced:
        message += (
            f" The model {result.model_replaced} is not on this server; using {result.model_used}."
        )
    for warning in result.warnings:
        message += f" {warning.capitalize()}."
    _flash(request, message)
    return RedirectResponse(f"/campaigns/{result.campaign.id}", status_code=303)


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


@router.post("/{campaign_id}/share")
async def share_campaign(request: Request, campaign_id: int) -> Response:
    """Make a join code the owner can send to other players."""
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    try:
        get_owned_campaign(request.app.state.engine, user.id, campaign_id)
        create_invite(
            request.app.state.engine,
            user.id,
            campaign_id,
            form_text(form, "label"),
        )
    except InvalidInput as exc:
        _flash(request, str(exc), "error")
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.post("/{campaign_id}/share/{invite_id}/deactivate")
async def deactivate_campaign_invite(
    request: Request, campaign_id: int, invite_id: int
) -> Response:
    """Stop a join code from letting anyone else in. Existing members stay."""
    user = page_user(request)
    await verify_csrf(request)
    deactivate_invite(request.app.state.engine, user.id, campaign_id, invite_id)
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.post("/{campaign_id}/members/{member_id}/readd")
async def readd_campaign_member(request: Request, campaign_id: int, member_id: int) -> Response:
    """Put a removed player back at the table without a new join code."""
    user = page_user(request)
    await verify_csrf(request)
    get_owned_campaign(request.app.state.engine, user.id, campaign_id)
    add_member(request.app.state.engine, campaign_id, member_id)
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.post("/{campaign_id}/members/{member_id}/remove")
async def remove_campaign_member(request: Request, campaign_id: int, member_id: int) -> Response:
    """Take a player off the campaign, choosing whether their character leaves the story."""
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    retire = form_text(form, "character") == "retire"
    try:
        remove_member(
            request.app.state.engine,
            user.id,
            campaign_id,
            member_id,
            retire_character=retire,
        )
    except InvalidInput as exc:
        _flash(request, str(exc), "error")
        return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.post("/{campaign_id}/debug/member")
async def debug_add_campaign_member(request: Request, campaign_id: int) -> Response:
    """Debug helper: put another account at the table without a join code. The owner can then
    sign in as that account in another browser to test a second player."""
    settings = request.app.state.settings
    if not settings.debug_panel:
        raise NotFound("not found")
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    try:
        merge_debug_member(
            request.app.state.engine,
            user.id,
            campaign_id,
            form_text(form, "email"),
        )
    except InvalidInput as exc:
        _flash(request, str(exc), "error")
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.post("/{campaign_id}/delete")
async def delete_campaign(request: Request, campaign_id: int) -> Response:
    """Delete one of the player's own campaigns after they type its label to confirm."""
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    campaign = get_owned_campaign(request.app.state.engine, user.id, campaign_id)
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
    setting = ""
    table_genres: list[str] | None = None
    try:
        campaign = get_campaign(engine, user.id, campaign_id)
        state = campaign_world(engine, user.id, campaign_id).state
        options_pending, character_pending = _creation_pending(
            state, library.rule_sets.get(campaign.rule_set), user.id
        )
        if not options_pending and not character_pending:
            raise InvalidInput("this campaign's options and player character are already set")
        initial_setup = not state.pcs()
        # Read the brief even for a replacement character so an error can show it again, but only
        # apply it before the campaign's first player character exists. A replacement character
        # or a player joining later must not rewrite the campaign's setting and story.
        setting = form_text(form, "setting")
        if initial_setup:
            table_genres = [
                value for value in form.getlist("table_genres") if isinstance(value, str)
            ]
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
                    setting=setting,
                    table_genres=table_genres,
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
        save_creation(
            engine,
            library,
            user.id,
            campaign_id,
            options=options,
            character=character,
            setting=setting if initial_setup else None,
            table_genres=table_genres,
        )
    except InvalidInput as exc:
        return _campaign_page(
            request,
            user,
            campaign_id,
            400,
            str(exc),
            setting=setting,
            table_genres=table_genres,
        )
    except Conflict as exc:
        return _campaign_page(
            request,
            user,
            campaign_id,
            409,
            str(exc),
            setting=setting,
            table_genres=table_genres,
        )
    if character is not None:
        request.session.pop(key, None)
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.get("/{campaign_id}/advancement")
def advancement_page(request: Request, campaign_id: int) -> Response:
    """The player's level-up or Advancement page, for their character or a hireling."""
    user = page_user(request)
    return _advancement_page(
        request, user, campaign_id, character=request.query_params.get("character", "")
    )


@router.post("/{campaign_id}/advancement")
async def submit_advancement(request: Request, campaign_id: int) -> Response:
    """Apply a level-up, a pool of advancement picks, or an Advancement option."""
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    engine = request.app.state.engine
    library = request.app.state.content
    campaign = get_campaign(engine, user.id, campaign_id)
    rule_set = library.rule_sets.get(campaign.rule_set)
    if rule_set is None:
        return _campaign_page(
            request, user, campaign_id, 400, "that rule set is no longer available"
        )
    world = library.world_content(campaign.world_template, campaign.rule_set)
    state = campaign_world(engine, user.id, campaign.id).state
    viewer_pc = state.player_character(user.id)
    requested = str(form.get("character", "")).strip()
    target = None
    if requested:
        target = next(
            (
                character
                for character in level_up_service.controlled_characters(state, user.id)
                if name_key(character.name) == name_key(requested)
            ),
            None,
        )
        if target is None:
            return _campaign_page(
                request, user, campaign_id, 400, "that character is not yours to advance"
            )
    character_name = (
        target.name if target is not None else (viewer_pc.name if viewer_pc is not None else None)
    )
    try:
        level_up_service.apply_page(
            engine, campaign.id, rule_set, world, dict(form), character_name
        )
    except InvalidInput as exc:
        return _advancement_page(
            request,
            user,
            campaign_id,
            400,
            str(exc),
            character=target.name if target is not None else "",
        )
    try:
        remaining = level_up_service.pages_for_player(engine, campaign.id, rule_set, world, user.id)
    except InvalidInput:
        remaining = []
    if remaining:
        return RedirectResponse(f"/campaigns/{campaign_id}/advancement", status_code=303)
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.get("/{campaign_id}/export")
def export_campaign_file(request: Request, campaign_id: int) -> Response:
    """Download the campaign row and its complete event log as one JSON file."""
    user = page_user(request)
    try:
        filename, payload = campaign_files.export_campaign(
            request.app.state.engine, user.id, campaign_id
        )
    except Conflict as exc:
        return PlainTextResponse(str(exc), status_code=409)
    except InvalidInput as exc:
        return PlainTextResponse(str(exc), status_code=400)
    return Response(
        content=payload,
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/{campaign_id}/convert")
async def convert_campaign_page(request: Request, campaign_id: int) -> Response:
    """Upgrade an older-major campaign in place after replaying its rewritten log."""
    user = page_user(request)
    await verify_csrf(request)
    campaign = get_owned_campaign(request.app.state.engine, user.id, campaign_id)
    if campaign.major_version == CAMPAIGN_MAJOR_VERSION:
        _flash(
            request,
            f"This campaign already plays on Cygnus Loom {CAMPAIGN_MAJOR_VERSION}.",
        )
        return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)
    adapter = _upgrade_adapter(campaign.major_version)
    if adapter is None:
        _flash(
            request,
            f"There is no upgrade path from Cygnus Loom {campaign.major_version} to "
            f"Cygnus Loom {CAMPAIGN_MAJOR_VERSION}.",
            "error",
        )
        return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)
    try:
        adapter.convert_campaign(
            request.app.state.engine,
            user.id,
            campaign_id,
            request.app.state.content,
        )
    except (Conflict, InvalidInput) as exc:
        _flash(request, f"Could not upgrade the campaign: {exc}", "error")
        return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.get("/{campaign_id}")
def campaign_page(request: Request, campaign_id: int) -> Response:
    user = page_user(request)
    return _campaign_page(request, user, campaign_id)


@router.get("/{campaign_id}/live")
def campaign_live(request: Request, campaign_id: int) -> Response:
    """The campaign's views on their own, so a finished turn can replace them in place.

    The turn stream used to reload the whole page when the GM finished or asked the player
    something. On a slow connection that reload is a white screen and a fresh round trip. This
    fragment carries the same state without the shell, so the player keeps their place."""
    user = page_user(request)
    return _campaign_page(request, user, campaign_id, fragment=True)


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
    return JSONResponse(_turn_snapshot(engine, campaign_id, request.app.state.typing))


@router.post("/{campaign_id}/typing")
async def typing_heartbeat(request: Request, campaign_id: int) -> Response:
    """Record that the player is composing a message, or that they stopped.

    Typing is a hint, not campaign state: the composer sends it while the box holds text, and it
    expires on its own. Every page receives it in the live snapshot it already watches."""
    user = page_user(request)
    engine = request.app.state.engine
    campaign = get_campaign(engine, user.id, campaign_id)
    await verify_csrf(request)
    form = await request.form()
    if form_text(form, "active") == "0":
        request.app.state.typing.stop(campaign.id, user.id)
    else:
        # The composer renders the name server-side, so the hint names the character the player
        # speaks for without folding the campaign log on every keystroke.
        name = form_text(form, "name").strip() or public_name(user)
        request.app.state.typing.note(campaign.id, user.id, name)
    return Response(status_code=204)


def _newest_event(engine: Any, campaign_id: int) -> int:
    with session_scope(engine) as db:
        return last_event_id(db, campaign_id)


def _turn_snapshot(
    engine: Any,
    campaign_id: int,
    typing: TypingRegistry,
    *,
    newest_event: int | None = None,
    busy: bool | None = None,
    typers: tuple[Typer, ...] | None = None,
    activity: str | None = None,
    reply: str | None = None,
) -> dict[str, Any]:
    """What every page needs to know about the table: the newest event, whose turn it is, who
    is typing, and what the GM is doing. Both the status endpoint and the live stream send this
    shape. `newest_event`, `busy`, `typers`, `activity`, and `reply` may be passed when the
    caller just read them, which spares a second query."""
    if newest_event is None:
        newest_event = _newest_event(engine, campaign_id)
    if busy is None:
        busy = turn_busy(campaign_id)
    if typers is None:
        typers = typing.active(campaign_id)
    if activity is None or reply is None:
        live_activity, live_reply = turn_progress(campaign_id)
        activity = live_activity if activity is None else activity
        reply = live_reply if reply is None else reply
    with session_scope(engine) as db:
        state = load_state(db, campaign_id)
    newest = newest_event
    turn = state.turn
    if turn is None:
        status = "idle"
    elif busy:
        status = "busy"
    elif turn.failed is not None:
        status = "failed"
    elif turn.prompt is not None:
        status = "prompt"
    else:
        status = "busy"
    running = busy and turn is not None
    actor_id = turn.player_id if turn is not None else None
    actor = turn.player_name if turn is not None else ""
    if state.encounter is not None and state.encounter.acting:
        acting = state.characters.get(name_key(state.encounter.acting))
        if acting is not None:
            if acting.kind == PC_KIND:
                actor_id = state.owner_of(acting)
                actor = acting.player_name or acting.name
            else:
                # The GM plays an NPC's turn; no player owns it.
                actor_id = None
                actor = acting.name
    return {
        "status": status,
        "running": running,
        "event": newest,
        "turn": turn.opener if turn is not None else 0,
        "actor_id": actor_id,
        "actor": actor,
        "pending_for": turn.player_id if turn is not None and turn.prompt else None,
        "typing": [{"id": typer.user_id, "name": typer.name} for typer in typers],
        "activity": activity,
        "reply": reply,
    }


@router.get("/{campaign_id}/events")
def campaign_events(request: Request, campaign_id: int) -> Response:
    """A live stream of the campaign's newest event and whose turn it is.

    Every player's page watches this, so a turn one player starts updates the whole table
    without a reload. The stream sends a snapshot whenever it changes, checked against the
    database so a turn written by another server process is seen too."""
    user = page_user(request)
    engine = request.app.state.engine
    get_campaign(engine, user.id, campaign_id)
    return StreamingResponse(
        _event_lines(engine, campaign_id, request.app.state.typing),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _event_lines(engine: Any, campaign_id: int, typing: TypingRegistry) -> AsyncIterator[str]:
    last_payload = ""
    last_key: tuple[int, bool, tuple[Typer, ...], str, str] | None = None
    heartbeat = 0
    while True:
        try:
            newest = _newest_event(engine, campaign_id)
            busy = turn_busy(campaign_id)
            typers = typing.active(campaign_id)
            activity, reply = turn_progress(campaign_id) if busy else ("", "")
            key = (newest, busy, typers, activity, reply)
            if key != last_key:
                last_key = key
                # Only replay the log when something has actually changed. An idle stream
                # otherwise costs one small query a second, however long the campaign is.
                snapshot = _turn_snapshot(
                    engine,
                    campaign_id,
                    typing,
                    newest_event=newest,
                    busy=busy,
                    typers=typers,
                    activity=activity,
                    reply=reply,
                )
                payload = json.dumps(snapshot)
                if payload != last_payload:
                    last_payload = payload
                    heartbeat = 0
                    yield f"data: {payload}\n\n"
                    await asyncio.sleep(1.0)
                    continue
        except SQLAlchemyError:
            # The database may be gone. End the stream; the page's next action tells the player.
            return
        heartbeat += 1
        if heartbeat >= 15:
            heartbeat = 0
            yield ": keep-alive\n\n"
        await asyncio.sleep(1.0)


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
    viewer_pc = world.state.player_character(user.id)
    encounter = encounter_view(
        world.state,
        rule_set.grid if rule_set else None,
        rule_set.combat if rule_set else None,
        rule_set.terrain if rule_set else None,
        rule_set.encounter_objects if rule_set else None,
    )
    local_map = _local_map(world.state, rule_set, viewer_pc)
    return render(
        request,
        "campaign_map.html",
        world_map=map_view(world.state, rule_set.world_terrain if rule_set else None, pc=viewer_pc),
        time=time_view(world.state, rule_set.clock if rule_set else None),
        encounter=encounter,
        encounter_rows=_encounter_rows(encounter) if encounter is not None else [],
        local_map=local_map,
        local_map_rows=_encounter_rows(local_map) if local_map is not None else [],
        map_fragment=True,
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


@router.post("/{campaign_id}/inventory-space")
async def change_inventory_space(request: Request, campaign_id: int) -> Response:
    """Turn the rule set's inventory space on or off for the owner's campaign."""
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    try:
        set_campaign_inventory_space(
            request.app.state.engine,
            user.id,
            campaign_id,
            form_text(form, "use_inventory_space") == "1",
        )
    except InvalidInput as exc:
        return _campaign_page(request, user, campaign_id, 400, str(exc))
    except Conflict as exc:
        return _campaign_page(request, user, campaign_id, 409, str(exc))
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.post("/{campaign_id}/settings")
async def change_campaign_settings(request: Request, campaign_id: int) -> Response:
    """Save the owner's model, campaign name, and inventory choice from the Settings page."""
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    try:
        update_campaign_settings(
            request.app.state.engine,
            request.app.state.catalog.model_ids(),
            user.id,
            campaign_id,
            model=form_text(form, "model"),
            inventory_space=form_text(form, "use_inventory_space") == "1",
            label=form_text(form, "label"),
        )
    except InvalidInput as exc:
        return _campaign_page(request, user, campaign_id, 400, str(exc))
    except Conflict as exc:
        return _campaign_page(request, user, campaign_id, 409, str(exc))
    _flash(request, "Settings saved.")
    return RedirectResponse(f"/campaigns/{campaign_id}#view-settings", status_code=303)


@router.post("/{campaign_id}/thinking")
async def change_internal_thinking(request: Request, campaign_id: int) -> Response:
    """Save this viewer's choice of whether to see the GM's internal thinking."""
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    set_show_internal_thinking(
        request.app.state.engine,
        user.id,
        form_text(form, "show_internal_thinking") == "1",
    )
    _flash(request, "Your view is saved.")
    return RedirectResponse(f"/campaigns/{campaign_id}#view-settings", status_code=303)


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


@router.post("/{campaign_id}/debug/grant")
async def grant_debug_advancement(request: Request, campaign_id: int) -> Response:
    settings = request.app.state.settings
    if not settings.debug_panel:
        raise NotFound("not found")
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    engine = request.app.state.engine
    library = request.app.state.content
    campaign = get_campaign(engine, user.id, campaign_id)
    rule_set = library.rule_sets.get(campaign.rule_set)
    if rule_set is None:
        return _campaign_page(
            request, user, campaign_id, 400, "that rule set is no longer available"
        )
    raw = form_text(form, "amount").strip()
    try:
        amount = int(raw) if raw else 0
        level_up_service.debug_grant(
            engine,
            campaign.id,
            rule_set,
            library.world_content(campaign.world_template, campaign.rule_set),
            form_text(form, "kind"),
            form_text(form, "name"),
            amount,
        )
    except ValueError:
        return _campaign_page(request, user, campaign_id, 400, "experience must be a number")
    except InvalidInput as exc:
        return _campaign_page(request, user, campaign_id, 400, str(exc))
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.post("/{campaign_id}/debug/credits")
async def change_debug_credits(request: Request, campaign_id: int) -> Response:
    settings = request.app.state.settings
    if not settings.debug_panel:
        raise NotFound("not found")
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    try:
        balance = int(form_text(form, "balance"))
        set_debug_credits(
            request.app.state.engine,
            user.id,
            campaign_id,
            balance,
            force=form_text(form, "force") == "1",
        )
    except ValueError:
        return _campaign_page(request, user, campaign_id, 400, "balance must be a whole number")
    except (InvalidInput, NotFound) as exc:
        return _campaign_page(request, user, campaign_id, 400, str(exc))
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


@router.post("/{campaign_id}/debug/injection")
async def change_debug_injection(request: Request, campaign_id: int) -> Response:
    settings = request.app.state.settings
    if not settings.debug_panel:
        raise NotFound("not found")
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    mode = form_text(form, "mode")
    try:
        if mode == "tool":
            campaign = get_campaign(request.app.state.engine, user.id, campaign_id)
            rule_set = request.app.state.content.rule_sets.get(campaign.rule_set)
            if rule_set is None:
                raise InvalidInput("that campaign's rule set is no longer available")
            name = form_text(form, "tool")
            tool = next(
                (candidate for candidate in rule_set.tool_calls.values() if candidate.name == name),
                None,
            )
            if tool is None:
                raise InvalidInput(f"there is no tool named {name!r}")
            if tool.player_input is not None:
                raise InvalidInput("choose a tool the GM can use without the player's answer")
            if (
                tool.lookup
                or tool.journal_step
                or tool.review_step
                or tool.name in ("ooc", "speak")
            ):
                raise InvalidInput("choose a state-changing tool")
            try:
                arguments = json.loads(form_text(form, "arguments") or "{}")
            except ValueError:
                raise InvalidInput("arguments must be valid JSON")
            problem = check_arguments(tool.parameters, arguments)
            if problem:
                raise InvalidInput(problem)
            set_debug_injection(
                request.app.state.engine,
                user.id,
                campaign_id,
                "tool",
                tool=tool.name,
                arguments=json.dumps(arguments, sort_keys=True, ensure_ascii=False),
            )
        elif mode == "narration":
            set_debug_injection(
                request.app.state.engine,
                user.id,
                campaign_id,
                "narration",
                text=form_text(form, "text"),
            )
        else:
            raise InvalidInput("mode must be tool or narration")
    except InvalidInput as exc:
        return _campaign_page(request, user, campaign_id, 400, str(exc))
    except Conflict as exc:
        return _campaign_page(request, user, campaign_id, 409, str(exc))
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.post("/{campaign_id}/debug/injection/clear")
async def remove_debug_injection(request: Request, campaign_id: int) -> Response:
    settings = request.app.state.settings
    if not settings.debug_panel:
        raise NotFound("not found")
    user = page_user(request)
    await verify_csrf(request)
    try:
        clear_debug_injection(request.app.state.engine, user.id, campaign_id)
    except Conflict as exc:
        return _campaign_page(request, user, campaign_id, 409, str(exc))
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.get("/{campaign_id}/messages/{event_id}/context")
def turn_context_fragment(request: Request, campaign_id: int, event_id: int) -> Response:
    user = page_user(request)
    if not user.show_internal_thinking:
        raise NotFound("message not found")
    parts = get_turn_context(request.app.state.engine, user.id, campaign_id, event_id)
    return render(request, "turn_context.html", parts=parts)


@router.get("/{campaign_id}/messages/{event_id}/transcript")
def turn_transcript_fragment(request: Request, campaign_id: int, event_id: int) -> Response:
    user = page_user(request)
    if not user.show_internal_thinking:
        raise NotFound("message not found")
    entries = get_turn_transcript(request.app.state.engine, user.id, campaign_id, event_id)
    return render(request, "turn_transcript.html", entries=entries)


@router.post("/{campaign_id}/messages/{event_id}/rollback")
async def rollback(request: Request, campaign_id: int, event_id: int) -> Response:
    user = page_user(request)
    await verify_csrf(request)
    try:
        rollback_to(request.app.state.engine, user.id, campaign_id, event_id)
    except InvalidInput as exc:
        return _campaign_page(request, user, campaign_id, 400, str(exc))
    except Conflict as exc:
        return _campaign_page(request, user, campaign_id, 409, str(exc))
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.post("/{campaign_id}/messages")
async def post_message(request: Request, campaign_id: int) -> Response:
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    text = form_text(form, "content")
    events = stream_message(
        request.app.state.engine,
        request.app.state.providers,
        request.app.state.catalog.model_ids(),
        request.app.state.content,
        _campaign_context_tokens(request, user.id, campaign_id),
        user.id,
        campaign_id,
        text,
    )
    try:
        job = _start_turn_job(request, campaign_id, events)
    except Conflict as exc:
        return _campaign_page(request, user, campaign_id, 409, str(exc), text)
    # The turn owns itself from here. Waiting on it is shielded, so a page that goes away
    # leaves the reply running and the next load reads it from the log.
    await job.wait()
    error = job.error
    if isinstance(error, InvalidInput):
        return _campaign_page(request, user, campaign_id, 400, str(error), text)
    if isinstance(error, Conflict):
        return _campaign_page(request, user, campaign_id, 409, str(error), text)
    if isinstance(error, LLMError):
        return _campaign_page(request, user, campaign_id, 502, str(error), text)
    if isinstance(error, NotFound):
        raise error
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


@router.post("/{campaign_id}/messages/stream")
async def post_message_stream(request: Request, campaign_id: int) -> Response:
    """Newline-delimited JSON events for one turn. Errors are reported as an event, because the
    response has already started by the time they happen."""
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    text = form_text(form, "content")
    events = stream_message(
        request.app.state.engine,
        request.app.state.providers,
        request.app.state.catalog.model_ids(),
        request.app.state.content,
        _campaign_context_tokens(request, user.id, campaign_id),
        user.id,
        campaign_id,
        text,
    )
    try:
        job = _start_turn_job(request, campaign_id, events)
    except Conflict as exc:
        return _refused_stream(str(exc))
    return StreamingResponse(
        job.follow(),
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
    events = resume_turn(
        request.app.state.engine,
        request.app.state.providers,
        request.app.state.catalog.model_ids(),
        request.app.state.content,
        _campaign_context_tokens(request, user.id, campaign_id),
        user.id,
        campaign_id,
        action,
        text,
    )
    try:
        job = _start_turn_job(request, campaign_id, events)
    except Conflict as exc:
        return _refused_stream(str(exc))
    return StreamingResponse(
        job.follow(),
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
    events = start_encounter_turn(
        request.app.state.engine,
        request.app.state.providers,
        request.app.state.catalog.model_ids(),
        request.app.state.content,
        _campaign_context_tokens(request, user.id, campaign_id),
        user.id,
        campaign_id,
        name,
    )
    try:
        job = _start_turn_job(request, campaign_id, events)
    except Conflict as exc:
        return _refused_stream(str(exc))
    return StreamingResponse(
        job.follow(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache"},
    )


# BACKWARDS-COMPATIBILITY: 3 - old pages may still post to the explicit begin route.
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


def _start_turn_job(
    request: Request, campaign_id: int, events: AsyncIterator[TurnEvent]
) -> TurnJob:
    """Hand the turn to a task of its own, so closing the page no longer cancels it."""
    registry: TurnJobRegistry = request.app.state.turn_jobs
    return registry.start(campaign_id, events, _turn_lines)


async def _one_line(event: TurnError) -> AsyncIterator[str]:
    yield _line(event) + "\n"


def _refused_stream(message: str) -> StreamingResponse:
    """A 409 the page reads like any other turn stream: one NDJSON error line, HTTP 200."""
    return StreamingResponse(
        _one_line(TurnError(409, message)),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache"},
    )


async def _turn_lines(
    events: AsyncIterator[TurnEvent],
    campaign_id: int,
    errors: list[Exception] | None = None,
) -> AsyncIterator[str]:
    try:
        async for event in events:
            match event:
                case Status(text):
                    set_turn_activity(campaign_id, text)
                case Delta(text):
                    append_turn_reply(campaign_id, text)
                case Retract() | NextMessage() | Saved():
                    clear_turn_reply(campaign_id)
            yield _line(event) + "\n"
    except InvalidInput as exc:
        if errors is not None:
            errors.append(exc)
        yield _line(TurnError(400, str(exc))) + "\n"
    except NotFound as exc:
        if errors is not None:
            errors.append(exc)
        yield _line(TurnError(404, str(exc))) + "\n"
    except Conflict as exc:
        if errors is not None:
            errors.append(exc)
        yield _line(TurnError(409, str(exc))) + "\n"
    except LLMError as exc:
        if errors is not None:
            errors.append(exc)
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


def _upgrade_adapter(major: int) -> ModuleType | None:
    """The adapter that upgrades a campaign of `major`, imported only when one is offered."""
    from cygnusloom.services import compat_v3

    return compat_v3 if compat_v3.supports(major) else None


def _new_form(
    request: Request,
    user: User,
    status_code: int = 200,
    error: str = "",
    label: str = "",
    world_template: str = "",
    rule_set: str = "",
) -> Response:
    library = request.app.state.content
    # The rule set comes first and decides which worlds are offered, so start from a rule set
    # and a world that fits it. A world with no rule set fits every rule set.
    configured = library.core.default_campaign
    if rule_set not in library.rule_sets:
        rule_set = (
            configured.rule_set
            if configured is not None and configured.rule_set in library.rule_sets
            else next(iter(library.rule_sets), "")
        )
    worlds = [
        world
        for world in library.world_templates.values()
        if world.rule_set is None or world.rule_set == rule_set
    ]
    if world_template not in {world.id for world in worlds}:
        # Prefer the configured world, then a world built for this rule set over a generic one
        # when the form first opens.
        preferred = []
        if configured is not None and configured.rule_set == rule_set:
            preferred = [world for world in worlds if world.id == configured.world_template]
        if not preferred:
            preferred = [world for world in worlds if world.rule_set == rule_set]
        world_template = (preferred or worlds)[0].id if worlds else ""
    chosen_rule_set = library.rule_sets.get(rule_set)
    chosen_world = library.world_templates.get(world_template)
    default_labels = default_campaign_labels(request.app.state.engine, library, user.id)
    return render(
        request,
        "new_campaign.html",
        status_code=status_code,
        error=error,
        campaign_label=label or default_labels.get(world_template, ""),
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
        selected_rule_set_third_party=bool(chosen_rule_set and chosen_rule_set.third_party),
        selected_world_third_party=bool(chosen_world and chosen_world.third_party),
    )


def _advancement_page(
    request: Request,
    user: User,
    campaign_id: int,
    status_code: int = 200,
    error: str = "",
    character: str = "",
) -> Response:
    engine = request.app.state.engine
    library = request.app.state.content
    campaign = get_campaign(engine, user.id, campaign_id)
    rule_set = library.rule_sets.get(campaign.rule_set)
    if rule_set is None:
        return _campaign_page(
            request, user, campaign_id, 400, "that rule set is no longer available"
        )
    world = library.world_content(campaign.world_template, campaign.rule_set)
    try:
        pages = level_up_service.pages_for_player(engine, campaign.id, rule_set, world, user.id)
    except InvalidInput as exc:
        return _campaign_page(request, user, campaign_id, 400, str(exc))
    if not pages:
        return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)
    view = pages[0]
    if character:
        chosen = next(
            (page for page in pages if name_key(page.character) == name_key(character)),
            None,
        )
        if chosen is None:
            return _campaign_page(
                request, user, campaign_id, 400, "that character has no advancement waiting"
            )
        view = chosen
    return render(
        request,
        "advancement.html",
        status_code=status_code,
        error=error,
        campaign=campaign,
        rule_set=rule_set,
        world=library.world_templates.get(campaign.world_template),
        view=view,
        pending=tuple(page.character for page in pages),
    )


def _campaign_page(
    request: Request,
    user: User,
    campaign_id: int,
    status_code: int = 200,
    error: str = "",
    draft: str = "",
    setting: str = "",
    table_genres: Sequence[str] | None = None,
    fragment: bool = False,
) -> Response:
    engine = request.app.state.engine
    settings = request.app.state.settings
    library = request.app.state.content
    campaign = get_campaign(engine, user.id, campaign_id, playable=False)
    if not is_playable(campaign):
        return render(
            request,
            "campaign_blocked.html",
            status_code=status_code,
            campaign=campaign,
            is_owner=campaign.owner_id == user.id,
            current_major=CAMPAIGN_MAJOR_VERSION,
            can_upgrade=_upgrade_adapter(campaign.major_version) is not None,
        )
    rule_set = library.rule_sets.get(campaign.rule_set)
    debug_tools = (
        sorted(
            tool.name
            for tool in rule_set.tool_calls.values()
            if not tool.lookup
            and not tool.journal_step
            and not tool.review_step
            and tool.player_input is None
            and tool.name not in ("ooc", "speak")
        )
        if rule_set is not None
        else []
    )
    inventory_rules = rule_set.inventory if rule_set is not None else None
    magic = rule_set.magic if rule_set is not None else None
    spec = rule_set.character if rule_set else NO_CHARACTER
    options = rule_set.campaign if rule_set else NO_CAMPAIGN
    # Read the newest event before folding the state. If another player writes in between,
    # the state includes that event and the live stream sees a newer one, so the page catches
    # up instead of believing it is current.
    with session_scope(engine) as db:
        newest = last_event_id(db, campaign.id)
    world = campaign_world(engine, user.id, campaign.id)
    compendium = compendium_view(library, campaign.rule_set, campaign.world_template, world.state)
    viewer_pc = world.state.player_character(user.id)
    world_map = map_view(world.state, rule_set.world_terrain if rule_set else None, pc=viewer_pc)
    options_pending, character_pending = _creation_pending(world.state, rule_set, user.id)
    is_owner = campaign.owner_id == user.id
    members = list_members(engine, campaign.id, world.state)
    invites = list_invites(engine, campaign.id) if is_owner else []
    removed_members = list_removed_members(engine, campaign.id, world.state) if is_owner else []
    if options_pending or character_pending:
        if fragment:
            # The creation page has no live refresh; a reload shows the form again.
            return PlainTextResponse("This campaign is still being set up.", status_code=409)
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
                conditions=rule_set.conditions if rule_set is not None else None,
                pc=viewer_pc,
                item_links=compendium.item_links,
                ability_links=compendium.ability_links,
                creature_links=compendium.creature_links,
                place_links=compendium.place_links,
                sheet_links=compendium.sheet_links,
                track_inventory=campaign.use_inventory_space,
            ),
            world_map=world_map,
            rule_set=rule_set,
            compendium=compendium,
            story=story_view(world.state),
            show_options=options_pending,
            show_setting=not world.state.pcs(),
            campaign_setting=setting or campaign.setting,
            table_genres=library.available_table_genres,
            selected_table_genres=(
                tuple(table_genres)
                if table_genres
                else (rule_set.table_genres if rule_set is not None else ())
            ),
            options=campaign_view(options) if options_pending else None,
            show_character=character_pending,
            character=character,
            fallen=viewer_pc,
            promotable=promotable,
            is_owner=is_owner,
            viewer_id=user.id,
            members=members,
            invites=invites,
            removed_members=removed_members,
            unclaimed=[c.name for c in world.state.pcs() if c.unclaimed],
            error=error,
        )
    messages = list_messages(engine, campaign.id)
    transcript = campaign_transcript(engine, user.id, campaign.id)
    world_content = library.world_content(campaign.world_template, campaign.rule_set)
    pending = pending_turn(engine, user.id, campaign.id)
    advancement_due = (
        level_up_service.advancement_due_for_player(world.state, rule_set, user.id)
        if rule_set is not None
        else False
    )
    advancement_available = False
    if rule_set is not None:
        try:
            advancement_available = bool(
                level_up_service.pages_for_player(
                    engine, campaign.id, rule_set, world_content, user.id
                )
            )
        except InvalidInput:
            advancement_available = False
    context = context_usage(
        campaign,
        world.state,
        messages,
        rule_set,
        options,
        request.app.state.catalog.context_tokens(campaign.model),
        world_content.rules,
        unattached_world_rules=world_content.unattached_rules,
        world=library.world_templates.get(campaign.world_template),
        tool_schema_mode=settings.tool_schema_mode,
        rule_info_mode=settings.rule_info_mode,
    )
    credit_price = credit_service.model_price(engine, campaign.model)
    credit_balance = credit_service.balance(engine, campaign.owner_id)
    credits_exempt = credit_price <= 0 or credit_service.is_exempt(engine, settings, campaign)
    credits_ok = credits_exempt or credit_balance >= credit_price
    credit_message = (
        "" if credits_ok else credit_service.shortfall_message(engine, campaign, credit_price)
    )
    encounter = encounter_view(
        world.state,
        rule_set.grid if rule_set else None,
        rule_set.combat if rule_set else None,
        rule_set.terrain if rule_set else None,
        rule_set.encounter_objects if rule_set else None,
    )
    local_map = _local_map(world.state, rule_set, viewer_pc)
    debug_xp, debug_advancement = (
        level_up_service.debug_grant_options(world.state, rule_set, viewer_pc)
        if settings.debug_panel and rule_set is not None
        else (None, None)
    )
    player_name = viewer_pc.name if viewer_pc is not None else ""
    acting_name = speaker_name(world.state, user.id) or player_name
    # A running turn is visible to every page, including a second tab of the account that
    # started it. The busy flag is in-process, like the typing hints; a page that misses it
    # still catches the saved reply through the live stream's newest event.
    gm_busy = turn_busy(campaign.id) and world.state.turn is not None
    gm_activity, gm_reply = turn_progress(campaign.id) if gm_busy else ("", "")
    interact = _interaction_state(world.state, user.id, pending, is_owner=is_owner, busy=gm_busy)
    music = music_view(
        world.state,
        clock_rules=rule_set.clock if rule_set is not None else None,
    )
    values = {
        "campaign": campaign,
        "world": library.world_templates.get(campaign.world_template),
        "scene": scene_view(
            world.state,
            spec,
            inventory_rules,
            rule_set.relations if rule_set else None,
            conditions=rule_set.conditions if rule_set is not None else None,
            pc=viewer_pc,
            item_links=compendium.item_links,
            ability_links=compendium.ability_links,
            creature_links=compendium.creature_links,
            place_links=compendium.place_links,
            sheet_links=compendium.sheet_links,
            track_inventory=campaign.use_inventory_space,
        ),
        "world_map": world_map,
        "time": time_view(world.state, rule_set.clock if rule_set else None),
        "story": story_view(world.state),
        "rule_set": rule_set,
        "compendium": compendium,
        "creature_links": compendium.creature_links,
        "place_links": compendium.place_links,
        "messages": messages,
        "transcript": transcript,
        "player_name": player_name,
        "acting_name": acting_name,
        "context": context,
        "draft": draft,
        "pending": pending,
        "advancement_due": advancement_due,
        "advancement_available": advancement_available,
        "factions": faction_views(world.state, rule_set.tallies if rule_set else {}),
        "models": request.app.state.catalog.specs(),
        "credit_price": credit_price,
        "credit_balance": credit_balance,
        "credits_exempt": credits_exempt,
        "credits_blocked": not credits_ok,
        "credit_message": credit_message,
        "encounter": encounter,
        "encounter_rows": _encounter_rows(encounter) if encounter is not None else [],
        "local_map": local_map,
        "local_map_rows": _encounter_rows(local_map) if local_map is not None else [],
        "debug_panel": settings.debug_panel,
        "debug_currency": (
            rule_set.marketplace.currency if rule_set and rule_set.marketplace else ""
        ),
        "debug_stats": [(stat.id, stat.name) for stat in spec.stats.values()],
        "debug_tools": debug_tools,
        "debug_xp": debug_xp,
        "debug_advancement": debug_advancement,
        "debug_injection": world.state.debug_injection,
        "pc_invincible": viewer_pc.invincible if viewer_pc is not None else False,
        "item_kind_icons": magic.item_kind_icons if magic is not None else {},
        "is_owner": is_owner,
        "viewer_id": user.id,
        "show_internal_thinking": bool(user.show_internal_thinking),
        "music": music,
        "members": members,
        "invites": invites,
        "removed_members": removed_members,
        "unclaimed": [c.name for c in world.state.pcs() if c.unclaimed],
        "last_event": newest,
        "turn_status": interact["status"],
        "gm_busy": gm_busy,
        "gm_activity": gm_activity,
        "gm_reply": gm_reply,
        "waiting_for": interact["waiting_for"],
        "composer_blocked": interact["composer_blocked"],
        "pending_for_viewer": interact["pending_for_viewer"],
        "auto_start": interact["auto_start"] and credits_ok,
        "encounter_control": interact["encounter_control"],
    }
    if fragment:
        return render_fragment(request, "campaign_live.html", **values)
    return render(request, "campaign.html", status_code=status_code, error=error, **values)


@dataclass(frozen=True)
class _Interaction:
    """Who may act now, for the page's controls and the live refresh."""

    status: str
    waiting_for: str = ""
    composer_blocked: bool = False
    pending_for_viewer: bool = True
    auto_start: bool = False
    # "end", "start", or "" for the encounter controls.
    encounter_control: str = ""


def _interaction_state(
    state: CampaignState, user_id: int, pending: Any, *, is_owner: bool, busy: bool = False
) -> dict:
    """The page's view of whose turn it is. Every player sees the same turn, but only the
    player whose character is acting may post. The owner may also end anyone's turn."""
    status = "busy" if state.turn is not None else "idle"
    if pending is not None and pending.prompt is not None:
        status = "prompt"
    elif pending is not None and pending.error is not None:
        status = "failed"
    pending_for_viewer = pending is None or pending.player_id in (None, user_id)
    waiting_for = ""
    other_turn = False
    if pending is not None and not pending_for_viewer:
        waiting_for = f"{pending.player_name or 'Another player'} must answer the GM."
        other_turn = True
    open_turn = state.turn
    if open_turn is not None and open_turn.player_id not in (None, user_id):
        waiting_for = f"{open_turn.player_name or 'Another player'} is taking a turn."
        other_turn = True
    control = ""
    encounter = state.encounter
    if encounter is not None:
        actor = state.characters.get(name_key(encounter.acting)) if encounter.acting else None
        if actor is not None and actor.kind == PC_KIND:
            mine = state.owned_by(actor, user_id)
            if not mine:
                waiting_for = f"It is {actor.name}'s turn."
                other_turn = True
            if mine or is_owner:
                control = "end"
        elif actor is not None:
            waiting_for = f"The GM is playing {actor.name}'s turn."
            other_turn = True
        elif encounter.order and encounter.up < len(encounter.order):
            upcoming = state.characters.get(name_key(encounter.order[encounter.up]))
            if upcoming is not None:
                if upcoming.kind == PC_KIND:
                    mine = state.owned_by(upcoming, user_id)
                    if not mine:
                        waiting_for = f"Waiting for {upcoming.name}'s turn."
                        other_turn = True
                else:
                    control = "start"
    auto_start = bool(
        pending is not None
        and pending.auto_continue
        and (pending.player_id == user_id or (pending.player_id is None and is_owner))
    )
    composer_blocked = pending is not None or other_turn or busy
    return {
        "status": status,
        "waiting_for": waiting_for,
        "composer_blocked": composer_blocked,
        "pending_for_viewer": pending_for_viewer,
        "auto_start": auto_start,
        "encounter_control": control,
    }


def _local_map(state: CampaignState, rule_set: Any, player: Any = None) -> dict[str, Any] | None:
    """The map of the place the viewing player's character is in, if the place has one."""
    if rule_set is None:
        return None
    player = player if player is not None else state.pc()
    if player is None:
        return None
    return local_map_view(
        state,
        rule_set.grid,
        rule_set.terrain,
        rule_set.encounter_objects,
        state.characters,
        player.location,
    )


def _creation_pending(state: Any, rule_set: Any, user_id: int) -> tuple[bool, bool]:
    """Whether the campaign options and this player's character still need creating. Options can
    only be pending while there is no player character, so a campaign is set up in one pass."""
    options = rule_set.campaign if rule_set else NO_CAMPAIGN
    pc = state.player_character(user_id)
    return bool(options.fields) and not state.options, pc is None or pc.out_of_play


def _encounter_rows(encounter: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """The encounter map as rows of cells. Each cell has the background or marked terrain and
    object under it, the cell's own colour, pattern, icon, and label, and the participant
    standing on it, if any."""
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
            terrain_color = ground.get("terrain_color", ground.get("color", ""))
            terrain_pattern = ground.get("terrain_pattern", ground.get("pattern", ""))
            cell_color = mark.get("cell_color", "")
            blocks = bool(mark.get("blocks", False))
            label = mark.get("label", "")
            cells.append(
                {
                    "occupant": occupant,
                    "tip": _cell_tip(
                        occupant,
                        terrain_name,
                        terrain_rule,
                        mark.get("object", ""),
                        blocks,
                        label,
                    ),
                    "color": cell_color or terrain_color,
                    "cell_color": cell_color,
                    "terrain_color": terrain_color,
                    "terrain_name": terrain_name,
                    "terrain_pattern": terrain_pattern,
                    "pattern": mark.get("pattern", "") or terrain_pattern,
                    "difficult": bool(ground.get("difficult", False)),
                    "object_icon": mark.get("object_icon", ""),
                    "icon": mark.get("cell_icon", ""),
                    "object_color": mark.get("object_color", ""),
                    "object_solid": bool(mark.get("object_solid", False)),
                    "object_name": mark.get("object", ""),
                    "label": label,
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
    label: str = "",
) -> str:
    """The hover text for a cell: the token on it, the cell's label, the object, and the ground
    under it with its rule effect. A blocking object replaces that rule, since nothing can enter
    the cell."""
    where = " on ".join(part for part in (object_name, terrain_name) if part)
    if not where:
        return occupant["name"] if occupant else label
    rule = "Blocks movement: no one can enter this cell." if blocks else terrain_rule
    ground = f"{where}. {rule}" if rule else f"{where}."
    text = f"{occupant['name']} on {ground}" if occupant else ground
    return f"{label}: {text}" if label else text


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
