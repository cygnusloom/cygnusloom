from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from sqlalchemy import inspect

from alembic import command
from cygnusloom.db.session import make_engine

ROOT = Path(__file__).resolve().parents[1]


def test_migrations_create_the_full_schema(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv("CYGNUSLOOM_DATABASE_URL", url)
    config = Config(str(ROOT / "alembic.ini"))

    command.upgrade(config, "head")

    tables = set(inspect(make_engine(url)).get_table_names())
    assert {"user", "campaign", "event", "user_session", "admin_audit", "llm_model"} <= tables
    campaign_columns = {
        column["name"] for column in inspect(make_engine(url)).get_columns("campaign")
    }
    assert "setting" in campaign_columns
    assert "table_genres" in campaign_columns


def test_migrations_can_be_reversed(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'reversed.db'}"
    monkeypatch.setenv("CYGNUSLOOM_DATABASE_URL", url)
    config = Config(str(ROOT / "alembic.ini"))

    command.upgrade(config, "head")
    command.downgrade(config, "base")

    tables = set(inspect(make_engine(url)).get_table_names())
    assert "user" not in tables
    assert "campaign" not in tables
