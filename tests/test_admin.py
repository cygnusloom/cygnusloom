from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, select

import cygnusloom
from cygnusloom.config import Settings
from cygnusloom.db.models import Campaign, Event, LlmModel, LlmUsage, User
from cygnusloom.db.session import create_schema, make_engine
from cygnusloom.services import admin as service
from cygnusloom.services.accounts import encode_roles, get_or_create_test_user
from cygnusloom.services.common import Conflict
from cygnusloom.services.sessions import create_session
from tests.conftest import csrf_token, make_app, register


def _engine(settings: Settings):
    engine = make_engine(settings.database_url)
    create_schema(engine)
    return engine


def _user(
    engine,
    email: str,
    roles: tuple[str, ...] = (),
    disabled: bool = False,
    subject: str | None = None,
) -> User:
    with Session(engine) as db:
        user = User(
            issuer="test",
            subject=subject or email,
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
    assert f"Cygnus Loom version {cygnusloom.__version__}" in page.text


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

    ada = _user(engine, "ada2@example.com")
    rows, total = service.search_users(engine, f"#{ada.id}")
    assert total == 1
    assert rows[0].user.id == ada.id


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
    actor = _user(engine, "admin@example.com", roles=("cygnusloom-admins",))
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
    actor = _user(engine, "admin@example.com", roles=("cygnusloom-admins",))
    with pytest.raises(Conflict, match="your own"):
        service.set_user_disabled(engine, actor, actor.id, True)


def test_delete_user_requires_campaigns_to_be_moved(settings: Settings):
    engine = _engine(settings)
    actor = _user(engine, "admin@example.com", roles=("cygnusloom-admins",))
    target = _user(engine, "target@example.com")
    _campaign(engine, target)
    with pytest.raises(Conflict, match="campaigns first"):
        service.delete_user(engine, actor, target.id)


def test_transfer_campaign_changes_owner_and_audits(settings: Settings):
    engine = _engine(settings)
    actor = _user(engine, "admin@example.com", roles=("cygnusloom-admins",))
    first = _user(engine, "first@example.com")
    second = _user(engine, "second@example.com")
    campaign = _campaign(engine, first)

    service.transfer_campaign(engine, actor, campaign.id, second.id)
    detail = service.campaign_detail(engine, campaign.id)
    assert detail.owner.email == "second@example.com"
    entries, total = service.audit_entries(engine)
    assert total == 1
    assert entries[0].action == "campaign.transferred"


def test_transfer_campaign_uses_account_id_when_emails_match(settings: Settings):
    engine = _engine(settings)
    actor = _user(engine, "admin@example.com", roles=("cygnusloom-admins",))
    first = _user(engine, "shared@example.com", subject="first-identity")
    second = _user(engine, "shared@example.com", subject="second-identity")
    campaign = _campaign(engine, first)

    service.transfer_campaign(engine, actor, campaign.id, second.id)
    detail = service.campaign_detail(engine, campaign.id)
    assert detail.owner.id == second.id


def test_delete_campaign_removes_events_and_audits(settings: Settings):
    engine = _engine(settings)
    actor = _user(engine, "admin@example.com", roles=("cygnusloom-admins",))
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
    assert "Account ID" in page.text

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


def test_admin_user_search_suggestions_match_by_id(settings: Settings):
    client = _admin_client(settings)
    register(client, "admin@example.com")
    engine = _engine(settings)
    target = get_or_create_test_user(engine, "target@example.com")

    response = client.get(f"/admin/users/search?q={target.id}")
    assert response.status_code == 200
    users = response.json()["users"]
    assert users[0]["id"] == target.id
    assert users[0]["email"] == "target@example.com"

    assert client.get("/admin/users/search?q=").json() == {"users": []}


def test_admin_can_transfer_a_campaign_by_account_id(settings: Settings):
    client = _admin_client(settings)
    register(client, "admin@example.com")
    engine = _engine(settings)
    owner = get_or_create_test_user(engine, "owner@example.com")
    new_owner = get_or_create_test_user(engine, "new-owner@example.com")
    campaign = _campaign(engine, owner, "A Dark Night")

    token = csrf_token(client, f"/admin/campaigns/{campaign.id}")
    response = client.post(
        f"/admin/campaigns/{campaign.id}/transfer",
        data={"csrf_token": token, "owner_id": str(new_owner.id)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert service.campaign_detail(engine, campaign.id).owner.id == new_owner.id


def test_transfer_rejects_a_non_numeric_account_id(settings: Settings):
    client = _admin_client(settings)
    register(client, "admin@example.com")
    engine = _engine(settings)
    owner = get_or_create_test_user(engine, "owner@example.com")
    campaign = _campaign(engine, owner, "A Dark Night")

    token = csrf_token(client, f"/admin/campaigns/{campaign.id}")
    response = client.post(
        f"/admin/campaigns/{campaign.id}/transfer",
        data={"csrf_token": token, "owner_id": "not-a-number"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/campaigns/{campaign.id}"
    assert service.campaign_detail(engine, campaign.id).owner.id == owner.id


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
    assert "Cache hit input" in page.text
    assert "Cache miss input" in page.text
    assert "Cost calculator" in page.text
    assert "Peak hours" in page.text
    assert 'data-calc="peak-start"' in page.text
    assert "Share of usage at peak" not in page.text
    assert "Peak" in page.text and "Off-peak" in page.text
    assert "Clear saved calculator values" in page.text
    assert 'action="/admin/models/calculators/clear"' in page.text
    assert "data-clear-calculators" not in page.text
    assert 'data-model="fake/scripted"' in page.text
    assert 'data-save-url="/admin/models/' in page.text
    assert "saved with the model" in page.text

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


def test_admin_can_save_and_clear_model_calculators(settings: Settings):
    client = _admin_client(settings)
    register(client, "admin@example.com")
    engine = _engine(settings)
    with Session(engine) as db:
        row = db.exec(select(LlmModel).where(LlmModel.model == "scripted")).one()
        row_id = row.id

    token = csrf_token(client, "/admin/models")
    rejected = client.post(
        f"/admin/models/{row_id}/calculator",
        data={"csrf_token": token, "hit-peak": "-1"},
        follow_redirects=False,
    )
    assert rejected.status_code == 400
    assert "non-negative" in rejected.json()["error"]

    token = csrf_token(client, "/admin/models")
    response = client.post(
        f"/admin/models/{row_id}/calculator",
        data={
            "csrf_token": token,
            "hit-peak": "0.25",
            "miss-peak": "0.5",
            "output-peak": "2",
            "hit-off-peak": "0.1",
            "miss-off-peak": "0.2",
            "output-off-peak": "1",
            "peak-start": "22:00",
            "peak-end": "06:00",
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True

    with Session(engine) as db:
        saved = db.get(LlmModel, row_id)
    assert saved is not None
    assert '"hit-peak": "0.25"' in saved.calculator_json
    page = client.get("/admin/models")
    assert 'value="0.25"' in page.text
    assert 'value="22:00"' in page.text

    token = csrf_token(client, "/admin/models")
    response = client.post(
        "/admin/models/calculators/clear",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/models"
    with Session(engine) as db:
        cleared = db.get(LlmModel, row_id)
    assert cleared is not None and cleared.calculator_json == "{}"
    page = client.get("/admin/models")
    assert "Cleared saved calculator values for" in page.text


def test_admin_models_page_hides_estimated_token_counts(settings: Settings):
    client = _admin_client(settings)
    register(client, "admin@example.com")
    engine = _engine(settings)
    with Session(engine) as db:
        db.add(
            LlmUsage(
                campaign_id=1,
                user_id=1,
                reply_event_id=1,
                model="fake/scripted",
                input_tokens=987654,
                cached_tokens=123456,
                output_tokens=555555,
                reasoning_tokens=0,
                estimated=True,
                created_at=datetime(2026, 1, 15, tzinfo=UTC),
            )
        )
        db.commit()

    page = client.get("/admin/models")
    assert page.status_code == 200
    assert "usage not reported" in page.text
    assert "1 unreported" in page.text
    assert "Calculate" in page.text
    assert "987654" not in page.text
    assert "555555" not in page.text


def test_admin_model_usage_breakdown_supports_peak_hours(settings: Settings):
    client = _admin_client(settings)
    register(client, "admin@example.com")
    engine = _engine(settings)
    with Session(engine) as db:
        db.add_all(
            [
                LlmUsage(
                    campaign_id=1,
                    user_id=1,
                    reply_event_id=1,
                    model="fake/scripted",
                    input_tokens=1000,
                    output_tokens=100,
                    estimated=False,
                    created_at=datetime(2026, 1, 1, 23, 0, tzinfo=UTC),
                ),
                LlmUsage(
                    campaign_id=1,
                    user_id=1,
                    reply_event_id=2,
                    model="fake/scripted",
                    input_tokens=500,
                    cached_tokens=100,
                    output_tokens=50,
                    estimated=False,
                    created_at=datetime(2026, 1, 2, 12, 0, tzinfo=UTC),
                ),
            ]
        )
        db.commit()

    response = client.get(
        "/admin/models/usage?model=fake/scripted&period=all&peak_start=22:00&peak_end=06:00"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["peak_hours"] == {"start": "22:00", "end": "06:00"}
    assert body["totals"]["calls"] == 2
    assert body["totals"]["cache_hit_tokens"] == 100
    assert body["totals"]["cache_miss_tokens"] == 1400
    assert body["peak"]["calls"] == 1
    assert body["peak"]["cache_miss_tokens"] == 1000
    assert body["off_peak"]["calls"] == 1
    assert body["off_peak"]["cache_hit_tokens"] == 100
    assert body["off_peak"]["cache_miss_tokens"] == 400

    plain = client.get("/admin/models/usage?model=fake/scripted&period=all")
    assert plain.status_code == 200
    assert plain.json()["peak"] is None
    assert plain.json()["off_peak"] is None

    missing = client.get("/admin/models/usage?model=fake/scripted&period=all&peak_start=22:00")
    assert missing.status_code == 400


def test_admin_models_page_filters_usage_by_date(settings: Settings):
    client = _admin_client(settings)
    register(client, "admin@example.com")
    engine = _engine(settings)
    with Session(engine) as db:
        db.add_all(
            [
                LlmUsage(
                    campaign_id=1,
                    user_id=1,
                    reply_event_id=1,
                    model="fake/scripted",
                    input_tokens=111111,
                    output_tokens=0,
                    estimated=False,
                    created_at=datetime(2026, 1, 15, tzinfo=UTC),
                ),
                LlmUsage(
                    campaign_id=1,
                    user_id=1,
                    reply_event_id=2,
                    model="fake/scripted",
                    input_tokens=222222,
                    output_tokens=0,
                    estimated=False,
                    created_at=datetime(2026, 2, 15, tzinfo=UTC),
                ),
            ]
        )
        db.commit()

    from_start = client.get("/admin/models?period=from&start=2026-02-01")
    assert from_start.status_code == 200
    assert "From 2026-02-01 (UTC)" in from_start.text
    assert "222222" in from_start.text
    assert "111111" not in from_start.text

    between = client.get("/admin/models?period=between&start=2026-01-01&end=2026-01-31")
    assert between.status_code == 200
    assert "2026-01-01 to 2026-01-31 (UTC)" in between.text
    assert "111111" in between.text
    assert "222222" not in between.text

    invalid = client.get("/admin/models?period=between&start=2026-02-01&end=2026-01-01")
    assert invalid.status_code == 200
    assert "start date must not be after" in invalid.text


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
