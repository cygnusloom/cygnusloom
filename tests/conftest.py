from __future__ import annotations

import atexit
import functools
import os
import re
import shutil
import tempfile
import weakref
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event

from cygnusloom.config import Settings
from cygnusloom.content.library import DATA_DIR, ContentLibrary
from cygnusloom.db.session import make_engine
from cygnusloom.llm.fake import FakeProvider
from cygnusloom.services.prompts import narrate_instruction
from cygnusloom.web import app as web_app
from cygnusloom.web.app import create_app

SECRET = "test-secret-key-0123456789abcdef"
TOKEN_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
FIXTURES = Path(__file__).parent / "fixtures"


@functools.cache
def content_tree() -> Path:
    """The bundled content with the test-only fixtures added. The product never loads it."""
    root = Path(tempfile.mkdtemp(prefix="cygnusloom-content-"))
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    shutil.copytree(
        DATA_DIR,
        root,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("*.pdf", "*.md"),
    )
    shutil.copytree(FIXTURES, root, dirs_exist_ok=True)
    return root


@functools.cache
def content_library() -> ContentLibrary:
    """Loaded once per process. The library is immutable, so tests can share it."""
    return ContentLibrary.load(content_tree())


# The narration prompt for the rolltest rule set, which has no penalty item.
NARRATE_TEXT = narrate_instruction(content_library().rule_sets["rolltest"])


def make_app(
    settings: Settings,
    fake_provider: FakeProvider | None = None,
    oidc_provider=None,
) -> FastAPI:
    """An app whose content includes the test-only fixtures."""
    return create_app(
        settings,
        fake_provider=fake_provider,
        content=content_library(),
        oidc_provider=oidc_provider,
    )


def pytest_configure(config: pytest.Config) -> None:
    """Refuse a serial run: the suite is written for pytest-xdist. `CYGNUSLOOM_SERIAL_TESTS=1`
    allows one, for a debugger."""
    if hasattr(config, "workerinput") or os.environ.get("CYGNUSLOOM_SERIAL_TESTS") == "1":
        return
    if not getattr(config.option, "numprocesses", None):
        raise pytest.UsageError(
            "You ran the tests serially. This means you haven't read the documentation. Review your instructions."
        )


@pytest.fixture(autouse=True)
def no_fsync_databases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never need durable writes; skipping fsync makes each commit much cheaper."""

    def engine_without_fsync(url: str) -> Engine:
        engine = make_engine(url)

        @event.listens_for(engine, "connect")
        def _relax_sync(dbapi_connection, _record) -> None:
            dbapi_connection.execute("PRAGMA journal_mode=MEMORY")
            dbapi_connection.execute("PRAGMA temp_store=MEMORY")
            dbapi_connection.execute("PRAGMA synchronous=OFF")

        return engine

    monkeypatch.setattr(web_app, "make_engine", engine_without_fsync)


class LiveNetworkBlocked(RuntimeError):
    """Raised when a test reaches the network through httpx's real transports."""


def _blocked(*_args: Any, **_kwargs: Any) -> None:
    raise LiveNetworkBlocked("live network call blocked in tests; use the fake provider")


@pytest.fixture(autouse=True)
def block_live_models(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _blocked)
    yield


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        secret_key=SECRET,
        llm_models=("fake/scripted", "ollama/test-model"),
        llm_default_model="fake/scripted",
        test_login=True,
        base_url="http://testserver",
    )


@pytest.fixture
def fake() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def app(settings: Settings, fake: FakeProvider) -> FastAPI:
    return make_app(settings, fake)


@pytest.fixture
def client_factory(app: FastAPI) -> Callable[[], TestClient]:
    """Each call returns a client with its own cookie jar, i.e. a separate browser."""
    return lambda: TestClient(app)


@pytest.fixture
def client(client_factory: Callable[[], TestClient]) -> TestClient:
    return client_factory()


JOURNAL_STEP = "Journal step."
REVIEW_STEP = "Turn review step."


def is_journal_messages(messages: list[dict[str, Any]]) -> bool:
    return any(m["role"] == "system" and JOURNAL_STEP in (m.get("content") or "") for m in messages)


def is_review_messages(messages: list[dict[str, Any]]) -> bool:
    return any(m["role"] == "system" and REVIEW_STEP in (m.get("content") or "") for m in messages)


def without_journal(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [call for call in calls if not is_journal_messages(call["messages"])]


_CSRF_TOKENS: weakref.WeakKeyDictionary[TestClient, tuple[str, str]] = weakref.WeakKeyDictionary()


def _session_cookie(client: TestClient) -> str:
    """The session cookie value. It changes on login and logout, which is when the CSRF token
    must be read again."""
    return "; ".join(f"{name}={value}" for name, value in sorted(client.cookies.items()))


def csrf_token(client: TestClient, path: str) -> str:
    """The session's CSRF token, cached until the session cookie changes. The product keeps one
    token for the session, so fetching it from a full page on every POST only slows the tests."""
    cookie = _session_cookie(client)
    cached = _CSRF_TOKENS.get(client)
    if cached is not None and cached[0] == cookie:
        return cached[1]
    match = TOKEN_RE.search(client.get(path).text)
    assert match, f"no csrf token on {path}"
    _CSRF_TOKENS[client] = (cookie, match.group(1))
    return match.group(1)


def register(client: TestClient, email: str = "ada@example.com", password: str | None = None):
    """Sign in a development account. The password argument is accepted and ignored so older
    call sites keep reading naturally while credentials live in the identity provider."""
    token = csrf_token(client, "/login")
    return client.post(
        "/login/test-user",
        data={"csrf_token": token, "email": email},
        follow_redirects=False,
    )


def login(client: TestClient, email: str = "ada@example.com", password: str | None = None):
    return register(client, email, password)
