"""Campaign pages: list, create, and chat."""

from __future__ import annotations

import json
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from starlette.responses import Response

from cygnusforge.auth.deps import page_user, verify_csrf
from cygnusforge.db.models import User
from cygnusforge.llm.base import LLMError
from cygnusforge.services.accounts import is_test_user
from cygnusforge.services.campaigns import (
    Delta,
    Logged,
    Prompt,
    Saved,
    Status,
    TurnEvent,
    campaign_log,
    campaign_world,
    context_usage,
    create_campaign,
    get_campaign,
    get_turn_context,
    get_turn_transcript,
    list_campaigns,
    list_messages,
    pending_turn,
    resume_turn,
    rollback_to,
    send_message,
    set_campaign_model,
    stream_message,
)
from cygnusforge.services.common import Conflict, InvalidInput, NotFound
from cygnusforge.services.names import random_campaign_name, random_character_name
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
    if is_test_user(user):
        rng = random.Random()
        return _new_form(
            request,
            name=random_campaign_name(rng),
            character_name=random_character_name(rng),
        )
    return _new_form(request)


@router.post("/new")
async def create(request: Request) -> Response:
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    name = form_text(form, "name")
    world_template = form_text(form, "world_template")
    rule_set = form_text(form, "rule_set")
    character_name = form_text(form, "character_name")
    settings = request.app.state.settings
    try:
        campaign = create_campaign(
            request.app.state.engine,
            request.app.state.content,
            settings.llm_models,
            user.id,
            name,
            world_template,
            rule_set,
            character_name,
            settings.llm_default_model,
        )
    except InvalidInput as exc:
        return _new_form(
            request,
            status_code=400,
            error=str(exc),
            name=name,
            world_template=world_template,
            rule_set=rule_set,
            character_name=character_name,
        )
    return RedirectResponse(f"/campaigns/{campaign.id}", status_code=303)


@router.get("/{campaign_id}")
def campaign_page(request: Request, campaign_id: int) -> Response:
    user = page_user(request)
    return _campaign_page(request, user, campaign_id)


@router.post("/{campaign_id}/model")
async def change_model(request: Request, campaign_id: int) -> Response:
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    model = form_text(form, "model")
    settings = request.app.state.settings
    try:
        set_campaign_model(
            request.app.state.engine, settings.llm_models, user.id, campaign_id, model
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
            settings.llm_models,
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
        settings.llm_models,
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
        settings.llm_models,
        request.app.state.content,
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
        case Logged(text):
            payload = {"type": "log", "text": text}
        case Prompt(text, dice, details):
            payload = {"type": "prompt", "text": text, "dice": dice, "details": details}
        case Saved(event_id):
            payload = {"type": "saved", "event_id": event_id}
        case TurnError(status, message):
            payload = {"type": "error", "status": status, "message": message}
    return json.dumps(payload)


def _new_form(
    request: Request,
    status_code: int = 200,
    error: str = "",
    name: str = "",
    world_template: str = "",
    rule_set: str = "",
    character_name: str = "",
) -> Response:
    library = request.app.state.content
    return render(
        request,
        "new_campaign.html",
        status_code=status_code,
        error=error,
        campaign_name=name,
        character_name=character_name,
        world_templates=library.world_templates.values(),
        rule_sets=library.rule_sets.values(),
        selected_world=world_template,
        selected_rule_set=rule_set,
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
    messages = list_messages(engine, campaign.id)
    world = campaign_world(engine, user.id, campaign.id)
    context = context_usage(
        campaign,
        messages,
        rule_set.rules if rule_set else {},
        settings.llm_context_tokens,
        world.text,
    )
    return render(
        request,
        "campaign.html",
        status_code=status_code,
        campaign=campaign,
        world=library.world_templates.get(campaign.world_template),
        world_view=world.view,
        rule_set=rule_set,
        messages=messages,
        log=campaign_log(engine, user.id, campaign.id),
        context=context,
        error=error,
        draft=draft,
        pending=pending_turn(engine, user.id, campaign.id),
        models=settings.llm_models,
    )
