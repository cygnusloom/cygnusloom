"""Operator commands: the desktop launcher, migrations, and account inspection."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlmodel import select

from cygnusloom.config import load_settings
from cygnusloom.db.models import User
from cygnusloom.db.session import make_engine
from cygnusloom.services.common import session_scope

MIGRATIONS = Path(__file__).resolve().parent / "migrations"
COMMANDS = ("play", "migrate", "list-users", "where", "content")


def migration_config(database_url: str) -> AlembicConfig:
    """An Alembic config that works from a wheel, a source tree, or a PyInstaller bundle."""
    config = AlembicConfig()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _migrate() -> int:
    settings = load_settings()
    alembic_command.upgrade(migration_config(settings.database_url), "head")
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


def _where() -> int:
    from cygnusloom.launcher import data_dir, database_url

    root = data_dir()
    print(root)
    print(database_url(root))
    return 0


def _play(args: argparse.Namespace) -> int:
    from cygnusloom.launcher import play

    return play(
        data_dir_path=args.data_dir,
        port=args.port,
        host=args.host,
        headless=args.headless,
        log_level=args.log_level,
    )


def _content_validate(args: argparse.Namespace) -> int:
    from cygnusloom.content.definitions import ContentError
    from cygnusloom.content.library import DATA_DIR, ContentLibrary

    data_dir = Path(args.data_dir) if args.data_dir else DATA_DIR
    try:
        library = ContentLibrary.load(data_dir, validate_worlds=False)
    except ContentError as exc:
        print(f"error: {exc}")
        return 1
    issues = library.validate(all_pairs=args.all_pairs)
    print(
        f"loaded {len(library.world_templates)} world(s) and {len(library.rule_sets)} rule set(s)"
    )
    for severity, message in issues:
        print(f"{severity}: {message}")
    errors = sum(severity == "error" for severity, _ in issues)
    warnings = len(issues) - errors
    if errors or (args.strict and warnings):
        print(f"validation failed: {errors} error(s), {warnings} warning(s)")
        return 1
    print(f"validation passed: {warnings} warning(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        # A double-clicked executable starts the game.
        argv = ["play"]
    elif argv[0] not in COMMANDS and argv[0] not in ("-h", "--help"):
        argv = ["play", *argv]
    parser = argparse.ArgumentParser(prog="cygnusloom")
    sub = parser.add_subparsers(dest="command", required=True)
    play_parser = sub.add_parser("play", help="start the desktop app")
    play_parser.add_argument(
        "--data-dir",
        default=None,
        help="where the database, keys, and logs live (default: the per-user app folder)",
    )
    play_parser.add_argument(
        "--port", type=int, default=0, help="local port (default: choose a free one)"
    )
    play_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="address to bind (diagnostics only; the player UI is loopback)",
    )
    play_parser.add_argument(
        "--headless",
        action="store_true",
        help="run the local server without a window (diagnostics)",
    )
    play_parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, or ERROR")
    sub.add_parser("migrate", help="apply database migrations")
    sub.add_parser("list-users", help="print accounts without changing them")
    sub.add_parser("where", help="print the desktop app's data folder")
    content_parser = sub.add_parser("content", help="inspect and validate content")
    content_sub = content_parser.add_subparsers(dest="content_command", required=True)
    validate_parser = content_sub.add_parser("validate", help="check worlds against rule sets")
    validate_parser.add_argument(
        "--data-dir",
        default=None,
        help="content directory to load (default: the bundled data)",
    )
    validate_parser.add_argument(
        "--all-pairs",
        action="store_true",
        help="also try every world with every rule set, reporting mismatches as warnings",
    )
    validate_parser.add_argument(
        "--strict",
        action="store_true",
        help="treat warnings as a failed validation",
    )
    args = parser.parse_args(argv)
    if args.command == "play":
        return _play(args)
    if args.command == "migrate":
        return _migrate()
    if args.command == "where":
        return _where()
    if args.command == "content" and args.content_command == "validate":
        return _content_validate(args)
    return _list_users()


if __name__ == "__main__":
    raise SystemExit(main())
