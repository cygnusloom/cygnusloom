from __future__ import annotations

from pathlib import Path

import pytest

from scripts.setup_env import write_env

ROOT = Path(__file__).resolve().parents[1]


def test_setup_env_generates_secrets_and_sets_names(tmp_path):
    output = tmp_path / ".env"
    write_env(
        ROOT / ".env.example",
        output,
        domain="cygnusloom.example.com",
        idp_domain="idp.example.com",
        email="me@example.com",
    )
    text = output.read_text()
    assert "=change-me" not in text
    assert "CYGNUSLOOM_DOMAIN=cygnusloom.example.com" in text
    assert "AUTHENTIK_DOMAIN=idp.example.com" in text
    assert "ACME_EMAIL=me@example.com" in text
    assert "AUTHENTIK_BOOTSTRAP_EMAIL=me@example.com" in text

    secret_keys = [
        "CYGNUSLOOM_SECRET_KEY",
        "POSTGRES_PASSWORD",
        "AUTHENTIK_SECRET_KEY",
        "AUTHENTIK_POSTGRES_PASSWORD",
        "CYGNUSLOOM_OIDC_CLIENT_SECRET",
        "AUTHENTIK_BOOTSTRAP_PASSWORD",
    ]
    values = [
        next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith(f"{key}="))
        for key in secret_keys
    ]
    assert all(len(value) >= 32 for value in values)
    assert len(set(values)) == len(values)


def test_setup_env_does_not_overwrite_without_force(tmp_path):
    output = tmp_path / ".env"
    output.write_text("keep me")
    with pytest.raises(FileExistsError):
        write_env(
            ROOT / ".env.example",
            output,
            domain="cygnusloom.example.com",
            idp_domain="idp.example.com",
            email="me@example.com",
        )
    assert output.read_text() == "keep me"
