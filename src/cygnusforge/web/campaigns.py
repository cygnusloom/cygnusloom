"""Campaign pages: list, create, and chat."""

from __future__ import annotations

from urllib.parse import urlencode

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
    try:
        campaign = create_campaign(request.app.state.engine, user.id, name)
    except InvalidInput as exc:
        return _new_form(request, status_code=400, error=str(exc), name=name)
    return RedirectResponse(f"/campaigns/{campaign.id}", status_code=303)


@router.get("/{campaign_id}")
def campaign_page(request: Request, campaign_id: int) -> Response:
    user = page_user(request)
    model = request.query_params.get("model", "")
    return _campaign_page(request, user, campaign_id, model=model)


@router.post("/{campaign_id}/messages")
async def post_message(request: Request, campaign_id: int) -> Response:
    user = page_user(request)
    await verify_csrf(request)
    form = await request.form()
    text = _form_text(form, "content")
    model = _form_text(form, "model")
    settings = request.app.state.settings
    try:
        await send_message(
            request.app.state.engine,
            request.app.state.providers,
            settings.llm_models,
            user.id,
            campaign_id,
            text,
            model,
        )
    except InvalidInput as exc:
        return _campaign_page(request, user, campaign_id, 400, str(exc), text, model)
    except LLMError as exc:
        return _campaign_page(request, user, campaign_id, 502, str(exc), text, model)
    return RedirectResponse(
        f"/campaigns/{campaign_id}?{urlencode({'model': model})}", status_code=303
    )


def _new_form(
    request: Request, status_code: int = 200, error: str = "", name: str = ""
) -> Response:
    return render(
        request,
        "new_campaign.html",
        status_code=status_code,
        error=error,
        campaign_name=name,
    )


def _campaign_page(
    request: Request,
    user: User,
    campaign_id: int,
    status_code: int = 200,
    error: str = "",
    draft: str = "",
    model: str = "",
) -> Response:
    engine = request.app.state.engine
    settings = request.app.state.settings
    campaign = get_campaign(engine, user.id, campaign_id)
    return render(
        request,
        "campaign.html",
        status_code=status_code,
        campaign=campaign,
        messages=list_messages(engine, campaign.id),
        error=error,
        draft=draft,
        models=settings.llm_models,
        selected_model=model if model in settings.llm_models else settings.llm_default_model,
    )
