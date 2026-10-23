from __future__ import annotations

import atexit
import functools
import re
import shutil
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cygnusforge.auth import passwords
from cygnusforge.config import Settings
from cygnusforge.content.library import DATA_DIR, ContentLibrary
from cygnusforge.llm.fake import FakeProvider
from cygnusforge.services.campaigns import narrate_instruction
from cygnusforge.web.app import create_app

SECRET = "test-secret-key-0123456789abcdef"
PASSWORD = "correct-horse-battery"
TOKEN_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
FIXTURES = Path(__file__).parent / "fixtures"


@functools.cache
def content_tree() -> Path:
    """The bundled content with the test-only fixtures added. The product never loads it."""
    root = Path(tempfile.mkdtemp(prefix="cygnusforge-content-"))
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    shutil.copytree(DATA_DIR, root, dirs_exist_ok=True)
    shutil.copytree(FIXTURES, root, dirs_exist_ok=True)
    return root


def content_library() -> ContentLibrary:
    return ContentLibrary.load(content_tree())


# The narration prompt for the rolltest rule set, which has no penalty item.
NARRATE_TEXT = narrate_instruction(content_library().rule_sets["rolltest"])


def make_app(settings: Settings, fake_provider: FakeProvider | None = None) -> FastAPI:
    """An app whose content includes the test-only fixtures."""
    app = create_app(settings, fake_provider=fake_provider)
    app.state.content = content_library()
    return app


class LiveNetworkBlocked(RuntimeError):
    """Raised when a test reaches the network through httpx's real transports."""


def _blocked(*_args: Any, **_kwargs: Any) -> None:
    raise LiveNetworkBlocked("live network call blocked in tests; use the fake provider")


@pytest.fixture(autouse=True)
def block_live_models(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _blocked)
    yield


@pytest.fixture(autouse=True)
def fast_password_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        passwords, "_hasher", PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)
    )


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        secret_key=SECRET,
        llm_models=("fake/scripted", "ollama/test-model"),
        llm_default_model="fake/scripted",
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


def csrf_token(client: TestClient, path: str) -> str:
    match = TOKEN_RE.search(client.get(path).text)
    assert match, f"no csrf token on {path}"
    return match.group(1)


def register(client: TestClient, email: str = "ada@example.com", password: str = PASSWORD):
    token = csrf_token(client, "/register")
    return client.post(
        "/register",
        data={"csrf_token": token, "email": email, "password": password},
        follow_redirects=False,
    )


def login(client: TestClient, email: str = "ada@example.com", password: str = PASSWORD):
    token = csrf_token(client, "/login")
    return client.post(
        "/login",
        data={"csrf_token": token, "email": email, "password": password},
        follow_redirects=False,
    )
