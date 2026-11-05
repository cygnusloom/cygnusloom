"""Operator commands: schema migration and read-only account inspection."""

from __future__ import annotations

import argparse
from pathlib import Path

from alembic.config import Config as AlembicConfig
from sqlmodel import select

from alembic import command as alembic_command
from cygnusloom.config import load_settings
from cygnusloom.db.models import User
from cygnusloom.db.session import make_engine
from cygnusloom.services.common import session_scope

ROOT = Path(__file__).resolve().parents[2]


def _migrate() -> int:
    settings = load_settings()
    config = AlembicConfig(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    alembic_command.upgrade(config, "head")
    print("database is at the latest migration")
    return 0


def _list_users() -> int:
    engine = make_engine(load_settings().database_url)
    with session_scope(engine) as db:
        users = db.exec(select(User).order_by(User.created_at)).all()
    if not users:
        print("no accounts")
        return 0
    for user in users:
        status = "disabled" if user.disabled else "active"
        print(f"{user.id}\t{user.email or user.subject}\t{status}\t{user.issuer}\t{user.roles}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cygnusloom")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply database migrations")
    sub.add_parser("list-users", help="print accounts without changing them")
    args = parser.parse_args(argv)
    if args.command == "migrate":
        return _migrate()
    return _list_users()


if __name__ == "__main__":
    raise SystemExit(main())
