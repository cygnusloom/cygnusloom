"""FastAPI application factory."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from cygnusforge.auth.deps import LoginRequired
from cygnusforge.auth.ratelimit import RateLimiter
from cygnusforge.config import Settings, load_settings
from cygnusforge.content.library import ContentLibrary
from cygnusforge.db.session import create_schema, make_engine
from cygnusforge.llm.fake import FakeProvider
from cygnusforge.llm.registry import ProviderFactory
from cygnusforge.services.common import NotFound
from cygnusforge.web.accounts import router as accounts_router
from cygnusforge.web.campaigns import router as campaigns_router

SESSION_COOKIE = "cygnusforge_session"
SESSION_SECONDS = 14 * 24 * 60 * 60
STATIC_DIR = Path(__file__).parent / "static"


class RevalidatingStaticFiles(StaticFiles):
    """Static files that browsers must revalidate. Without this, a browser can reuse an old script
    from its heuristic cache after an update, and the page runs stale code until a hard refresh."""

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(
    settings: Settings,
    fake_provider: FakeProvider | None = None,
    content: ContentLibrary | None = None,
) -> FastAPI:
    """Build the app. Pass `fake_provider` to script the `fake/` models in tests, and `content`
    to reuse an already-loaded library instead of reading the bundled content again."""
    if not settings.secret_key:
        raise RuntimeError("CYGNUSFORGE_SECRET_KEY is required")
    app = FastAPI(title="Cygnus Forge")
    app.state.settings = settings
    app.state.engine = make_engine(settings.database_url)
    create_schema(app.state.engine)
    app.state.content = content if content is not None else ContentLibrary.load()
    app.state.limiter = RateLimiter()
    app.state.providers = ProviderFactory(settings, fake=fake_provider)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie=SESSION_COOKIE,
        max_age=SESSION_SECONDS,
        same_site="lax",
        https_only=False,
    )
    app.add_exception_handler(LoginRequired, _login_redirect)
    app.add_exception_handler(NotFound, _not_found)
    app.mount("/static", RevalidatingStaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(accounts_router)
    app.include_router(campaigns_router)
    return app


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def app_from_env() -> FastAPI:
    """Uvicorn entry point: `uvicorn --factory cygnusforge.web.app:app_from_env`."""
    settings = load_settings()
    configure_logging(settings.log_level)
    return create_app(settings)


async def _login_redirect(request: Request, exc: LoginRequired) -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


async def _not_found(request: Request, exc: NotFound) -> PlainTextResponse:
    return PlainTextResponse("not found", status_code=404)
