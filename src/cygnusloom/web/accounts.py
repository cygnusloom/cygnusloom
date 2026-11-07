"""Account pages: sign in through the identity provider, sign out, and account details."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from cygnusloom.auth.claims import OIDCError
from cygnusloom.auth.deps import (
    current_session,
    current_user,
    page_user,
    sign_in,
    sign_out,
    verify_csrf,
)
from cygnusloom.services.accounts import (
    TEST_USER_EMAIL,
    AccountDisabled,
    get_or_create_test_user,
    provision_user,
    user_roles,
)
from cygnusloom.services.campaigns import list_campaigns
from cygnusloom.services.common import InvalidInput, NotFound
from cygnusloom.services.sessions import active_sessions, revoke_user_sessions
from cygnusloom.web.forms import form_text
from cygnusloom.web.rendering import render

router = APIRouter()


def _too_many(request: Request) -> bool:
    key = request.client.host if request.client else "unknown"
    return not request.app.state.limiter.allow(key)


def _redirect_uri(request: Request) -> str:
    return f"{request.app.state.settings.base_url}/auth/callback"


def _provider(request: Request):
    provider = request.app.state.oidc
    if provider is None:
        raise NotFound("single sign-on is not configured")
    return provider


def _safe_next(value: object) -> str:
    """Only same-site absolute paths are accepted as a post-login destination."""
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
        return "/campaigns"
    if "\\" in value or "\n" in value or "\r" in value:
        return "/campaigns"
    return value


def _login_page(
    request: Request,
    *,
    status_code: int = 200,
    error: str | None = None,
    email: str = "",
    next_path: str = "",
) -> Response:
    settings = request.app.state.settings
    provider = request.app.state.oidc
    return render(
        request,
        "auth_form.html",
        status_code=status_code,
        heading="Log in",
        oidc_enabled=provider is not None,
        oidc_name=provider.name if provider is not None else settings.oidc_name,
        test_login=settings.test_login,
        error=error,
        email=email,
        next_path=next_path,
    )


@router.get("/")
def home() -> Response:
    return RedirectResponse("/campaigns", status_code=303)


@router.get("/login")
def login_page(request: Request) -> Response:
    next_path = _safe_next(request.query_params.get("next"))
    if current_user(request) is not None:
        return RedirectResponse(next_path, status_code=303)
    return _login_page(request, next_path=next_path if next_path != "/campaigns" else "")


@router.get("/auth/login")
async def auth_login(request: Request) -> Response:
    provider = _provider(request)
    next_path = _safe_next(request.query_params.get("next"))
    step_up = request.query_params.get("step_up") == "1"
    if current_user(request) is not None and not step_up:
        return RedirectResponse(next_path, status_code=303)
    request.session["oidc_next"] = next_path
    request.session["oidc_step_up"] = step_up
    return await provider.authorize_redirect(
        request, _redirect_uri(request), max_age=0 if step_up else None
    )


@router.get("/auth/callback")
async def auth_callback(request: Request) -> Response:
    provider = _provider(request)
    if _too_many(request):
        return _login_page(request, status_code=429, error="Too many attempts. Try again later.")
    try:
        claims = await provider.fetch_claims(request, _redirect_uri(request))
    except OIDCError as exc:
        return _login_page(request, status_code=401, error=str(exc))
    try:
        user = provision_user(request.app.state.engine, claims)
    except AccountDisabled as exc:
        return _login_page(request, status_code=403, error=str(exc))
    except InvalidInput as exc:
        return _login_page(request, status_code=401, error=str(exc))
    next_path = _safe_next(request.session.get("oidc_next"))
    step_up = bool(request.session.get("oidc_step_up"))
    sign_in(request, user, claims.auth_time, id_token=claims.id_token)
    if step_up:
        request.session["step_up_done"] = True
    return RedirectResponse(next_path, status_code=303)


@router.get("/auth/step-up")
async def step_up(request: Request) -> Response:
    """Ask the provider to re-authenticate before a destructive admin action."""
    user = page_user(request)
    next_path = _safe_next(request.query_params.get("next") or "/admin")
    provider = request.app.state.oidc
    if provider is None:
        # Development without an identity provider: refresh the local session in place.
        sign_in(request, user, datetime.now(UTC))
        request.session["step_up_done"] = True
        return RedirectResponse(next_path, status_code=303)
    request.session["oidc_next"] = next_path
    request.session["oidc_step_up"] = True
    return await provider.authorize_redirect(request, _redirect_uri(request), max_age=0)


@router.post("/login/test-user")
async def login_as_test_user(request: Request) -> Response:
    settings = request.app.state.settings
    if not settings.test_login:
        raise NotFound("not found")
    if _too_many(request):
        return _login_page(request, status_code=429, error="Too many attempts. Try again later.")
    await verify_csrf(request)
    form = await request.form()
    email = form_text(form, "email") or TEST_USER_EMAIL
    next_path = _safe_next(form_text(form, "next"))
    try:
        user = get_or_create_test_user(
            request.app.state.engine,
            email,
            roles=settings.oidc_admin_values if settings.test_login_admin else None,
        )
    except InvalidInput as exc:
        return _login_page(request, status_code=400, error=str(exc), email=email)
    sign_in(request, user, datetime.now(UTC))
    return RedirectResponse(next_path, status_code=303)


@router.post("/logout")
async def logout(request: Request) -> Response:
    await verify_csrf(request)
    found = current_session(request)
    id_token_hint = found[1].oidc_id_token if found is not None else None
    sign_out(request)
    provider = request.app.state.oidc
    if provider is not None:
        url = await provider.logout_url(
            request,
            f"{request.app.state.settings.base_url}/login",
            id_token_hint=id_token_hint,
        )
        if url:
            return RedirectResponse(url, status_code=303)
    return RedirectResponse("/login", status_code=303)


@router.get("/account")
def account_page(request: Request) -> Response:
    user = page_user(request)
    engine = request.app.state.engine
    found = current_session(request)
    return render(
        request,
        "account.html",
        user=user,
        roles=user_roles(user),
        sessions=active_sessions(engine, user.id or 0),
        campaign_count=len(list_campaigns(engine, user.id)),
        current_session_id=found[1].id if found is not None else "",
    )


@router.get("/credits")
def credits_page(request: Request) -> Response:
    """A placeholder for buying credits. Payment is not integrated, so the page tells the
    player to ask an administrator. Single-player accounts are always exempt, so the page is
    not part of that edition."""
    if request.app.state.settings.single_player:
        raise NotFound("credits are not used in single-player mode")
    return render(request, "credits.html", user=page_user(request))


@router.post("/account/sessions/revoke")
async def revoke_sessions(request: Request) -> Response:
    user = page_user(request)
    await verify_csrf(request)
    revoke_user_sessions(request.app.state.engine, user.id or 0)
    sign_out(request)
    return RedirectResponse("/login", status_code=303)
