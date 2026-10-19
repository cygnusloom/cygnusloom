"""Operator commands: `python -m cygnusforge.cli reset-password EMAIL`."""

from __future__ import annotations

import argparse
import getpass
import sys

from cygnusforge.config import load_settings
from cygnusforge.db.session import make_engine
from cygnusforge.services.accounts import reset_password
from cygnusforge.services.common import InvalidInput, NotFound


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cygnusforge")
    sub = parser.add_subparsers(dest="command", required=True)
    reset = sub.add_parser("reset-password", help="set a new password for an account")
    reset.add_argument("email")
    args = parser.parse_args(argv)

    engine = make_engine(load_settings().database_url)
    password = getpass.getpass("New password: ")
    if password != getpass.getpass("Repeat password: "):
        print("passwords do not match", file=sys.stderr)
        return 1
    try:
        reset_password(engine, args.email, password)
    except (InvalidInput, NotFound) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"password updated for {args.email.strip().lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
