from __future__ import annotations

import re
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import command
from sqlalchemy import inspect

from cygnusloom.cli import migration_config
from cygnusloom.db.session import make_engine


def test_migrations_create_the_full_schema(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv("CYGNUSLOOM_DATABASE_URL", url)
    config = migration_config(url)

    command.upgrade(config, "head")

    tables = set(inspect(make_engine(url)).get_table_names())
    assert {
        "user",
        "campaign",
        "event",
        "user_session",
        "admin_audit",
        "llm_model",
        "llm_usage",
        "credit_entry",
    } <= tables
    campaign_columns = {
        column["name"] for column in inspect(make_engine(url)).get_columns("campaign")
    }
    assert "setting" in campaign_columns
    assert "table_genres" in campaign_columns
    assert "game_version" in campaign_columns
    assert "force_credit_charges" in campaign_columns
    assert "use_inventory_space" in campaign_columns
    user_columns = {column["name"] for column in inspect(make_engine(url)).get_columns("user")}
    assert "credit_balance" in user_columns
    model_columns = {
        column["name"] for column in inspect(make_engine(url)).get_columns("llm_model")
    }
    assert "credits_per_turn" in model_columns
    assert "stream_usage" in model_columns
    assert "calculator_json" in model_columns


def test_the_inventory_space_column_defaults_to_true(tmp_path, monkeypatch):
    """Existing campaigns keep the rule set's inventory limits when the column is added."""
    url = f"sqlite:///{tmp_path / 'inventory-space.db'}"
    monkeypatch.setenv("CYGNUSLOOM_DATABASE_URL", url)
    config = migration_config(url)
    command.upgrade(config, "head")

    engine = make_engine(url)
    user = sa.Table("user", sa.MetaData(), autoload_with=engine)
    campaign = sa.Table("campaign", sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        owner_id = connection.execute(
            user.insert().values(
                issuer="test",
                subject="owner",
                email="owner@example.com",
                email_verified=False,
                display_name="Owner",
                roles="[]",
                credit_balance=0,
                disabled=False,
                created_at=datetime.now(UTC),
            )
        ).inserted_primary_key[0]
        connection.execute(
            campaign.insert().values(
                owner_id=owner_id,
                name="Existing campaign",
                world_template="testhollow",
                rule_set="rolltest",
                model="fake/scripted",
                created_at=datetime.now(UTC),
            )
        )
        value = connection.execute(sa.select(campaign.c.use_inventory_space)).scalar_one()
    assert value is True


def test_migrations_can_be_reversed(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'reversed.db'}"
    monkeypatch.setenv("CYGNUSLOOM_DATABASE_URL", url)
    config = migration_config(url)

    command.upgrade(config, "head")
    command.downgrade(config, "base")

    tables = set(inspect(make_engine(url)).get_table_names())
    assert "user" not in tables
    assert "campaign" not in tables


def test_display_name_migration_replaces_email_names(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'names.db'}"
    monkeypatch.setenv("CYGNUSLOOM_DATABASE_URL", url)
    config = migration_config(url)
    command.upgrade(config, "0008_credits")

    created = datetime.now(UTC)
    with make_engine(url).begin() as connection:
        connection.execute(
            sa.text(
                'INSERT INTO "user" (issuer, subject, email, email_verified, '
                "display_name, roles, disabled, created_at) "
                "VALUES (:issuer, :subject, :email, :verified, :name, '[]', 0, :created)"
            ),
            [
                {
                    "issuer": "idp",
                    "subject": "one",
                    "email": "ada@example.com",
                    "verified": True,
                    "name": "ada@example.com",
                    "created": created,
                },
                {
                    "issuer": "idp",
                    "subject": "two",
                    "email": "grace@example.com",
                    "verified": True,
                    "name": "Rowan Hart 1234",
                    "created": created,
                },
            ],
        )

    command.upgrade(config, "head")

    with make_engine(url).connect() as connection:
        rows = (
            connection.execute(sa.text('SELECT display_name FROM "user" ORDER BY id'))
            .scalars()
            .all()
        )
    assert re.fullmatch(r"[A-Z][a-z]+ [A-Z][a-z]+ \d{4}", rows[0])
    assert rows[1] == "Rowan Hart 1234"
