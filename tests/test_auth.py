from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, select

from cygnusforge.auth.claims import OIDCError
from cygnusforge.config import Settings
from cygnusforge.db.models import User, UserSession
from cygnusforge.db.session import make_engine
from cygnusforge.services.accounts import get_or_create_test_user
from tests.conftest import csrf_token, login, make_app, register
from tests.fake_oidc import FakeOIDC, claims


def _engine(settings: Settings):
    return make_engine(settings.database_url)


def _user_count(settings: Settings) -> int:
    with _engine(settings).connect() as conn:
        return int(conn.execute(text('SELECT COUNT(*) FROM "user"')).scalar_one())


def _roles(settings: Settings, email: str) -> str:
    with Session(_engine(settings)) as db:
        user = db.exec(select(User).where(User.email == email)).first()
        assert user is not None
        return user.roles


def _client(settings: Settings, oidc: FakeOIDC | None = None) -> TestClient:
    return TestClient(make_app(settings, oidc_provider=oidc))


def test_login_page_shows_sso_button(settings: Settings):
    client = _client(settings, FakeOIDC())
    page = client.get("/login")
    assert page.status_code == 200
    assert "Continue with Test SSO" in page.text
    assert "Accounts, passwords, multi-factor authentication" in page.text


def test_test_login_signs_in_and_opens_campaigns(client: TestClient):
    response = register(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/campaigns"
    page = client.get("/campaigns")
    assert page.status_code == 200
    assert "No campaigns yet" in page.text


def test_test_login_rejects_missing_csrf(client: TestClient):
    csrf_token(client, "/login")
    response = client.post(
        "/login/test-user",
        data={"csrf_token": "forged", "email": "ada@example.com"},
    )
    assert response.status_code == 403


def test_test_login_is_hidden_by_default(settings: Settings):
    client = _client(replace(settings, test_login=False))
    page = client.get("/login")
    assert "Log in as test user" not in page.text
    assert client.post("/login/test-user", data={"csrf_token": "x"}).status_code == 404


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


def test_logout_uses_provider_logout_url(settings: Settings):
    oidc = FakeOIDC(
        identity=claims(id_token="raw-id-token"),
        logout="https://idp.example.com/bye",
    )
    client = _client(settings, oidc)
    client.get("/auth/callback")
    token = csrf_token(client, "/campaigns")
    response = client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "https://idp.example.com/bye"
    assert oidc.logout_hint == "raw-id-token"


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


def test_login_is_rate_limited(client: TestClient):
    for _ in range(10):
        assert (
            client.post(
                "/login/test-user", data={"csrf_token": "forged", "email": "ada@example.com"}
            ).status_code
            == 403
        )
    assert (
        client.post(
            "/login/test-user", data={"csrf_token": "forged", "email": "ada@example.com"}
        ).status_code
        == 429
    )


def test_oidc_login_creates_account(settings: Settings):
    fake = FakeOIDC(identity=claims(roles=("cygnusforge-admins",)))
    client = _client(settings, fake)
    response = client.get("/auth/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/callback"
    response = client.get("/auth/callback", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/campaigns"
    assert client.get("/campaigns").status_code == 200
    assert _user_count(settings) == 1
    assert _roles(settings, "ada@example.com") == '["cygnusforge-admins"]'


def test_oidc_login_only_keeps_safe_relative_next(settings: Settings):
    fake = FakeOIDC(identity=claims())
    client = _client(settings, fake)
    client.get("/auth/login?next=//evil.example", follow_redirects=False)
    response = client.get("/auth/callback", follow_redirects=False)
    assert response.headers["location"] == "/campaigns"

    other = _client(settings, fake)
    other.get("/auth/login?next=/account", follow_redirects=False)
    response = other.get("/auth/callback", follow_redirects=False)
    assert response.headers["location"] == "/account"


def test_oidc_rejects_bad_claims(settings: Settings):
    client = _client(settings, FakeOIDC(error=OIDCError("issuer mismatch")))
    client.get("/auth/login", follow_redirects=False)
    response = client.get("/auth/callback")
    assert response.status_code == 401
    assert "issuer mismatch" in response.text


def test_oidc_account_is_created_once_and_roles_refresh(settings: Settings):
    fake = FakeOIDC(identity=claims(roles=("cygnusforge-admins",)))
    first = _client(settings, fake)
    first.get("/auth/callback")
    assert first.get("/admin").status_code == 200

    fake.identity = claims(roles=())
    second = _client(settings, fake)
    second.get("/auth/callback")
    assert _user_count(settings) == 1
    assert _roles(settings, "ada@example.com") == "[]"
    assert second.get("/admin").status_code == 403


def test_oidc_disabled_account_cannot_sign_in(settings: Settings):
    client = _client(settings, FakeOIDC(identity=claims()))
    with Session(_engine(settings)) as db:
        db.add(
            User(
                issuer="https://idp.example.com",
                subject="subject-1",
                email="ada@example.com",
                disabled=True,
            )
        )
        db.commit()
    client.get("/auth/login", follow_redirects=False)
    response = client.get("/auth/callback")
    assert response.status_code == 403
    assert "disabled" in response.text


def test_admin_role_comes_from_claims(settings: Settings):
    client = _client(settings, FakeOIDC(identity=claims(roles=("cygnusforge-admins",))))
    client.get("/auth/callback")
    page = client.get("/admin")
    assert page.status_code == 200
    assert "Administration" in page.text


def test_non_admin_is_forbidden(settings: Settings):
    client = _client(settings, FakeOIDC(identity=claims()))
    client.get("/auth/callback")
    response = client.get("/admin")
    assert response.status_code == 403
    assert "Administrator access required" in response.text


def test_admin_step_up_refreshes_a_stale_session(settings: Settings):
    admin_settings = replace(settings, test_login_admin=True)
    client = _client(admin_settings)
    register(client, "admin@example.com")
    target = get_or_create_test_user(_engine(admin_settings), "target@example.com")

    engine = _engine(admin_settings)
    with Session(engine) as db:
        session = db.exec(select(UserSession)).first()
        assert session is not None
        session.auth_time = datetime.now(UTC) - timedelta(hours=2)
        db.add(session)
        db.commit()

    token = csrf_token(client, f"/admin/users/{target.id}")
    response = client.post(
        f"/admin/users/{target.id}/disabled",
        data={"csrf_token": token, "disabled": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/auth/step-up?next=")

    step = client.get(response.headers["location"], follow_redirects=False)
    assert step.status_code == 303
    assert step.headers["location"] == f"/admin/users/{target.id}"

    token = csrf_token(client, f"/admin/users/{target.id}")
    response = client.post(
        f"/admin/users/{target.id}/disabled",
        data={"csrf_token": token, "disabled": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(engine) as db:
        updated = db.get(User, target.id)
        assert updated is not None and updated.disabled is True


def test_test_user_new_campaign_form_gets_the_default_label(client: TestClient):
    register(client)
    page = client.get("/campaigns/new").text
    assert 'name="label" value="Test Hollow #1"' in page
    assert 'name="character_name"' not in page


def test_oidc_user_new_campaign_form_gets_the_default_label(settings: Settings):
    client = _client(settings, FakeOIDC(identity=claims()))
    client.get("/auth/callback")
    page = client.get("/campaigns/new").text
    assert 'name="label" value="Test Hollow #1"' in page
    assert 'name="character_name"' not in page


def test_account_page_shows_identity_and_sessions(client: TestClient):
    register(client)
    page = client.get("/account")
    assert page.status_code == 200
    assert "ada@example.com" in page.text
    assert "Signed-in devices" in page.text
    assert "test" in page.text


def test_sign_out_everywhere_revokes_all_clients(settings: Settings):
    first = _client(settings)
    second = _client(settings)
    register(first)
    register(second)
    token = csrf_token(first, "/account")
    response = first.post(
        "/account/sessions/revoke", data={"csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert first.get("/campaigns", follow_redirects=False).headers["location"] == "/login"
    assert second.get("/campaigns", follow_redirects=False).headers["location"] == "/login"


def test_healthz_checks_the_database(client: TestClient):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_login_is_required_for_account_page(client: TestClient):
    assert client.get("/account", follow_redirects=False).headers["location"] == "/login"
