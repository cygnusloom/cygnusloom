from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusloom.config import Settings, load_settings
from cygnusloom.db.models import User
from cygnusloom.db.session import make_engine
from cygnusloom.services.accounts import LOCAL_ISSUER, LOCAL_ROLE, user_roles
from tests.conftest import SECRET, csrf_token, make_app
from tests.factories import create_campaign

BASE = {"CYGNUSLOOM_SECRET_KEY": SECRET}


def _settings(tmp_path, **overrides) -> Settings:
    values = {
        "database_url": f"sqlite:///{tmp_path / 'solo.db'}",
        "secret_key": SECRET,
        "llm_models": ("fake/scripted",),
        "llm_default_model": "fake/scripted",
        "base_url": "http://127.0.0.1:8000",
        "single_player": True,
    }
    values.update(overrides)
    return Settings(**values)


def _users(settings: Settings) -> list[User]:
    with Session(make_engine(settings.database_url)) as db:
        return list(db.exec(select(User)).all())


def test_single_player_needs_no_oidc():
    settings = load_settings(
        {
            **BASE,
            "CYGNUSLOOM_SINGLE_PLAYER": "1",
            "CYGNUSLOOM_BASE_URL": "http://127.0.0.1:8765",
        }
    )
    assert settings.single_player is True
    assert settings.auto_create_schema is True
    assert settings.secure_cookies is False


@pytest.mark.parametrize(
    "override, message",
    [
        ({"CYGNUSLOOM_ENVIRONMENT": "production"}, "production"),
        ({"CYGNUSLOOM_TEST_LOGIN": "1"}, "local sign-in"),
        ({"CYGNUSLOOM_BASE_URL": "http://192.168.1.20:8000"}, "loopback"),
    ],
)
def test_single_player_rejects_unsafe_combinations(override, message):
    with pytest.raises(RuntimeError, match=message):
        load_settings(
            {
                **BASE,
                "CYGNUSLOOM_SINGLE_PLAYER": "1",
                "CYGNUSLOOM_BASE_URL": "http://127.0.0.1:8000",
                **override,
            }
        )


def test_single_player_signs_in_and_is_an_administrator(tmp_path):
    settings = _settings(tmp_path)
    client = TestClient(make_app(settings))

    page = client.get("/campaigns")

    assert page.status_code == 200
    assert "single-player" in page.text
    assert ">Admin<" in page.text
    assert "Log out" not in page.text
    users = _users(settings)
    assert len(users) == 1
    assert users[0].issuer == LOCAL_ISSUER
    assert users[0].display_name == "Local player"
    assert LOCAL_ROLE in user_roles(users[0])


def test_single_player_login_page_goes_to_campaigns(tmp_path):
    client = TestClient(make_app(_settings(tmp_path)))

    response = client.get("/login", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/campaigns"


def test_single_player_account_page_describes_the_local_player(tmp_path):
    settings = _settings(tmp_path, data_dir=str(tmp_path))
    client = TestClient(make_app(settings))

    page = client.get("/account")

    assert page.status_code == 200
    assert "local single-player account" in page.text
    assert "Data folder" in page.text
    assert "Sign out everywhere" not in page.text


def test_single_player_shows_no_credit_information(tmp_path):
    settings = _settings(tmp_path, debug_panel=True)
    app = make_app(settings)
    client = TestClient(app)

    account = client.get("/account")
    assert account.status_code == 200
    assert "credit" not in account.text.lower()

    campaign_id = create_campaign(client, label="Solo")
    campaign = client.get(f"/campaigns/{campaign_id}")
    assert campaign.status_code == 200
    assert "credits per reply" not in campaign.text
    assert 'class="credit-meter"' not in campaign.text
    assert 'href="/credits"' not in campaign.text
    assert "Save credits" not in campaign.text

    models = client.get("/admin/models")
    assert models.status_code == 200
    assert "credit" not in models.text.lower()

    users = client.get("/admin/users")
    assert users.status_code == 200
    assert "credit" not in users.text.lower()

    user = _users(settings)[0]
    detail = client.get(f"/admin/users/{user.id}")
    assert detail.status_code == 200
    assert "credit" not in detail.text.lower()

    assert client.get("/credits").status_code == 404


def test_single_player_model_update_keeps_the_hidden_price(tmp_path):
    settings = _settings(tmp_path)
    app = make_app(settings)
    client = TestClient(app)
    spec = app.state.catalog.all_specs()[0]
    assert spec.row_id is not None
    app.state.catalog.update(spec.row_id, base_url="", credits_per_turn=7)
    token = csrf_token(client, "/admin/models")

    response = client.post(
        f"/admin/models/{spec.row_id}/update",
        data={
            "csrf_token": token,
            "base_url": "",
            "stream_usage": "1",
            "context_tokens": "65536",
            "enabled": "1",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert app.state.catalog.all_specs()[0].credits_per_turn == 7


def test_single_player_admin_actions_skip_step_up(tmp_path):
    settings = _settings(tmp_path)
    app = make_app(settings)
    client = TestClient(app)
    spec = app.state.catalog.all_specs()[0]
    assert spec.row_id is not None
    token = csrf_token(client, "/admin/models")

    response = client.post(
        f"/admin/models/{spec.row_id}/delete",
        data={"csrf_token": token, "confirm": spec.model_id},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/models"


def test_setup_is_not_available_outside_single_player(tmp_path):
    settings = _settings(tmp_path, single_player=False)
    client = TestClient(make_app(settings))

    assert client.get("/setup").status_code == 404
