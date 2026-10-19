"""Account pages: register, log in, and log out."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from cygnusforge.auth.deps import USER_KEY, current_user, verify_csrf
from cygnusforge.services.accounts import authenticate, get_or_create_test_user, register_user
from cygnusforge.services.common import Conflict, InvalidInput, NotFound
from cygnusforge.web.forms import form_text
from cygnusforge.web.rendering import render

router = APIRouter()

AUTH_PAGES = {"register": ("Register", "/register"), "login": ("Log in", "/login")}


def _too_many(request: Request) -> bool:
    key = request.client.host if request.client else "unknown"
    return not request.app.state.limiter.allow(key)


@router.get("/")
def home() -> Response:
    return RedirectResponse("/campaigns", status_code=303)


@router.get("/register")
def register_page(request: Request) -> Response:
    if current_user(request) is not None:
        return RedirectResponse("/campaigns", status_code=303)
    return render(
        request, "auth_form.html", mode="register", heading="Register", action="/register"
    )


@router.post("/register")
async def register(request: Request) -> Response:
    if _too_many(request):
        return _form_error(request, "register", 429, "Too many attempts. Try again later.")
    await verify_csrf(request)
    form = await request.form()
    email = form_text(form, "email")
    try:
        user = register_user(request.app.state.engine, email, form_text(form, "password"))
    except InvalidInput as exc:
        return _form_error(request, "register", 400, str(exc), email)
    except Conflict as exc:
        return _form_error(request, "register", 409, str(exc), email)
    _sign_in(request, user.id)
    return RedirectResponse("/campaigns", status_code=303)


@router.get("/login")
def login_page(request: Request) -> Response:
    if current_user(request) is not None:
        return RedirectResponse("/campaigns", status_code=303)
    return render(
        request,
        "auth_form.html",
        mode="login",
        heading="Log in",
        action="/login",
        test_login=request.app.state.settings.test_login,
    )


@router.post("/login")
async def login(request: Request) -> Response:
    if _too_many(request):
        return _form_error(request, "login", 429, "Too many attempts. Try again later.")
    await verify_csrf(request)
    form = await request.form()
    email = form_text(form, "email")
    user = authenticate(request.app.state.engine, email, form_text(form, "password"))
    if user is None:
        return _form_error(request, "login", 401, "Email or password is incorrect.", email)
    _sign_in(request, user.id)
    return RedirectResponse("/campaigns", status_code=303)


@router.post("/login/test-user")
async def login_as_test_user(request: Request) -> Response:
    if not request.app.state.settings.test_login:
        raise NotFound("not found")
    await verify_csrf(request)
    user = get_or_create_test_user(request.app.state.engine)
    _sign_in(request, user.id)
    return RedirectResponse("/campaigns", status_code=303)


@router.post("/logout")
async def logout(request: Request) -> Response:
    await verify_csrf(request)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def _sign_in(request: Request, user_id: int | None) -> None:
    # Clearing first drops any pre-login state; the CSRF token is issued again on the next page.
    request.session.clear()
    request.session[USER_KEY] = user_id


def _form_error(
    request: Request, mode: str, status: int, message: str, email: str = ""
) -> Response:
    heading, action = AUTH_PAGES[mode]
    return render(
        request,
        "auth_form.html",
        status_code=status,
        mode=mode,
        heading=heading,
        action=action,
        error=message,
        email=email,
        test_login=request.app.state.settings.test_login,
    )
