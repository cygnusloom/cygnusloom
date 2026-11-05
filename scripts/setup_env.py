"""Create .env from .env.example with generated secrets.

Use this once, then edit the two domain names and the bootstrap email if they differ.
Only the Python standard library is needed, so it can run without installing the app.
"""

from __future__ import annotations

import argparse
import secrets
from pathlib import Path

SECRET_MARKER = "change-me"


def _set(text: str, key: str, value: str) -> str:
    prefix = f"{key}="
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = f"{prefix}{value}"
            return "\n".join(lines) + "\n"
    raise SystemExit(f"{key} is missing from the environment template")


def write_env(
    example: Path,
    output: Path,
    *,
    domain: str,
    idp_domain: str,
    email: str,
    force: bool = False,
) -> Path:
    if output.exists() and not force:
        raise FileExistsError(f"{output} already exists; pass --force to replace it")
    text = example.read_text()
    for line in text.splitlines():
        if SECRET_MARKER in line and "=" in line:
            key = line.split("=", 1)[0]
            text = _set(text, key, secrets.token_hex(32))
    text = _set(text, "CYGNUSLOOM_DOMAIN", domain)
    text = _set(text, "AUTHENTIK_DOMAIN", idp_domain)
    text = _set(text, "ACME_EMAIL", email)
    text = _set(text, "AUTHENTIK_BOOTSTRAP_EMAIL", email)
    output.write_text(text)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", required=True, help="public hostname of the app")
    parser.add_argument("--idp-domain", help="public hostname of Authentik")
    parser.add_argument("--email", default="admin@example.com", help="admin and ACME email")
    parser.add_argument("--example", type=Path, default=Path(".env.example"))
    parser.add_argument("--output", type=Path, default=Path(".env"))
    parser.add_argument("--force", action="store_true", help="replace an existing output file")
    args = parser.parse_args(argv)
    idp_domain = args.idp_domain or f"idp.{args.domain}"
    try:
        write_env(
            args.example,
            args.output,
            domain=args.domain,
            idp_domain=idp_domain,
            email=args.email,
            force=args.force,
        )
    except FileExistsError as exc:
        print(exc)
        return 1
    print(f"wrote {args.output}")
    print("Next: docker compose up -d --build")
    print("The initial Authentik admin password is in the new .env file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
