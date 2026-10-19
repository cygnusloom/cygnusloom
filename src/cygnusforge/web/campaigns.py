"""Campaign pages: list, create, and chat."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.datastructures import FormData
from starlette.responses import Response

from cygnusforge.auth.deps import page_user, verify_csrf
from cygnusforge.db.models import User
from cygnusforge.llm.base import LLMError
from cygnusforge.services.campaigns import (
    create_campaign,
    get_campaign,
    list_campaigns,
    list_messages,
    send_message,
)
from cygnusforge.services.common import InvalidInput
from cygnusforge.web.rendering import render

router = APIRouter(prefix="/campaigns")


def _form_text(form: FormData, name: str) -> str:
    value = form.get(name, "")
    return value if isinstance(value, str) else ""


@router.get("")
def campaigns_page(request: Request) -> Response:
    user = page_user(request)
    campaigns = list_campaigns(request.app.state.engine, user.id)
    return render(request, "campaigns.html", campaigns=campaigns)


@router.get("/new")
def new_campaign_page(request: Request) -> Response:
    page_user(request)
    return _new_form(request)


@router.post("/new")
async def create(request: Request) -> Response:
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    name = _form_text(form, "name")
    model = _form_text(form, "model")
    try:
        campaign = create_campaign(
            request.app.state.engine, user.id, name, model, request.app.state.settings.llm_models
        )
    except InvalidInput as exc:
        return _new_form(request, status_code=400, error=str(exc), name=name)
    return RedirectResponse(f"/campaigns/{campaign.id}", status_code=303)


@router.get("/{campaign_id}")
def campaign_page(request: Request, campaign_id: int) -> Response:
    user = page_user(request)
    return _campaign_page(request, user, campaign_id)


@router.post("/{campaign_id}/messages")
async def post_message(request: Request, campaign_id: int) -> Response:
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    text = _form_text(form, "content")
    try:
        await send_message(
            request.app.state.engine, request.app.state.providers, user.id, campaign_id, text
        )
    except InvalidInput as exc:
        return _campaign_page(request, user, campaign_id, 400, str(exc), text)
    except LLMError as exc:
        return _campaign_page(request, user, campaign_id, 502, str(exc), text)
    return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)


def _new_form(
    request: Request, status_code: int = 200, error: str = "", name: str = ""
) -> Response:
    settings = request.app.state.settings
    return render(
        request,
        "new_campaign.html",
        status_code=status_code,
        error=error,
        campaign_name=name,
        models=settings.llm_models,
        default_model=settings.llm_default_model,
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
    campaign = get_campaign(engine, user.id, campaign_id)
    return render(
        request,
        "campaign.html",
        status_code=status_code,
        campaign=campaign,
        messages=list_messages(engine, campaign.id),
        error=error,
        draft=draft,
    )
