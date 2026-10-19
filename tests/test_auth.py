from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace

from fastapi.testclient import TestClient
from sqlalchemy import text

from cygnusforge.config import Settings
from cygnusforge.db.session import make_engine
from cygnusforge.llm.fake import FakeProvider
from cygnusforge.services.accounts import reset_password
from cygnusforge.web.app import create_app
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


def test_test_user_button_is_hidden_by_default(client: TestClient):
    assert "Log in as test user" not in client.get("/login").text
    assert client.post("/login/test-user", data={"csrf_token": "x"}).status_code == 404


def _test_login_factory(settings: Settings, fake: FakeProvider) -> Callable[[], TestClient]:
    app = create_app(replace(settings, test_login=True), fake_provider=fake)
    return lambda: TestClient(app)


def test_test_user_button_shows_when_enabled(settings: Settings, fake: FakeProvider):
    page = _test_login_factory(settings, fake)().get("/login")
    assert page.status_code == 200
    assert 'action="/login/test-user"' in page.text
    assert "Log in as test user" in page.text


def test_test_user_button_stays_after_failed_login(settings: Settings, fake: FakeProvider):
    client = _test_login_factory(settings, fake)()
    assert login(client, "nobody@example.com").status_code == 401
    assert "Log in as test user" in client.get("/login").text


def test_test_user_button_signs_in(settings: Settings, fake: FakeProvider):
    client = _test_login_factory(settings, fake)()
    token = csrf_token(client, "/login")
    response = client.post("/login/test-user", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/campaigns"
    assert client.get("/campaigns", follow_redirects=False).status_code == 200


def test_test_user_is_the_same_account_every_time(settings: Settings, fake: FakeProvider):
    client_factory = _test_login_factory(settings, fake)
    for _ in range(2):
        client = client_factory()
        token = csrf_token(client, "/login")
        client.post("/login/test-user", data={"csrf_token": token})
    with make_engine(settings.database_url).connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM "user"')).scalar() == 1


def test_test_user_login_requires_csrf(settings: Settings, fake: FakeProvider):
    client = _test_login_factory(settings, fake)()
    csrf_token(client, "/login")
    response = client.post("/login/test-user", data={"csrf_token": "forged"})
    assert response.status_code == 403
    assert client.get("/campaigns", follow_redirects=False).headers["location"] == "/login"


def test_test_user_new_campaign_form_gets_random_names(settings: Settings, fake: FakeProvider):
    client = _test_login_factory(settings, fake)()
    token = csrf_token(client, "/login")
    client.post("/login/test-user", data={"csrf_token": token})
    page = client.get("/campaigns/new").text
    assert re.search(r'name="name" value="The [A-Za-z]+ [A-Za-z]+"', page)
    assert 'name="character_name"' not in page


def test_other_users_get_a_blank_new_campaign_form(client: TestClient):
    register(client)
    page = client.get("/campaigns/new").text
    assert 'name="name" value=""' in page
    assert 'name="character_name"' not in page
