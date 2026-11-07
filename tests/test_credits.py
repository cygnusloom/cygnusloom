from __future__ import annotations

import json
from dataclasses import replace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusloom.config import Settings
from cygnusloom.db.models import Campaign, CreditEntry, Event, LlmModel, LlmUsage, User
from cygnusloom.db.session import create_schema, make_engine
from cygnusloom.services import credits
from cygnusloom.services.events import ChatGM
from tests.conftest import csrf_token, make_app, register
from tests.factories import campaign_ready_fast, send_message

REPLY = "__FAKE__:" + json.dumps({"turns": [{"text": "The door creaks open."}]})


def _set_price(app: FastAPI, model_id: str, price: int) -> None:
    provider, _, model = model_id.partition("/")
    with Session(app.state.engine) as session:
        row = session.exec(
            select(LlmModel).where(LlmModel.provider == provider, LlmModel.model == model)
        ).one()
        row.credits_per_turn = price
        session.add(row)
        session.commit()


def _user(app: FastAPI, email: str) -> User:
    with Session(app.state.engine) as session:
        return session.exec(select(User).where(User.email == email)).one()


def _balance(app: FastAPI, email: str) -> int:
    return _user(app, email).credit_balance


def _count_events(app: FastAPI, campaign_id: int) -> int:
    with Session(app.state.engine) as session:
        return len(session.exec(select(Event).where(Event.campaign_id == campaign_id)).all())


def test_a_completed_reply_charges_the_owner_and_measures_usage(client_factory, app: FastAPI):
    client, campaign_id = campaign_ready_fast(client_factory)
    _set_price(app, "fake/scripted", 5)
    owner = _user(app, "ada@example.com")
    credits.set_balance(app.state.engine, owner.id, 10, reason=credits.ADMIN_REASON)

    response = send_message(client, campaign_id, REPLY)

    assert response.status_code == 303
    assert _balance(app, "ada@example.com") == 5
    with Session(app.state.engine) as session:
        entry = session.exec(
            select(CreditEntry).where(CreditEntry.reason == credits.TURN_REASON)
        ).one()
        assert entry.delta == -5
        assert entry.event_id is not None
        usage = session.exec(
            select(LlmUsage).where(LlmUsage.reply_event_id == entry.event_id)
        ).all()
    assert usage
    assert all(row.estimated for row in usage)
    assert sum(row.input_tokens for row in usage) > 0
    assert sum(row.output_tokens for row in usage) > 0

    summary = next(
        row for row in credits.usage_by_model(app.state.engine) if row.model == "fake/scripted"
    )
    assert summary.replies == 1
    assert summary.calls == len(usage)
    assert summary.estimated_calls == len(usage)
    assert summary.credits_charged == 5


def test_a_low_balance_blocks_before_the_message_is_recorded(client_factory, app: FastAPI):
    client, campaign_id = campaign_ready_fast(client_factory)
    _set_price(app, "fake/scripted", 5)
    owner = _user(app, "ada@example.com")
    credits.set_balance(app.state.engine, owner.id, 4, reason=credits.ADMIN_REASON)
    before = _count_events(app, campaign_id)

    response = send_message(client, campaign_id, REPLY)

    assert response.status_code == 400
    assert "needs 5 credits per reply" in response.text
    assert _balance(app, "ada@example.com") == 4
    assert _count_events(app, campaign_id) == before


def test_an_administrator_is_exempt_until_a_debug_campaign_forces_charges(settings: Settings):
    app = make_app(replace(settings, test_login_admin=True))
    client = TestClient(app)
    client, campaign_id = campaign_ready_fast(lambda: client)
    _set_price(app, "fake/scripted", 5)
    admin = _user(app, "ada@example.com")
    credits.set_balance(app.state.engine, admin.id, 0, reason=credits.ADMIN_REASON)

    assert send_message(client, campaign_id, REPLY).status_code == 303
    assert _balance(app, "ada@example.com") == 0
    with Session(app.state.engine) as session:
        assert (
            session.exec(select(CreditEntry).where(CreditEntry.reason == credits.TURN_REASON)).all()
            == []
        )
        assert session.exec(select(LlmUsage)).all()

    credits.set_balance(app.state.engine, admin.id, 5, reason=credits.DEBUG_REASON)
    with Session(app.state.engine) as session:
        campaign = session.get(Campaign, campaign_id)
        assert campaign is not None
        campaign.force_credit_charges = True
        session.add(campaign)
        session.commit()

    assert send_message(client, campaign_id, REPLY).status_code == 303
    assert _balance(app, "ada@example.com") == 0


def test_single_player_is_exempt_unless_forced(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'single.db'}",
        secret_key="test-secret",
        single_player=True,
        base_url="http://127.0.0.1:8000",
    )
    engine = make_engine(settings.database_url)
    create_schema(engine)
    with Session(engine) as session:
        user = User(issuer="local", subject="player", email="player@localhost", roles="[]")
        session.add(user)
        session.commit()
        session.refresh(user)
        campaign = Campaign(
            owner_id=user.id,
            name="Solo",
            world_template="testhollow",
            rule_set="rolltest",
            model="fake/scripted",
        )
        session.add(campaign)
        session.commit()
        session.refresh(campaign)

    assert credits.is_exempt(engine, settings, campaign)
    campaign.force_credit_charges = True
    assert not credits.is_exempt(engine, settings, campaign)


def test_rollback_refunds_the_charges_for_removed_replies(client_factory, app: FastAPI):
    client, campaign_id = campaign_ready_fast(client_factory)
    _set_price(app, "fake/scripted", 5)
    owner = _user(app, "ada@example.com")
    credits.set_balance(app.state.engine, owner.id, 10, reason=credits.ADMIN_REASON)
    assert send_message(client, campaign_id, REPLY).status_code == 303
    assert _balance(app, "ada@example.com") == 5

    with Session(app.state.engine) as session:
        opening = session.exec(
            select(Event)
            .where(Event.campaign_id == campaign_id, Event.kind == ChatGM.kind)
            .order_by(Event.seq)
        ).first()
    assert opening is not None and opening.id is not None

    token = csrf_token(client, f"/campaigns/{campaign_id}")
    response = client.post(
        f"/campaigns/{campaign_id}/messages/{opening.id}/rollback",
        data={"csrf_token": token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert _balance(app, "ada@example.com") == 10
    with Session(app.state.engine) as session:
        refunds = session.exec(
            select(CreditEntry).where(CreditEntry.reason == credits.REFUND_REASON)
        ).all()
    assert [entry.delta for entry in refunds] == [5]


def test_an_administrator_sets_an_account_balance(settings: Settings):
    app = make_app(replace(settings, test_login_admin=True))
    client = TestClient(app)
    register(client, "admin@example.com")
    with Session(app.state.engine) as session:
        target = User(
            issuer="test",
            subject="target@example.com",
            email="target@example.com",
            email_verified=True,
            display_name="Target",
        )
        session.add(target)
        session.commit()
        session.refresh(target)
        target_id = target.id

    token = csrf_token(client, f"/admin/users/{target_id}")
    response = client.post(
        f"/admin/users/{target_id}/credits",
        data={"csrf_token": token, "balance": "42"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert _balance(app, "target@example.com") == 42
    assert "42 credits" in client.get(f"/admin/users/{target_id}").text
    assert "user.credits_set" in client.get("/admin/audit").text


def test_the_model_picker_shows_each_models_price_and_balance(client_factory, app: FastAPI):
    client, campaign_id = campaign_ready_fast(client_factory)
    _set_price(app, "fake/scripted", 4)

    page = client.get(f"/campaigns/{campaign_id}").text

    assert "4 credits per reply" in page
    assert "free" in page
    assert "The campaign owner's balance is <strong>0</strong> credits." in page
