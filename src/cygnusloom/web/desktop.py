"""Desktop-app support: the local server list and browser-to-app sign-in.

The desktop window runs a local Cygnus Loom server for offline play. These routes let its main
menu manage servers on other machines, and let a sign-in completed in the host browser be handed
back to the app through a one-time code on the app's own loopback address.

The same module answers ``/desktop/info`` on every server. The desktop app uses it to recognize
a Cygnus Loom server before adding it to the list.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import secrets
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete
from sqlmodel import select
from starlette.responses import Response

from cygnusloom import __version__
from cygnusloom.auth.deps import (
    SESSION_KEY,
    client_ip,
    current_session,
    current_user,
    verify_csrf,
)
from cygnusloom.db.models import DesktopLogin, User, utcnow
from cygnusloom.desktop_servers import (
    FILE_NAME,
    ServerError,
    ServerStore,
    normalize_invite_url,
    normalize_server_url,
    origin_of,
)
from cygnusloom.services.accounts import public_name
from cygnusloom.services.common import NotFound, session_scope
from cygnusloom.services.sessions import as_utc, create_session
from cygnusloom.web.forms import form_text
from cygnusloom.web.rendering import render

router = APIRouter(prefix="/desktop")

FLOW_MINUTES = 10
CODE_MINUTES = 5
MAX_INFO_BYTES = 64 * 1024
_STATE_RE = re.compile(r"^[A-Za-z0-9_-]{16,200}$")


@dataclass(frozen=True)
class ServerInfo:
    """What a Cygnus Loom server says about itself. Nothing here is secret."""

    version: str = ""
    oidc_issuer: str = ""


class ServerCheckError(Exception):
    """A server address that did not answer as a Cygnus Loom server."""


def server_store(settings) -> ServerStore:
    """The server list, or NotFound when this is not the desktop app."""
    if not settings.desktop or not settings.data_dir:
        raise NotFound("the server list is only available in the desktop app")
    return ServerStore(Path(settings.data_dir) / FILE_NAME)


def optional_server_store(settings) -> ServerStore | None:
    """The server list for rendering, or None outside the desktop app."""
    if not settings.desktop or not settings.data_dir:
        return None
    return ServerStore(Path(settings.data_dir) / FILE_NAME)


def _flash(request: Request, message: str, kind: str = "info") -> None:
    request.session["flash"] = {"message": message, "kind": kind}


def _error(request: Request, message: str, status_code: int) -> Response:
    return render(request, "error.html", status_code=status_code, message=message)


def _safe_path(value: object) -> str:
    """Only same-site absolute paths are accepted as a post-sign-in destination."""
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
        return "/campaigns"
    if "\\" in value or "\n" in value or "\r" in value:
        return "/campaigns"
    return value


def _loopback_redirect(value: str) -> bool:
    """A desktop loopback callback, and nothing else. This keeps the code off the open web."""
    parsed = urlsplit(value or "")
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        return False
    if parsed.path != "/callback" or parsed.query or parsed.fragment:
        return False
    if parsed.username or parsed.password:
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    return port is not None and 0 < port < 65536


def _valid_state(value: str) -> bool:
    return bool(_STATE_RE.fullmatch(value or ""))


def _hash_code(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _callback_url(row: DesktopLogin, **params: str) -> str:
    query = urlencode({"state": row.state, **params})
    return f"{row.redirect_uri}?{query}"


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


def _http_probe(endpoint: str) -> tuple[int, bytes]:
    """Read the server information over plain HTTP, without building an SSL context."""
    parsed = urlsplit(endpoint)
    if parsed.scheme != "http" or not parsed.hostname:
        raise ServerCheckError("That server address is not a valid HTTP address.")
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=10.0)
    try:
        connection.request(
            "GET",
            path,
            headers={
                "Accept": "application/json",
                "User-Agent": f"CygnusLoom/{__version__}",
            },
        )
        response = connection.getresponse()
        return response.status, response.read(MAX_INFO_BYTES + 1)
    finally:
        connection.close()


def probe_server(url: str) -> ServerInfo:
    """Ask a server for ``/desktop/info``. Only that endpoint, with no cookies or credentials."""
    endpoint = f"{url}/desktop/info"
    try:
        if urlsplit(endpoint).scheme == "http":
            status, body = _http_probe(endpoint)
        else:
            with httpx.stream(
                "GET",
                endpoint,
                timeout=10.0,
                follow_redirects=False,
                headers={
                    "Accept": "application/json",
                    "User-Agent": f"CygnusLoom/{__version__}",
                },
            ) as response:
                status = response.status_code
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_INFO_BYTES:
                        raise ServerCheckError(f"{url} answered with too much server information.")
    except ssl.SSLError as exc:
        raise ServerCheckError(
            f"Could not establish a secure connection to {url}. "
            "Check this computer's SSL configuration and the server's certificate."
        ) from exc
    except (httpx.HTTPError, http.client.HTTPException, OSError, ValueError) as exc:
        raise ServerCheckError(
            f"Could not reach {url}. Check the address and that the server is running."
        ) from exc
    if status != 200:
        raise ServerCheckError(f"{url} did not answer as a Cygnus Loom server (HTTP {status}).")
    if len(body) > MAX_INFO_BYTES:
        raise ServerCheckError(f"{url} answered with too much server information.")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise ServerCheckError(
            f"{url} did not answer with Cygnus Loom server information."
        ) from exc
    if not isinstance(payload, dict) or payload.get("app") != "cygnusloom":
        raise ServerCheckError(f"{url} is not a Cygnus Loom server.")
    issuer = payload.get("oidc_issuer")
    return ServerInfo(
        version=str(payload.get("version") or ""),
        oidc_issuer=origin_of(issuer if isinstance(issuer, str) else ""),
    )


@router.get("/info")
def desktop_info(request: Request) -> JSONResponse:
    """Public server identity and desktop sign-in support. Safe for any client to read."""
    settings = request.app.state.settings
    return JSONResponse(
        {
            "app": "cygnusloom",
            "version": __version__,
            "oidc": bool(settings.oidc_issuer),
            "oidc_issuer": settings.oidc_issuer,
            "desktop_login": not settings.single_player,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/servers")
async def add_server(request: Request) -> Response:
    store = server_store(request.app.state.settings)
    await verify_csrf(request)
    form = await request.form()
    try:
        url = normalize_server_url(form_text(form, "url"))
        if store.find_by_url(url) is not None:
            _flash(request, f"{url} is already in the server list.", "info")
            return RedirectResponse("/campaigns", status_code=303)
        info = probe_server(url)
    except (ServerError, ServerCheckError) as exc:
        _flash(request, str(exc), "error")
        return RedirectResponse("/campaigns", status_code=303)
    server = store.add(url, form_text(form, "name"), oidc_issuer=info.oidc_issuer)
    _flash(request, f"Added {server.name}.", "info")
    return RedirectResponse("/campaigns", status_code=303)


@router.post("/servers/invite")
async def open_invite(request: Request) -> Response:
    server_store(request.app.state.settings)
    await verify_csrf(request)
    form = await request.form()
    try:
        invite_url = normalize_invite_url(form_text(form, "url"))
    except ServerError as exc:
        _flash(request, str(exc), "error")
        return RedirectResponse("/campaigns", status_code=303)
    return _no_store(render(request, "desktop_invite.html", invite_url=invite_url))


@router.post("/servers/{server_id}/rename")
async def rename_server(request: Request, server_id: str) -> Response:
    store = server_store(request.app.state.settings)
    await verify_csrf(request)
    form = await request.form()
    try:
        server = store.rename(server_id, form_text(form, "name"))
    except ServerError as exc:
        _flash(request, str(exc), "error")
        return RedirectResponse("/campaigns", status_code=303)
    _flash(request, f"Renamed the server to {server.name}.", "info")
    return RedirectResponse("/campaigns", status_code=303)


@router.post("/servers/{server_id}/remove")
async def remove_server(request: Request, server_id: str) -> Response:
    store = server_store(request.app.state.settings)
    await verify_csrf(request)
    try:
        server = store.get(server_id)
        store.remove(server_id)
    except ServerError as exc:
        _flash(request, str(exc), "error")
        return RedirectResponse("/campaigns", status_code=303)
    _flash(request, f"Removed {server.name} from this computer.", "info")
    return RedirectResponse("/campaigns", status_code=303)


@router.get("/login")
def desktop_login(request: Request) -> Response:
    """Start a browser sign-in that returns to the desktop app's loopback listener."""
    settings = request.app.state.settings
    if settings.single_player:
        return _error(request, "This server has no remote sign-in.", 404)
    if not request.app.state.limiter.allow(f"desktop-login:{client_ip(request)}"):
        return _error(request, "Too many desktop sign-in attempts. Try again later.", 429)
    redirect_uri = request.query_params.get("redirect_uri", "")
    state = request.query_params.get("state", "")
    next_path = _safe_path(request.query_params.get("next") or "/campaigns")
    if not _loopback_redirect(redirect_uri):
        return _error(request, "The desktop sign-in address is not valid.", 400)
    if not _valid_state(state):
        return _error(request, "The desktop sign-in request is not valid.", 400)
    flow = secrets.token_urlsafe(24)
    now = utcnow()
    with session_scope(request.app.state.engine) as db:
        db.execute(delete(DesktopLogin).where(DesktopLogin.expires_at <= now))
        db.add(
            DesktopLogin(
                flow=flow,
                redirect_uri=redirect_uri,
                state=state,
                next_path=next_path,
                created_at=now,
                expires_at=now + timedelta(minutes=FLOW_MINUTES),
            )
        )
        db.commit()
    approve = f"/desktop/approve?{urlencode({'flow': flow})}"
    if current_user(request) is not None:
        return RedirectResponse(approve, status_code=303)
    if request.app.state.oidc is not None:
        return RedirectResponse(f"/auth/login?next={quote(approve)}", status_code=303)
    return RedirectResponse(f"/login?next={quote(approve)}", status_code=303)


def _load_flow(request: Request, flow: str) -> DesktopLogin | None:
    if not flow:
        return None
    with session_scope(request.app.state.engine) as db:
        row = db.exec(select(DesktopLogin).where(DesktopLogin.flow == flow)).first()
        if row is None or as_utc(row.expires_at) <= datetime.now(UTC):
            return None
        return row


def _approve_redirect(flow: str) -> str:
    target = f"/desktop/approve?{urlencode({'flow': flow})}"
    return f"/login?next={quote(target)}"


@router.get("/approve")
def approve_page(request: Request, flow: str = "") -> Response:
    row = _load_flow(request, flow)
    if row is None:
        return _error(
            request, "This desktop sign-in request expired. Start again from the app.", 404
        )
    if row.code_hash:
        return _error(request, "This desktop sign-in was already approved.", 409)
    user = current_user(request)
    if user is None:
        return RedirectResponse(_approve_redirect(flow), status_code=303)
    settings = request.app.state.settings
    return _no_store(
        render(
            request,
            "desktop_authorize.html",
            flow=flow,
            display_name=public_name(user),
            server_url=settings.base_url,
        )
    )


@router.post("/approve")
async def approve(request: Request, flow: str = "") -> Response:
    row = _load_flow(request, flow)
    if row is None:
        return _error(
            request, "This desktop sign-in request expired. Start again from the app.", 404
        )
    await verify_csrf(request)
    form = await request.form()
    user = current_user(request)
    if user is None:
        return RedirectResponse(_approve_redirect(flow), status_code=303)
    if form_text(form, "deny") == "1":
        with session_scope(request.app.state.engine) as db:
            db.execute(delete(DesktopLogin).where(DesktopLogin.flow == flow))
            db.commit()
        return RedirectResponse(_callback_url(row, error="denied"), status_code=303)
    found = current_session(request)
    if found is None:
        return RedirectResponse(_approve_redirect(flow), status_code=303)
    if row.code_hash:
        return _error(request, "This desktop sign-in was already approved.", 409)
    code = secrets.token_urlsafe(32)
    now = utcnow()
    with session_scope(request.app.state.engine) as db:
        current = db.exec(select(DesktopLogin).where(DesktopLogin.flow == flow)).first()
        if current is None or current.code_hash:
            return _error(request, "This desktop sign-in was already approved.", 409)
        current.user_id = user.id
        current.auth_time = found[1].auth_time
        current.code_hash = _hash_code(code)
        current.expires_at = now + timedelta(minutes=CODE_MINUTES)
        db.add(current)
        db.commit()
    return RedirectResponse(_callback_url(row, code=code), status_code=303)


@router.get("/session")
def desktop_session(request: Request) -> Response:
    """Exchange a one-time code for a new session in the desktop app's webview."""
    code = request.query_params.get("code", "")
    if not code:
        return _error(request, "This desktop sign-in link is not valid.", 404)
    now = utcnow()
    with session_scope(request.app.state.engine) as db:
        row = db.exec(
            select(DesktopLogin).where(DesktopLogin.code_hash == _hash_code(code))
        ).first()
        if (
            row is None
            or row.user_id is None
            or row.used_at is not None
            or as_utc(row.expires_at) <= datetime.now(UTC)
        ):
            return _error(
                request,
                "This desktop sign-in link expired or was already used. Start again from the app.",
                404,
            )
        user = db.get(User, row.user_id)
        if user is None or user.disabled:
            return _error(request, "This account cannot sign in.", 403)
        row.used_at = now
        db.add(row)
        db.commit()
        auth_time = as_utc(row.auth_time) if row.auth_time is not None else now
        next_path = _safe_path(row.next_path)
    settings = request.app.state.settings
    session = create_session(
        request.app.state.engine,
        user.id,
        auth_time=auth_time,
        hours=settings.session_hours,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
    )
    request.session.clear()
    request.session[SESSION_KEY] = session.id
    response = RedirectResponse(next_path, status_code=303)
    response.headers["Cache-Control"] = "no-store"
    return response
