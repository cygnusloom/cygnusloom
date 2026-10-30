from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ("install.sh", "update.sh", "backup.sh")


pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")


def test_deploy_scripts_parse() -> None:
    for name in SCRIPTS:
        subprocess.run(
            ["bash", "-n", str(ROOT / "scripts" / name)],
            check=True,
            capture_output=True,
            text=True,
        )


def test_deploy_scripts_have_help() -> None:
    for name in ("install.sh", "update.sh"):
        result = subprocess.run(
            [str(ROOT / "scripts" / name), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "Usage:" in result.stdout


def test_compose_publishes_only_loopback_ports() -> None:
    compose = (ROOT / "compose.yaml").read_text()
    env_example = (ROOT / ".env.example").read_text()
    assert "caddy" not in compose.lower()
    assert "127.0.0.1:${CYGNUSFORGE_APP_PORT:-21872}:8000" in compose
    assert "127.0.0.1:${AUTHENTIK_PORT:-21873}:9000" in compose
    assert "CYGNUSFORGE_APP_PORT=21872" in env_example
    assert "AUTHENTIK_PORT=21873" in env_example


def test_nginx_sample_proxies_to_the_stack() -> None:
    config = (ROOT / "deploy" / "nginx" / "cygnusforge.conf").read_text()
    assert "proxy_pass http://127.0.0.1:21872;" in config
    assert "proxy_pass http://127.0.0.1:21873;" in config
    assert "proxy_buffering off;" in config


def test_local_mode_makes_the_idp_reachable_from_the_app() -> None:
    local = (ROOT / "compose.local.yaml").read_text()
    assert "ports: !override" in local
    assert "0.0.0.0:${AUTHENTIK_PORT:-21873}:9000" in local


def test_entrypoint_waits_for_database_dns() -> None:
    entrypoint = ROOT / "deploy" / "entrypoint.sh"
    subprocess.run(["sh", "-n", str(entrypoint)], check=True, capture_output=True, text=True)
    assert "retrying migrations" in entrypoint.read_text()
