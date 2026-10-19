from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient

from cygnusforge.config import Settings
from cygnusforge.db.session import make_engine
from cygnusforge.services.accounts import reset_password
from tests.conftest import PASSWORD, csrf_token, login, register


def test_register_signs_in_and_opens_campaigns(client: TestClient):
    response = register(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/campaigns"
    page = client.get("/campaigns")
    assert page.status_code == 200
    assert "No campaigns yet" in page.text


def test_register_rejects_missing_csrf(client: TestClient):
    csrf_token(client, "/register")
    response = client.post(
        "/register",
        data={"csrf_token": "forged", "email": "ada@example.com", "password": PASSWORD},
    )
    assert response.status_code == 403


def test_register_rejects_duplicate_email(client_factory: Callable[[], TestClient]):
    register(client_factory())
    response = register(client_factory())
    assert response.status_code == 409
    assert "already exists" in response.text


def test_register_enforces_password_length(client: TestClient):
    response = register(client, password="short")
    assert response.status_code == 400
    assert "password must be" in response.text


def test_login_rejects_wrong_password(client_factory: Callable[[], TestClient]):
    register(client_factory())
    response = login(client_factory(), password="wrong-password-here")
    assert response.status_code == 401
    assert "incorrect" in response.text


def test_logout_ends_session(client: TestClient):
    register(client)
    token = csrf_token(client, "/campaigns")
    response = client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert client.get("/campaigns", follow_redirects=False).headers["location"] == "/login"


def test_logout_requires_csrf(client: TestClient):
    register(client)
    response = client.post("/logout", data={"csrf_token": "forged"})
    assert response.status_code == 403
    assert client.get("/campaigns").status_code == 200


def test_login_after_logout(client: TestClient):
    register(client)
    token = csrf_token(client, "/campaigns")
    client.post("/logout", data={"csrf_token": token})
    response = login(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/campaigns"


def test_protected_pages_redirect_to_login(client: TestClient):
    response = client.get("/campaigns", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_is_rate_limited(client_factory: Callable[[], TestClient]):
    client = client_factory()
    for _ in range(10):
        assert login(client, password="wrong-password-here").status_code == 401
    assert login(client, password="wrong-password-here").status_code == 429


def test_reset_password_changes_login(settings: Settings, client: TestClient):
    register(client)
    reset_password(make_engine(settings.database_url), "ada@example.com", "a-new-password-123")
    assert login(client, password=PASSWORD).status_code == 401
    assert login(client, password="a-new-password-123").status_code == 303
