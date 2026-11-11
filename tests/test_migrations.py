from __future__ import annotations

import re
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory
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
    assert "game_version" not in campaign_columns
    assert "force_credit_charges" in campaign_columns
    assert "use_inventory_space" in campaign_columns
    assert "major_version" in campaign_columns
    user_columns = {column["name"] for column in inspect(make_engine(url)).get_columns("user")}
    assert "credit_balance" in user_columns
    assert "show_internal_thinking" in user_columns
    assert "usage_filter_mode" in user_columns
    assert "usage_filter_start" in user_columns
    assert "usage_filter_end" in user_columns
    model_columns = {
        column["name"] for column in inspect(make_engine(url)).get_columns("llm_model")
    }
    assert "credits_per_turn" in model_columns
    assert "stream_usage" in model_columns
    assert "context_tokens" in model_columns
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


def test_model_context_tokens_default_to_64k_for_existing_models(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'model-context.db'}"
    monkeypatch.setenv("CYGNUSLOOM_DATABASE_URL", url)
    config = migration_config(url)
    command.upgrade(config, "0014_campaign_major_version")

    engine = make_engine(url)
    model = sa.Table("llm_model", sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(
            model.insert().values(
                provider="fake",
                model="scripted",
                base_url="",
                api_key="",
                credits_per_turn=0,
                stream_usage=True,
                calculator_json="{}",
                enabled=True,
                is_default=True,
                created_at=datetime.now(UTC),
            )
        )

    command.upgrade(config, "head")

    upgraded = make_engine(url)
    upgraded_model = sa.Table("llm_model", sa.MetaData(), autoload_with=upgraded)
    with upgraded.connect() as connection:
        value = connection.execute(sa.select(upgraded_model.c.context_tokens)).scalar_one()
    assert value == 65536


def test_migrations_can_be_reversed(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'reversed.db'}"
    monkeypatch.setenv("CYGNUSLOOM_DATABASE_URL", url)
    config = migration_config(url)

    command.upgrade(config, "head")
    command.downgrade(config, "base")

    tables = set(inspect(make_engine(url)).get_table_names())
    assert "user" not in tables
    assert "campaign" not in tables


def test_existing_campaigns_become_non_playable(tmp_path, monkeypatch):
    """3.x campaigns stay stored and exportable, but must not become playable under 4.0."""
    url = f"sqlite:///{tmp_path / 'campaign-major.db'}"
    monkeypatch.setenv("CYGNUSLOOM_DATABASE_URL", url)
    config = migration_config(url)
    command.upgrade(config, "0013_user_show_internal_thinking")
    with make_engine(url).begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO user (id, issuer, subject, email, email_verified, display_name, "
                "show_internal_thinking, roles, credit_balance, disabled, created_at) "
                "VALUES (1, 'issuer', 'subject', 'old@example.com', 1, 'Old', 0, '[]', 0, 0, "
                "CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO campaign "
                "(owner_id, name, world_template, rule_set, model, setting, table_genres, "
                "force_credit_charges, use_inventory_space, created_at) "
                "VALUES (1, 'Old', 'unwritten-world', 'cairn2e', 'fake/scripted', '', '', 0, 1, "
                "CURRENT_TIMESTAMP)"
            )
        )

    command.upgrade(config, "head")

    with make_engine(url).connect() as connection:
        major = connection.execute(sa.text("SELECT major_version FROM campaign")).scalar_one()
    assert major == 3


def test_every_revision_id_fits_alembics_version_column():
    """Alembic stores revision ids in VARCHAR(32), and Postgres enforces it."""
    script = ScriptDirectory.from_config(migration_config("sqlite:///unused.db"))
    for revision in script.walk_revisions():
        assert len(revision.revision) <= 32, revision.revision


def test_a_legacy_long_revision_id_is_rewritten(tmp_path, monkeypatch):
    """A database that recorded 3.7.0's long id still upgrades to the shortened revision."""
    url = f"sqlite:///{tmp_path / 'legacy-revision.db'}"
    monkeypatch.setenv("CYGNUSLOOM_DATABASE_URL", url)
    config = migration_config(url)
    command.upgrade(config, "0011_campaign_inventory_space")
    # SQLite applied 0012 under the old id, then recorded the id that PostgreSQL rejects.
    with make_engine(url).begin() as connection:
        connection.execute(sa.text("ALTER TABLE campaign DROP COLUMN game_version"))
        connection.execute(
            sa.text("UPDATE alembic_version SET version_num = :old"),
            {"old": "0012_remove_campaign_game_version"},
        )

    command.upgrade(config, "head")

    with make_engine(url).connect() as connection:
        version = connection.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    assert version == "0016_user_usage_filter"
    campaign_columns = {
        column["name"] for column in inspect(make_engine(url)).get_columns("campaign")
    }
    assert "game_version" not in campaign_columns
    user_columns = {column["name"] for column in inspect(make_engine(url)).get_columns("user")}
    assert "show_internal_thinking" in user_columns


def test_upgrading_from_0011_applies_the_shortened_0012(tmp_path, monkeypatch):
    """The failed PostgreSQL upgrade left the database at 0011; the shortened id now runs."""
    url = f"sqlite:///{tmp_path / 'from-0011.db'}"
    monkeypatch.setenv("CYGNUSLOOM_DATABASE_URL", url)
    config = migration_config(url)
    command.upgrade(config, "0011_campaign_inventory_space")

    command.upgrade(config, "head")

    campaign_columns = {
        column["name"] for column in inspect(make_engine(url)).get_columns("campaign")
    }
    assert "game_version" not in campaign_columns
    user_columns = {column["name"] for column in inspect(make_engine(url)).get_columns("user")}
    assert "show_internal_thinking" in user_columns


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
