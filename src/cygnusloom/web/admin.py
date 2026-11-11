"""Administrator pages: account and campaign lookup, moderation, and audit history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.responses import Response

from cygnusloom.auth.deps import (
    admin_user,
    client_ip,
    require_recent_auth,
    verify_csrf,
)
from cygnusloom.config import DEFAULT_MODEL_CONTEXT_TOKENS
from cygnusloom.llm.catalog import CALCULATOR_FIELDS
from cygnusloom.services import admin as service
from cygnusloom.services import credits as credit_service
from cygnusloom.services.common import Conflict, InvalidInput, NotFound
from cygnusloom.web.forms import form_text
from cygnusloom.web.rendering import render

router = APIRouter(prefix="/admin")


@dataclass(frozen=True)
class UsagePeriod:
    """The date window selected on the measured-usage card. Dates are UTC calendar days; the
    end of a range includes the whole final day."""

    mode: str
    start: str
    end: str
    since: datetime | None
    until: datetime | None
    label: str
    error: str = ""


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_clock(value: str) -> time | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        return time.fromisoformat(cleaned)
    except ValueError:
        raise InvalidInput("peak hours must use HH:MM")


def _usage_period(request: Request) -> UsagePeriod:
    mode = request.query_params.get("period", "all").strip().lower()
    start_raw = request.query_params.get("start", "").strip()
    end_raw = request.query_params.get("end", "").strip()
    if mode not in {"all", "from", "between"}:
        mode = "all"
    if mode == "from":
        start = _parse_date(start_raw)
        if start is None:
            return UsagePeriod(
                mode, start_raw, end_raw, None, None, "All time", "Choose a valid start date."
            )
        return UsagePeriod(
            mode,
            start.isoformat(),
            end_raw,
            datetime.combine(start, time.min, tzinfo=UTC),
            None,
            f"From {start.isoformat()} (UTC)",
        )
    if mode == "between":
        start = _parse_date(start_raw)
        end = _parse_date(end_raw)
        if start is None or end is None:
            return UsagePeriod(
                mode,
                start_raw,
                end_raw,
                None,
                None,
                "All time",
                "Choose valid start and end dates.",
            )
        if start > end:
            return UsagePeriod(
                mode,
                start.isoformat(),
                end.isoformat(),
                None,
                None,
                "All time",
                "The start date must not be after the end date.",
            )
        return UsagePeriod(
            mode,
            start.isoformat(),
            end.isoformat(),
            datetime.combine(start, time.min, tzinfo=UTC),
            datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC),
            f"{start.isoformat()} to {end.isoformat()} (UTC)",
        )
    return UsagePeriod("all", start_raw, end_raw, None, None, "All time")


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


def _context_tokens(form, default: int = DEFAULT_MODEL_CONTEXT_TOKENS) -> int:
    raw = form_text(form, "context_tokens").strip()
    if not raw:
        # BACKWARDS-COMPATIBILITY: 4 - old admin pages predate this field.
        return default
    try:
        return int(raw)
    except ValueError:
        raise InvalidInput("context tokens must be a whole number")


def _owner_id(form) -> int:
    raw = form_text(form, "owner_id").strip()
    try:
        value = int(raw)
    except ValueError:
        raise InvalidInput("choose the new owner by account ID")
    if value <= 0:
        raise InvalidInput("choose the new owner by account ID")
    return value


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


@router.get("/users/search")
def search_accounts(request: Request) -> Response:
    """Account suggestions for the administrators' searcher. It uses the same lookup as the
    accounts page, so an id, email, display name, or identity-provider subject all work."""
    admin_user(request)
    query = request.query_params.get("q", "").strip()
    if not query:
        return JSONResponse({"users": []})
    rows, _ = service.search_users(request.app.state.engine, query, "", 1, service.PAGE_SIZE)
    return JSONResponse(
        {
            "users": [
                {
                    "id": row.user.id,
                    "display_name": row.user.display_name,
                    "email": row.user.email,
                    "subject": row.user.subject,
                    "disabled": row.user.disabled,
                    "campaigns": row.campaign_count,
                }
                for row in rows
            ]
        }
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
            _owner_id(form),
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
    period = _usage_period(request)
    rows = catalog.all_specs()
    usage = credit_service.usage_by_model(
        request.app.state.engine, since=period.since, until=period.until
    )
    return render(
        request,
        "admin/models.html",
        admin=admin,
        rows=rows,
        source=catalog.source(),
        usage=usage,
        default_context_tokens=DEFAULT_MODEL_CONTEXT_TOKENS,
        # Each configured model's card owns a calculator dialog. The usage table links to the
        # same dialog, which loads exact usage for the selected period when it opens.
        calculator_ids={
            spec.model_id: f"cost-calculator-{index}" for index, spec in enumerate(rows, start=1)
        },
        period=period,
    )


@router.get("/models/usage")
def model_usage_breakdown(request: Request) -> Response:
    """Exact reported usage for one model, optionally split by UTC peak hours. The calculator
    uses these totals so its cost figures are based on real calls, not a guessed share."""
    admin_user(request)
    model = request.query_params.get("model", "").strip()
    if not model:
        return JSONResponse({"error": "model is required"}, status_code=400)
    period = _usage_period(request)
    try:
        peak_start = _parse_clock(request.query_params.get("peak_start", ""))
        peak_end = _parse_clock(request.query_params.get("peak_end", ""))
        breakdown = credit_service.usage_breakdown(
            request.app.state.engine,
            model,
            since=period.since,
            until=period.until,
            peak_start=peak_start,
            peak_end=peak_end,
        )
    except InvalidInput as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    def slice_json(value: credit_service.UsageSlice) -> dict[str, int]:
        return {
            "calls": value.calls,
            "replies": value.replies,
            "cache_hit_tokens": value.cache_hit_tokens,
            "cache_miss_tokens": value.cache_miss_tokens,
            "output_tokens": value.output_tokens,
            "reasoning_tokens": value.reasoning_tokens,
        }

    return JSONResponse(
        {
            "model": breakdown.model,
            "period": {
                "mode": period.mode,
                "start": period.start,
                "end": period.end,
                "label": period.label,
            },
            "peak_hours": (
                {
                    "start": peak_start.strftime("%H:%M"),
                    "end": peak_end.strftime("%H:%M"),
                }
                if peak_start is not None and peak_end is not None
                else None
            ),
            "totals": slice_json(breakdown.totals),
            "peak": slice_json(breakdown.peak) if breakdown.peak is not None else None,
            "off_peak": (
                slice_json(breakdown.off_peak) if breakdown.off_peak is not None else None
            ),
        }
    )


@router.post("/models/calculators/clear")
async def clear_model_calculators(request: Request) -> Response:
    admin_user(request)
    await verify_csrf(request)
    count = request.app.state.catalog.clear_calculators()
    noun = "model" if count == 1 else "models"
    _flash(request, f"Cleared saved calculator values for {count} {noun}.")
    return _back("/admin/models")


@router.post("/models/{row_id}/calculator")
async def save_model_calculator(request: Request, row_id: int) -> Response:
    """Persist one model's calculator prices and optional peak hours."""
    admin_user(request)
    await verify_csrf(request)
    form = await request.form()
    settings = {field: form_text(form, field) for field in CALCULATOR_FIELDS if field in form}
    try:
        spec = request.app.state.catalog.set_calculator(row_id, settings)
    except (InvalidInput, NotFound) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "model": spec.model_id})


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
            context_tokens=_context_tokens(form),
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
            "context_tokens": spec.context_tokens,
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
            context_tokens=_context_tokens(form, spec.context_tokens),
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
            "context_tokens": updated.context_tokens,
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
