from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from cygnusloom.config import Settings
from cygnusloom.db.models import Campaign, CreditEntry, Event, LlmModel, LlmUsage, User
from cygnusloom.db.session import create_schema, make_engine
from cygnusloom.services import credits, membership
from cygnusloom.services.accounts import encode_roles
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


def _set_roles(app: FastAPI, email: str, roles: tuple[str, ...]) -> None:
    with Session(app.state.engine) as session:
        user = session.exec(select(User).where(User.email == email)).one()
        user.roles = encode_roles(roles)
        session.add(user)
        session.commit()


def test_model_usage_averages_treat_cache_and_reasoning_as_subsets():
    usage = credits.ModelUsage(
        model="provider/model",
        calls=4,
        replies=3,
        reported_replies=2,
        cache_hit_tokens=41,
        cache_miss_tokens=60,
        output_tokens=61,
        reasoning_tokens=21,
        unreported_calls=1,
        credits_charged=0,
    )
    assert usage.reported_calls == 3
    assert usage.has_reported_usage is True
    assert usage.avg_cache_hit_input_tokens == 21
    assert usage.avg_cache_miss_input_tokens == 30
    assert usage.avg_output_tokens == 31
    assert usage.avg_reasoning_tokens == 11


def test_usage_by_model_excludes_estimates_and_filters_by_time(app: FastAPI):
    with Session(app.state.engine) as session:
        session.add_all(
            [
                LlmUsage(
                    campaign_id=1,
                    user_id=1,
                    reply_event_id=101,
                    model="provider/model",
                    input_tokens=1000,
                    cached_tokens=400,
                    output_tokens=300,
                    reasoning_tokens=100,
                    estimated=False,
                    created_at=datetime(2026, 1, 10, 12, tzinfo=UTC),
                ),
                LlmUsage(
                    campaign_id=1,
                    user_id=1,
                    reply_event_id=102,
                    model="provider/model",
                    input_tokens=500,
                    cached_tokens=100,
                    output_tokens=200,
                    reasoning_tokens=0,
                    estimated=False,
                    created_at=datetime(2026, 2, 10, 12, tzinfo=UTC),
                ),
                LlmUsage(
                    campaign_id=1,
                    user_id=1,
                    reply_event_id=103,
                    model="provider/model",
                    input_tokens=99999,
                    cached_tokens=0,
                    output_tokens=99999,
                    reasoning_tokens=0,
                    estimated=True,
                    created_at=datetime(2026, 2, 15, 12, tzinfo=UTC),
                ),
                LlmUsage(
                    campaign_id=1,
                    user_id=1,
                    reply_event_id=104,
                    model="fake/scripted",
                    input_tokens=88888,
                    cached_tokens=0,
                    output_tokens=77777,
                    reasoning_tokens=0,
                    estimated=True,
                    created_at=datetime(2026, 2, 15, 12, tzinfo=UTC),
                ),
            ]
        )
        session.commit()

    rows = credits.usage_by_model(app.state.engine)
    usage = next(row for row in rows if row.model == "provider/model")
    assert usage.calls == 3
    assert usage.replies == 3
    assert usage.reported_replies == 2
    assert usage.reported_calls == 2
    assert usage.cache_hit_tokens == 500
    assert usage.cache_miss_tokens == 1000
    assert usage.output_tokens == 500
    assert usage.reasoning_tokens == 100
    assert usage.unreported_calls == 1
    assert usage.has_reported_usage is True
    assert usage.avg_cache_hit_input_tokens == 250
    assert usage.avg_cache_miss_input_tokens == 500
    assert usage.avg_output_tokens == 250
    assert usage.avg_reasoning_tokens == 50

    fake = next(row for row in rows if row.model == "fake/scripted")
    assert fake.has_reported_usage is False
    assert fake.unreported_calls == 1
    assert fake.avg_cache_hit_input_tokens == 0
    assert fake.avg_cache_miss_input_tokens == 0

    later = next(
        row
        for row in credits.usage_by_model(app.state.engine, since=datetime(2026, 2, 1, tzinfo=UTC))
        if row.model == "provider/model"
    )
    assert later.calls == 2
    assert later.reported_replies == 1
    assert later.cache_hit_tokens == 100
    assert later.cache_miss_tokens == 400
    assert later.unreported_calls == 1
    assert later.avg_cache_miss_input_tokens == 400

    january = next(
        row
        for row in credits.usage_by_model(app.state.engine, until=datetime(2026, 2, 1, tzinfo=UTC))
        if row.model == "provider/model"
    )
    assert january.calls == 1
    assert january.reported_replies == 1
    assert january.unreported_calls == 0
    assert january.avg_cache_miss_input_tokens == 600


def test_usage_breakdown_splits_peak_hours_across_midnight(app: FastAPI):
    rows = [
        (201, 1000, 200, 100, 10, datetime(2026, 1, 1, 23, 30, tzinfo=UTC), False),
        (202, 500, 0, 50, 0, datetime(2026, 1, 2, 1, 30, tzinfo=UTC), False),
        (203, 300, 100, 25, 5, datetime(2026, 1, 2, 12, 0, tzinfo=UTC), False),
        (204, 99999, 0, 99999, 0, datetime(2026, 1, 2, 23, 45, tzinfo=UTC), True),
    ]
    with Session(app.state.engine) as session:
        for reply, input_tokens, cached, output, reasoning, created_at, estimated in rows:
            session.add(
                LlmUsage(
                    campaign_id=1,
                    user_id=1,
                    reply_event_id=reply,
                    model="provider/model",
                    input_tokens=input_tokens,
                    cached_tokens=cached,
                    output_tokens=output,
                    reasoning_tokens=reasoning,
                    estimated=estimated,
                    created_at=created_at,
                )
            )
        session.commit()

    breakdown = credits.usage_breakdown(
        app.state.engine,
        "provider/model",
        peak_start=time(22, 0),
        peak_end=time(6, 0),
    )
    assert breakdown.totals.calls == 3
    assert breakdown.totals.replies == 3
    assert breakdown.totals.cache_hit_tokens == 300
    assert breakdown.totals.cache_miss_tokens == 1500
    assert breakdown.totals.output_tokens == 175
    assert breakdown.totals.reasoning_tokens == 15
    assert breakdown.peak is not None and breakdown.peak.calls == 2
    assert breakdown.peak.cache_hit_tokens == 200
    assert breakdown.peak.cache_miss_tokens == 1300
    assert breakdown.peak.output_tokens == 150
    assert breakdown.peak.reasoning_tokens == 10
    assert breakdown.off_peak is not None and breakdown.off_peak.calls == 1
    assert breakdown.off_peak.cache_hit_tokens == 100
    assert breakdown.off_peak.cache_miss_tokens == 200
    assert breakdown.off_peak.output_tokens == 25
    assert breakdown.off_peak.reasoning_tokens == 5

    plain = credits.usage_breakdown(app.state.engine, "provider/model")
    assert plain.peak is None
    assert plain.off_peak is None
    assert plain.totals == breakdown.totals

    with pytest.raises(credits.InvalidInput, match="must differ"):
        credits.usage_breakdown(
            app.state.engine,
            "provider/model",
            peak_start=time(9, 0),
            peak_end=time(9, 0),
        )
    with pytest.raises(credits.InvalidInput, match="together"):
        credits.usage_breakdown(app.state.engine, "provider/model", peak_start=time(9, 0))


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
    assert summary.unreported_calls == len(usage)
    assert summary.has_reported_usage is False
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


def test_a_credit_exempt_owner_is_exempt_even_with_no_credits(client_factory, app: FastAPI):
    client, campaign_id = campaign_ready_fast(client_factory)
    _set_price(app, "fake/scripted", 5)
    _set_roles(app, "ada@example.com", ("CygnusLoom-Credit-Exempt",))
    assert _balance(app, "ada@example.com") == 0
    with Session(app.state.engine) as session:
        campaign = session.get(Campaign, campaign_id)
        assert campaign is not None
        assert credits.is_exempt(app.state.engine, app.state.settings, campaign)

    assert send_message(client, campaign_id, REPLY).status_code == 303
    assert _balance(app, "ada@example.com") == 0


def test_a_credit_exempt_member_does_not_exempt_the_owners_campaign(client_factory, app: FastAPI):
    _, campaign_id = campaign_ready_fast(client_factory)
    _set_price(app, "fake/scripted", 5)
    with Session(app.state.engine) as session:
        guest = User(
            issuer="test",
            subject="guest@example.com",
            email="guest@example.com",
            email_verified=True,
            display_name="Guest",
            roles=encode_roles(("CygnusLoom-Credit-Exempt",)),
        )
        session.add(guest)
        session.commit()
        session.refresh(guest)
        assert guest.id is not None
        guest_id = guest.id
    membership.add_member(app.state.engine, campaign_id, guest_id)
    assert membership.is_member(app.state.engine, guest_id, campaign_id)
    with Session(app.state.engine) as session:
        campaign = session.get(Campaign, campaign_id)
        assert campaign is not None
        assert not credits.is_exempt(app.state.engine, app.state.settings, campaign)


def test_force_credit_charges_overrides_the_credit_exempt_role(client_factory, app: FastAPI):
    _, campaign_id = campaign_ready_fast(client_factory)
    _set_roles(app, "ada@example.com", ("CygnusLoom-Credit-Exempt",))
    with Session(app.state.engine) as session:
        campaign = session.get(Campaign, campaign_id)
        assert campaign is not None
        assert credits.is_exempt(app.state.engine, app.state.settings, campaign)
        campaign.force_credit_charges = True
        session.add(campaign)
        session.commit()
    with Session(app.state.engine) as session:
        campaign = session.get(Campaign, campaign_id)
        assert campaign is not None
        assert not credits.is_exempt(app.state.engine, app.state.settings, campaign)


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
    assert 'class="credit-meter"' in page
    assert "0 / 4 credits" in page
    assert 'class="context-meter"' not in page
    assert 'href="/credits"' in page

    _set_price(app, "fake/scripted", 0)
    page = client.get(f"/campaigns/{campaign_id}").text

    assert 'class="context-meter"' in page
    assert 'class="credit-meter"' not in page


def test_the_credits_page_asks_the_administrator_and_is_linked_from_account(
    client_factory, app: FastAPI
):
    client, campaign_id = campaign_ready_fast(client_factory)
    _set_price(app, "fake/scripted", 4)

    campaign_page = client.get(f"/campaigns/{campaign_id}").text
    account_page = client.get("/account").text
    credits_page = client.get("/credits")

    assert 'href="/credits"' in campaign_page
    assert 'href="/credits"' in account_page
    assert credits_page.status_code == 200
    assert "Ask the administrator of this server to add credits" in credits_page.text
    assert "<strong>0</strong> credits" in credits_page.text
    assert "A server administrator can add an account to the" in credits_page.text
    assert "cygnusloom-credit-exempt" in credits_page.text
    assert "Only campaigns that account owns are exempt." in credits_page.text
    assert "not charged" not in credits_page.text
