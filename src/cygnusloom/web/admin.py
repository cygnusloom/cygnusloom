"""Administrator pages: account and campaign lookup, moderation, and audit history."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from cygnusloom.auth.deps import (
    admin_user,
    client_ip,
    require_recent_auth,
    verify_csrf,
)
from cygnusloom.services import admin as service
from cygnusloom.services import credits as credit_service
from cygnusloom.services.common import Conflict, InvalidInput, NotFound
from cygnusloom.web.forms import form_text
from cygnusloom.web.rendering import render

router = APIRouter(prefix="/admin")


def _page(request: Request) -> int:
    try:
        return max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        return 1


def _pages(total: int) -> int:
    return max(1, (total + service.PAGE_SIZE - 1) // service.PAGE_SIZE)


def _flash(request: Request, message: str, kind: str = "info") -> None:
    request.session["flash"] = {"message": message, "kind": kind}


def _back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _error(request: Request, url: str, exc: Exception) -> RedirectResponse:
    _flash(request, str(exc), "error")
    return _back(url)


def _turn_price(form) -> int:
    raw = form_text(form, "credits_per_turn").strip() or "0"
    try:
        return int(raw)
    except ValueError:
        raise InvalidInput("credits per turn must be a whole number")


@router.get("")
def dashboard(request: Request) -> Response:
    admin = admin_user(request)
    return render(
        request,
        "admin/dashboard.html",
        admin=admin,
        data=service.dashboard(request.app.state.engine),
    )


@router.get("/users")
def users(request: Request) -> Response:
    admin = admin_user(request)
    query = request.query_params.get("q", "")
    status = request.query_params.get("status", "")
    page = _page(request)
    rows, total = service.search_users(
        request.app.state.engine, query, status, page, service.PAGE_SIZE
    )
    return render(
        request,
        "admin/users.html",
        admin=admin,
        rows=rows,
        total=total,
        pages=_pages(total),
        page=page,
        query=query,
        status=status,
    )


@router.get("/users/{user_id}")
def user_page(request: Request, user_id: int) -> Response:
    admin = admin_user(request)
    detail = service.user_detail(request.app.state.engine, user_id)
    return render(request, "admin/user.html", admin=admin, detail=detail)


@router.post("/users/{user_id}/disabled")
async def set_disabled(request: Request, user_id: int) -> Response:
    admin = admin_user(request)
    require_recent_auth(request, f"/admin/users/{user_id}")
    await verify_csrf(request)
    form = await request.form()
    disabled = form_text(form, "disabled") == "1"
    try:
        service.set_user_disabled(
            request.app.state.engine, admin, user_id, disabled, ip=client_ip(request)
        )
    except (Conflict, InvalidInput, NotFound) as exc:
        return _error(request, f"/admin/users/{user_id}", exc)
    _flash(request, "Account disabled." if disabled else "Account enabled.")
    return _back(f"/admin/users/{user_id}")


@router.post("/users/{user_id}/sessions/revoke")
async def revoke_sessions(request: Request, user_id: int) -> Response:
    admin = admin_user(request)
    require_recent_auth(request, f"/admin/users/{user_id}")
    await verify_csrf(request)
    try:
        count = service.revoke_sessions(
            request.app.state.engine, admin, user_id, ip=client_ip(request)
        )
    except (Conflict, InvalidInput, NotFound) as exc:
        return _error(request, f"/admin/users/{user_id}", exc)
    _flash(request, f"Signed out {count} session(s).")
    return _back(f"/admin/users/{user_id}")


@router.post("/users/{user_id}/delete")
async def delete_user(request: Request, user_id: int) -> Response:
    admin = admin_user(request)
    require_recent_auth(request, f"/admin/users/{user_id}")
    await verify_csrf(request)
    form = await request.form()
    try:
        detail = service.user_detail(request.app.state.engine, user_id)
        expected = detail.user.email or detail.user.subject
    except NotFound as exc:
        return _error(request, "/admin/users", exc)
    if form_text(form, "confirm").strip() != expected:
        _flash(request, "Type the account email exactly to confirm deletion.", "error")
        return _back(f"/admin/users/{user_id}")
    try:
        service.delete_user(request.app.state.engine, admin, user_id, ip=client_ip(request))
    except (Conflict, InvalidInput, NotFound) as exc:
        return _error(request, f"/admin/users/{user_id}", exc)
    _flash(request, "Account deleted.")
    return _back("/admin/users")


@router.get("/campaigns")
def campaigns(request: Request) -> Response:
    admin = admin_user(request)
    query = request.query_params.get("q", "")
    page = _page(request)
    rows, total = service.search_campaigns(request.app.state.engine, query, page, service.PAGE_SIZE)
    return render(
        request,
        "admin/campaigns.html",
        admin=admin,
        rows=rows,
        total=total,
        pages=_pages(total),
        page=page,
        query=query,
    )


@router.get("/campaigns/{campaign_id}")
def campaign_page(request: Request, campaign_id: int) -> Response:
    admin = admin_user(request)
    detail = service.campaign_detail(request.app.state.engine, campaign_id)
    return render(
        request,
        "admin/campaign.html",
        admin=admin,
        detail=detail,
        models=request.app.state.catalog.model_ids(),
    )


@router.post("/campaigns/{campaign_id}/rename")
async def rename_campaign(request: Request, campaign_id: int) -> Response:
    admin = admin_user(request)
    await verify_csrf(request)
    form = await request.form()
    try:
        service.rename_campaign(
            request.app.state.engine,
            admin,
            campaign_id,
            form_text(form, "name"),
            ip=client_ip(request),
        )
    except (Conflict, InvalidInput, NotFound) as exc:
        return _error(request, f"/admin/campaigns/{campaign_id}", exc)
    _flash(request, "Campaign renamed.")
    return _back(f"/admin/campaigns/{campaign_id}")


@router.post("/campaigns/{campaign_id}/model")
async def set_campaign_model(request: Request, campaign_id: int) -> Response:
    admin = admin_user(request)
    await verify_csrf(request)
    form = await request.form()
    try:
        service.set_campaign_model(
            request.app.state.engine,
            admin,
            campaign_id,
            form_text(form, "model"),
            request.app.state.catalog.model_ids(),
            ip=client_ip(request),
        )
    except (Conflict, InvalidInput, NotFound) as exc:
        return _error(request, f"/admin/campaigns/{campaign_id}", exc)
    _flash(request, "Campaign model changed.")
    return _back(f"/admin/campaigns/{campaign_id}")


@router.post("/campaigns/{campaign_id}/transfer")
async def transfer_campaign(request: Request, campaign_id: int) -> Response:
    admin = admin_user(request)
    require_recent_auth(request, f"/admin/campaigns/{campaign_id}")
    await verify_csrf(request)
    form = await request.form()
    try:
        service.transfer_campaign(
            request.app.state.engine,
            admin,
            campaign_id,
            form_text(form, "owner_email"),
            ip=client_ip(request),
        )
    except (Conflict, InvalidInput, NotFound) as exc:
        return _error(request, f"/admin/campaigns/{campaign_id}", exc)
    _flash(request, "Campaign transferred.")
    return _back(f"/admin/campaigns/{campaign_id}")


@router.post("/campaigns/{campaign_id}/delete")
async def delete_campaign(request: Request, campaign_id: int) -> Response:
    admin = admin_user(request)
    require_recent_auth(request, f"/admin/campaigns/{campaign_id}")
    await verify_csrf(request)
    form = await request.form()
    try:
        detail = service.campaign_detail(request.app.state.engine, campaign_id)
    except NotFound as exc:
        return _error(request, "/admin/campaigns", exc)
    if form_text(form, "confirm").strip() != detail.campaign.name:
        _flash(request, "Type the campaign name exactly to confirm deletion.", "error")
        return _back(f"/admin/campaigns/{campaign_id}")
    try:
        service.delete_campaign(request.app.state.engine, admin, campaign_id, ip=client_ip(request))
    except (Conflict, InvalidInput, NotFound) as exc:
        return _error(request, f"/admin/campaigns/{campaign_id}", exc)
    _flash(request, "Campaign deleted.")
    return _back("/admin/campaigns")


@router.get("/models")
def models_page(request: Request) -> Response:
    admin = admin_user(request)
    catalog = request.app.state.catalog
    return render(
        request,
        "admin/models.html",
        admin=admin,
        rows=catalog.all_specs(),
        source=catalog.source(),
        usage=credit_service.usage_by_model(request.app.state.engine),
    )


@router.post("/models")
async def add_model(request: Request) -> Response:
    admin = admin_user(request)
    await verify_csrf(request)
    form = await request.form()
    try:
        spec = request.app.state.catalog.add(
            form_text(form, "provider"),
            form_text(form, "model"),
            form_text(form, "base_url"),
            form_text(form, "api_key"),
            credits_per_turn=_turn_price(form),
            stream_usage=form_text(form, "stream_usage") == "1",
            is_default=form_text(form, "is_default") == "1",
        )
    except (Conflict, InvalidInput, NotFound) as exc:
        return _error(request, "/admin/models", exc)
    service.record_audit(
        request.app.state.engine,
        actor=admin,
        action="model.added",
        target_type="model",
        target_id=spec.model_id,
        details={
            "default": spec.is_default,
            "credits_per_turn": spec.credits_per_turn,
            "stream_usage": spec.stream_usage,
        },
        ip=client_ip(request),
    )
    _flash(request, f"Added {spec.model_id}.")
    return _back("/admin/models")


@router.post("/models/{row_id}/update")
async def update_model(request: Request, row_id: int) -> Response:
    admin = admin_user(request)
    await verify_csrf(request)
    form = await request.form()
    raw_key = form_text(form, "api_key").strip()
    api_key: str | None = raw_key or None
    if form_text(form, "clear_api_key") == "1":
        api_key = ""
    try:
        spec = request.app.state.catalog.get(row_id)
        updated = request.app.state.catalog.update(
            row_id,
            base_url=form_text(form, "base_url"),
            api_key=api_key,
            # The single-player page hides prices, so keep whatever was stored.
            credits_per_turn=(
                None if request.app.state.settings.single_player else _turn_price(form)
            ),
            stream_usage=form_text(form, "stream_usage") == "1",
            enabled=form_text(form, "enabled") == "1",
            make_default=form_text(form, "is_default") == "1",
        )
    except (Conflict, InvalidInput, NotFound) as exc:
        return _error(request, "/admin/models", exc)
    service.record_audit(
        request.app.state.engine,
        actor=admin,
        action="model.updated",
        target_type="model",
        target_id=updated.model_id,
        details={
            "enabled": updated.enabled,
            "default": updated.is_default,
            "credits_per_turn": updated.credits_per_turn,
            "stream_usage": updated.stream_usage,
        },
        ip=client_ip(request),
    )
    _flash(request, f"Saved {spec.model_id}.")
    return _back("/admin/models")


@router.post("/models/{row_id}/default")
async def default_model(request: Request, row_id: int) -> Response:
    admin = admin_user(request)
    await verify_csrf(request)
    try:
        spec = request.app.state.catalog.set_default(row_id)
    except (Conflict, InvalidInput, NotFound) as exc:
        return _error(request, "/admin/models", exc)
    service.record_audit(
        request.app.state.engine,
        actor=admin,
        action="model.default_changed",
        target_type="model",
        target_id=spec.model_id,
        ip=client_ip(request),
    )
    _flash(request, f"{spec.model_id} is now the default.")
    return _back("/admin/models")


@router.post("/models/{row_id}/toggle")
async def toggle_model(request: Request, row_id: int) -> Response:
    admin = admin_user(request)
    await verify_csrf(request)
    try:
        spec = request.app.state.catalog.get(row_id)
        updated = request.app.state.catalog.set_enabled(row_id, not spec.enabled)
    except (Conflict, InvalidInput, NotFound) as exc:
        return _error(request, "/admin/models", exc)
    service.record_audit(
        request.app.state.engine,
        actor=admin,
        action="model.enabled" if updated.enabled else "model.disabled",
        target_type="model",
        target_id=updated.model_id,
        ip=client_ip(request),
    )
    _flash(request, f"{updated.model_id} is now {'enabled' if updated.enabled else 'disabled'}.")
    return _back("/admin/models")


@router.post("/models/{row_id}/delete")
async def delete_model(request: Request, row_id: int) -> Response:
    admin = admin_user(request)
    require_recent_auth(request, "/admin/models")
    await verify_csrf(request)
    form = await request.form()
    try:
        spec = request.app.state.catalog.get(row_id)
    except (Conflict, InvalidInput, NotFound) as exc:
        return _error(request, "/admin/models", exc)
    if form_text(form, "confirm").strip() != spec.model_id:
        _flash(request, "Type the model id exactly to confirm deletion.", "error")
        return _back("/admin/models")
    try:
        request.app.state.catalog.delete(row_id)
    except (Conflict, InvalidInput, NotFound) as exc:
        return _error(request, "/admin/models", exc)
    service.record_audit(
        request.app.state.engine,
        actor=admin,
        action="model.deleted",
        target_type="model",
        target_id=spec.model_id,
        ip=client_ip(request),
    )
    _flash(request, f"Deleted {spec.model_id}.")
    return _back("/admin/models")


@router.post("/users/{user_id}/credits")
async def set_user_credits(request: Request, user_id: int) -> Response:
    admin = admin_user(request)
    require_recent_auth(request, f"/admin/users/{user_id}")
    await verify_csrf(request)
    form = await request.form()
    try:
        balance = int(form_text(form, "balance"))
        service.set_user_credits(
            request.app.state.engine, admin, user_id, balance, ip=client_ip(request)
        )
    except ValueError:
        _flash(request, "Balance must be a whole number.", "error")
        return _back(f"/admin/users/{user_id}")
    except (Conflict, InvalidInput, NotFound) as exc:
        return _error(request, f"/admin/users/{user_id}", exc)
    _flash(request, f"Credit balance set to {balance}.")
    return _back(f"/admin/users/{user_id}")


@router.get("/audit")
def audit(request: Request) -> Response:
    admin = admin_user(request)
    page = _page(request)
    rows, total = service.audit_entries(request.app.state.engine, page, service.PAGE_SIZE)
    return render(
        request,
        "admin/audit.html",
        admin=admin,
        rows=rows,
        total=total,
        pages=_pages(total),
        page=page,
    )
