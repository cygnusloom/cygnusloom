"""FastAPI application factory."""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from cygnusloom import license_text
from cygnusloom.auth.deps import AdminRequired, LoginRequired, StepUpRequired
from cygnusloom.auth.oidc import OIDCProvider, build_oidc_provider
from cygnusloom.auth.ratelimit import RateLimiter
from cygnusloom.config import Settings, load_settings
from cygnusloom.content.library import ContentLibrary
from cygnusloom.db.session import create_schema, make_engine
from cygnusloom.llm.catalog import ModelCatalog
from cygnusloom.llm.fake import FakeProvider
from cygnusloom.llm.registry import ProviderFactory
from cygnusloom.services.common import NotFound
from cygnusloom.web.accounts import router as accounts_router
from cygnusloom.web.admin import router as admin_router
from cygnusloom.web.campaigns import router as campaigns_router
from cygnusloom.web.compendium import router as compendium_router
from cygnusloom.web.rendering import render

SESSION_COOKIE = "cygnusloom_session"
SECURE_SESSION_COOKIE = "__Host-cygnusloom_session"
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
    oidc_provider: OIDCProvider | None = None,
) -> FastAPI:
    """Build the app. Pass `fake_provider` to script the `fake/` models in tests, `content`
    to reuse an already-loaded library, and `oidc_provider` to bypass a live identity provider."""
    if not settings.secret_key:
        raise RuntimeError("CYGNUSLOOM_SECRET_KEY is required")
    app = FastAPI(title="Cygnus Loom")
    app.state.settings = settings
    app.state.engine = make_engine(settings.database_url)
    if settings.auto_create_schema:
        create_schema(app.state.engine)
    app.state.content = content if content is not None else ContentLibrary.load()
    app.state.limiter = RateLimiter()
    app.state.oidc = oidc_provider if oidc_provider is not None else build_oidc_provider(settings)
    app.state.catalog = ModelCatalog(app.state.engine, settings)
    app.state.catalog.seed_from_env()
    app.state.providers = ProviderFactory(settings, fake=fake_provider, catalog=app.state.catalog)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie=SECURE_SESSION_COOKIE if settings.secure_cookies else SESSION_COOKIE,
        max_age=settings.session_hours * 60 * 60,
        same_site="lax",
        https_only=settings.secure_cookies,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> JSONResponse:
        try:
            with app.state.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception:
            logging.getLogger(__name__).exception("health check failed")
            return JSONResponse({"status": "error"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.get("/license", include_in_schema=False)
    def license_page(request: Request) -> Response:
        """The AGPL notice and the source offer, reachable before signing in."""
        return render(request, "license.html")

    @app.get("/license.txt", include_in_schema=False)
    def license_file() -> PlainTextResponse:
        """The license text itself, so a deployment needs no other host to serve it."""
        return PlainTextResponse(license_text())

    app.add_exception_handler(LoginRequired, _login_redirect)
    app.add_exception_handler(AdminRequired, _forbidden)
    app.add_exception_handler(StepUpRequired, _step_up_redirect)
    app.add_exception_handler(NotFound, _not_found)
    app.mount("/static", RevalidatingStaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(accounts_router)
    app.include_router(campaigns_router)
    app.include_router(compendium_router)
    app.include_router(admin_router)
    return app


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def app_from_env() -> FastAPI:
    """Uvicorn entry point: `uvicorn --factory cygnusloom.web.app:app_from_env`."""
    settings = load_settings()
    configure_logging(settings.log_level)
    return create_app(settings)


async def _login_redirect(request: Request, exc: LoginRequired) -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


async def _forbidden(request: Request, exc: AdminRequired) -> Response:
    return render(request, "error.html", status_code=403, message="Administrator access required.")


async def _step_up_redirect(request: Request, exc: StepUpRequired) -> RedirectResponse:
    return RedirectResponse(f"/auth/step-up?next={quote(exc.next_path)}", status_code=303)


async def _not_found(request: Request, exc: NotFound) -> PlainTextResponse:
    return PlainTextResponse("not found", status_code=404)
