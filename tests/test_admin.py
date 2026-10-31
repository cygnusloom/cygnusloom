from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, select

import cygnusforge
from cygnusforge.config import Settings
from cygnusforge.db.models import Campaign, Event, LlmModel, User
from cygnusforge.db.session import create_schema, make_engine
from cygnusforge.services import admin as service
from cygnusforge.services.accounts import encode_roles, get_or_create_test_user
from cygnusforge.services.common import Conflict
from cygnusforge.services.sessions import create_session
from tests.conftest import csrf_token, make_app, register


def _engine(settings: Settings):
    engine = make_engine(settings.database_url)
    create_schema(engine)
    return engine


def _user(engine, email: str, roles: tuple[str, ...] = (), disabled: bool = False) -> User:
    with Session(engine) as db:
        user = User(
            issuer="test",
            subject=email,
            email=email,
            email_verified=True,
            display_name=email.split("@", 1)[0],
            roles=encode_roles(roles),
            disabled=disabled,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user


def _campaign(engine, owner: User, name: str = "The Long Road") -> Campaign:
    with Session(engine) as db:
        campaign = Campaign(
            owner_id=owner.id,
            name=name,
            world_template="twin-lakes",
            rule_set="cairn2e",
            model="fake/scripted",
        )
        db.add(campaign)
        db.commit()
        db.refresh(campaign)
        return campaign


def _event(engine, campaign: Campaign, seq: int = 1):
    with Session(engine) as db:
        db.add(
            Event(
                campaign_id=campaign.id,
                seq=seq,
                channel="transcript",
                kind="chat.player",
                payload="{}",
            )
        )
        db.commit()


def _admin_client(settings: Settings) -> TestClient:
    from dataclasses import replace

    from tests.conftest import make_app

    return TestClient(make_app(replace(settings, test_login_admin=True)))


def test_dashboard_counts_accounts_campaigns_and_events(settings: Settings):
    engine = _engine(settings)
    user = _user(engine, "ada@example.com")
    campaign = _campaign(engine, user)
    _event(engine, campaign)

    data = service.dashboard(engine)
    assert data.user_count == 1
    assert data.campaign_count == 1
    assert data.event_count == 1
    assert data.campaigns_by_model == (("fake/scripted", 1),)
    assert data.recent_users[0].email == "ada@example.com"
    assert data.recent_campaigns[0].campaign.name == "The Long Road"


def test_dashboard_shows_version(settings: Settings):
    client = _admin_client(settings)
    register(client, "admin@example.com")

    page = client.get("/admin")
    assert page.status_code == 200
    assert f"Cygnus Forge version {cygnusforge.__version__}" in page.text


def test_search_users_matches_email_name_and_status(settings: Settings):
    engine = _engine(settings)
    _user(engine, "ada@example.com")
    _user(engine, "grace@example.com", disabled=True)

    rows, total = service.search_users(engine, "grace")
    assert total == 1
    assert rows[0].user.email == "grace@example.com"
    assert rows[0].user.disabled is True

    rows, total = service.search_users(engine, status="active")
    assert total == 1
    assert rows[0].user.email == "ada@example.com"


def test_user_detail_includes_campaigns_sessions_and_audit(settings: Settings):
    engine = _engine(settings)
    user = _user(engine, "ada@example.com")
    campaign = _campaign(engine, user)
    _event(engine, campaign)
    create_session(engine, user.id, auth_time=user.created_at, hours=1)

    detail = service.user_detail(engine, user.id)
    assert detail.user.email == "ada@example.com"
    assert detail.campaigns[0].campaign.name == "The Long Road"
    assert detail.campaigns[0].event_count == 1
    assert len(detail.sessions) == 1


def test_disable_user_revokes_sessions_and_writes_audit(settings: Settings):
    engine = _engine(settings)
    actor = _user(engine, "admin@example.com", roles=("cygnusforge-admins",))
    target = _user(engine, "target@example.com")
    create_session(engine, target.id, auth_time=target.created_at, hours=1)

    service.set_user_disabled(engine, actor, target.id, True, ip="127.0.0.1")
    detail = service.user_detail(engine, target.id)
    assert detail.user.disabled is True
    assert detail.sessions == ()
    assert detail.audit[0].action == "user.disabled"
    assert detail.audit[0].actor_email == "admin@example.com"


def test_admin_cannot_disable_self(settings: Settings):
    engine = _engine(settings)
    actor = _user(engine, "admin@example.com", roles=("cygnusforge-admins",))
    with pytest.raises(Conflict, match="your own"):
        service.set_user_disabled(engine, actor, actor.id, True)


def test_delete_user_requires_campaigns_to_be_moved(settings: Settings):
    engine = _engine(settings)
    actor = _user(engine, "admin@example.com", roles=("cygnusforge-admins",))
    target = _user(engine, "target@example.com")
    _campaign(engine, target)
    with pytest.raises(Conflict, match="campaigns first"):
        service.delete_user(engine, actor, target.id)


def test_transfer_campaign_changes_owner_and_audits(settings: Settings):
    engine = _engine(settings)
    actor = _user(engine, "admin@example.com", roles=("cygnusforge-admins",))
    first = _user(engine, "first@example.com")
    second = _user(engine, "second@example.com")
    campaign = _campaign(engine, first)

    service.transfer_campaign(engine, actor, campaign.id, second.email)
    detail = service.campaign_detail(engine, campaign.id)
    assert detail.owner.email == "second@example.com"
    entries, total = service.audit_entries(engine)
    assert total == 1
    assert entries[0].action == "campaign.transferred"


def test_delete_campaign_removes_events_and_audits(settings: Settings):
    engine = _engine(settings)
    actor = _user(engine, "admin@example.com", roles=("cygnusforge-admins",))
    owner = _user(engine, "owner@example.com")
    campaign = _campaign(engine, owner)
    _event(engine, campaign)

    service.delete_campaign(engine, actor, campaign.id)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM campaign")).scalar_one() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM event")).scalar_one() == 0
    entries, _ = service.audit_entries(engine)
    assert entries[0].action == "campaign.deleted"


def test_admin_panel_lists_and_manages_accounts(settings: Settings):
    client = _admin_client(settings)
    register(client, "admin@example.com")
    engine = _engine(settings)
    target = get_or_create_test_user(engine, "target@example.com")
    campaign = _campaign(engine, target, "A Dark Night")

    users = client.get("/admin/users?q=target")
    assert users.status_code == 200
    assert "target@example.com" in users.text

    page = client.get(f"/admin/users/{target.id}")
    assert page.status_code == 200
    assert "A Dark Night" in page.text
    assert "Roles (from the identity provider)" in page.text

    campaigns = client.get("/admin/campaigns?q=Dark")
    assert campaigns.status_code == 200
    assert "A Dark Night" in campaigns.text

    token = csrf_token(client, f"/admin/campaigns/{campaign.id}")
    response = client.post(
        f"/admin/campaigns/{campaign.id}/rename",
        data={"csrf_token": token, "name": "A Brighter Day"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(engine) as db:
        updated = db.get(Campaign, campaign.id)
        assert updated is not None and updated.name == "A Brighter Day"

    token = csrf_token(client, f"/admin/campaigns/{campaign.id}")
    response = client.post(
        f"/admin/campaigns/{campaign.id}/delete",
        data={"csrf_token": token, "confirm": "A Brighter Day"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(engine) as db:
        assert db.get(Campaign, campaign.id) is None


def test_admin_panel_requires_confirmation_text(settings: Settings):
    client = _admin_client(settings)
    register(client, "admin@example.com")
    engine = _engine(settings)
    target = get_or_create_test_user(engine, "target@example.com")

    token = csrf_token(client, f"/admin/users/{target.id}")
    response = client.post(
        f"/admin/users/{target.id}/delete",
        data={"csrf_token": token, "confirm": "wrong@example.com"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(engine) as db:
        assert db.get(User, target.id) is not None


def test_audit_page_shows_admin_actions(settings: Settings):
    client = _admin_client(settings)
    register(client, "admin@example.com")
    engine = _engine(settings)
    target = get_or_create_test_user(engine, "target@example.com")

    token = csrf_token(client, f"/admin/users/{target.id}")
    client.post(
        f"/admin/users/{target.id}/disabled",
        data={"csrf_token": token, "disabled": "1"},
        follow_redirects=False,
    )
    page = client.get("/admin/audit")
    assert page.status_code == 200
    assert "user.disabled" in page.text


def test_admin_model_page_lists_and_adds_models(settings: Settings):
    client = _admin_client(settings)
    register(client, "admin@example.com")
    page = client.get("/admin/models")
    assert page.status_code == 200
    assert "fake/scripted" in page.text
    assert "ollama/test-model" in page.text

    token = csrf_token(client, "/admin/models")
    response = client.post(
        "/admin/models",
        data={
            "csrf_token": token,
            "provider": "openai",
            "model": "gpt-4o-mini",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test",
            "is_default": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get("/admin/models")
    assert "openai/gpt-4o-mini" in page.text
    assert "sk-test" not in page.text

    engine = _engine(settings)
    with Session(engine) as db:
        row = db.exec(select(LlmModel).where(LlmModel.provider == "openai")).one()
    assert row.is_default is True
    assert row.api_key != "sk-test"

    audit = client.get("/admin/audit")
    assert "model.added" in audit.text


def test_admin_model_page_requires_confirmation_to_delete(settings: Settings):
    client = _admin_client(settings)
    register(client, "admin@example.com")
    engine = _engine(settings)
    with Session(engine) as db:
        row = db.exec(select(LlmModel).where(LlmModel.model == "scripted")).one()
    token = csrf_token(client, "/admin/models")

    response = client.post(
        f"/admin/models/{row.id}/delete",
        data={"csrf_token": token, "confirm": "wrong"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(engine) as db:
        assert db.get(LlmModel, row.id) is not None

    token = csrf_token(client, "/admin/models")
    response = client.post(
        f"/admin/models/{row.id}/delete",
        data={"csrf_token": token, "confirm": "fake/scripted"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(engine) as db:
        assert db.get(LlmModel, row.id) is None
    assert "model.deleted" in client.get("/admin/audit").text


def test_no_models_blocks_campaign_creation_until_one_is_added(settings: Settings):
    empty = replace(settings, llm_models=(), llm_default_model="", test_login_admin=True)
    client = TestClient(make_app(empty))
    register(client, "admin@example.com")

    page = client.get("/campaigns/new")
    assert page.status_code == 503
    assert "No language model is configured" in page.text

    token = csrf_token(client, "/admin/models")
    response = client.post(
        "/admin/models",
        data={
            "csrf_token": token,
            "provider": "fake",
            "model": "scripted",
            "base_url": "",
            "is_default": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert client.get("/campaigns/new").status_code == 200
