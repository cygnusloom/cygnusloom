"""The page behind a campaign's share link: ask the signed-in account to join."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from cygnusloom.auth.deps import page_user, verify_csrf
from cygnusloom.services.common import InvalidInput, NotFound
from cygnusloom.services.membership import (
    accept_invite,
    invite_details,
    is_member,
    is_removed,
)
from cygnusloom.web.rendering import render

router = APIRouter()


@router.get("/join/{code}")
def join_page(request: Request, code: str) -> Response:
    user = page_user(request)
    details = invite_details(request.app.state.engine, code)
    if details is None:
        return render(
            request,
            "error.html",
            status_code=404,
            message="That join link is not valid.",
        )
    campaign = details.campaign
    if campaign.owner_id == user.id or is_member(request.app.state.engine, user.id, campaign.id):
        return RedirectResponse(f"/campaigns/{campaign.id}", status_code=303)
    if is_removed(request.app.state.engine, user.id, campaign.id):
        return _removed(request)
    return render(
        request,
        "join.html",
        status_code=200,
        campaign=campaign,
        owner_name=details.owner_name,
        label=details.label,
        code=code,
    )


@router.post("/join/{code}")
async def join_campaign(request: Request, code: str) -> Response:
    user = page_user(request)
    await verify_csrf(request)
    try:
        campaign = accept_invite(request.app.state.engine, user.id, code)
    except NotFound:
        return render(
            request,
            "error.html",
            status_code=404,
            message="That join link is not valid.",
        )
    except InvalidInput:
        return _removed(request)
    return RedirectResponse(f"/campaigns/{campaign.id}", status_code=303)


def _removed(request: Request) -> Response:
    return render(
        request,
        "error.html",
        status_code=403,
        message="The campaign owner removed this account. Ask them to invite you again.",
    )
