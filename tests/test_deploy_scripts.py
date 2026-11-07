from __future__ import annotations

import shutil
import subprocess
import tomllib
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
    assert "127.0.0.1:${CYGNUSLOOM_APP_PORT:-21872}:8000" in compose
    assert "127.0.0.1:${AUTHENTIK_PORT:-21873}:9000" in compose
    assert "CYGNUSLOOM_APP_PORT=21872" in env_example
    assert "AUTHENTIK_PORT=21873" in env_example


def test_nginx_sample_proxies_to_the_stack() -> None:
    config = (ROOT / "deploy" / "nginx" / "cygnusloom.conf").read_text()
    assert "proxy_pass http://127.0.0.1:21872;" in config
    assert "proxy_pass http://127.0.0.1:21873;" in config
    assert "proxy_buffering off;" in config


def test_local_mode_makes_the_idp_reachable_from_the_app() -> None:
    local = (ROOT / "compose.local.yaml").read_text()
    assert "ports: !override" in local
    assert "0.0.0.0:${AUTHENTIK_PORT:-21873}:9000" in local


def test_authentik_application_has_a_launch_url() -> None:
    blueprint = (ROOT / "deploy" / "authentik" / "blueprints" / "cygnusloom.yaml").read_text()
    assert "meta_launch_url: !Env CYGNUSLOOM_BASE_URL" in blueprint


def test_the_blueprint_creates_the_fixed_credit_exempt_group() -> None:
    blueprint = (ROOT / "deploy" / "authentik" / "blueprints" / "cygnusloom.yaml").read_text()
    assert "id: cygnusloom-credit-exempt" in blueprint
    assert "name: cygnusloom-credit-exempt" in blueprint
    entry = blueprint.split("id: cygnusloom-credit-exempt", 1)[1].split("- model:", 1)[0]
    assert "!Env" not in entry


def test_the_blueprint_creates_an_invite_only_enrollment_flow() -> None:
    blueprint = (ROOT / "deploy" / "authentik" / "blueprints" / "cygnusloom.yaml").read_text()
    assert "id: cygnusloom-players" in blueprint
    assert "name: cygnusloom-players" in blueprint
    entry = blueprint.split("id: cygnusloom-players", 1)[1].split("- model:", 1)[0]
    assert "!Env" not in entry
    assert "id: cygnusloom-enrollment-invite" in blueprint
    assert "slug: cygnusloom-enrollment-invite" in blueprint
    assert "designation: enrollment" in blueprint
    assert "authentication: require_unauthenticated" in blueprint
    assert "authentik_stages_invitation.invitationstage" in blueprint
    assert "continue_flow_without_invitation: false" in blueprint
    assert "create_users_group: !KeyOf cygnusloom-players" in blueprint
    assert (
        "target: !KeyOf cygnusloom-enrollment-invite\n"
        "      stage: !KeyOf cygnusloom-enrollment-invitation\n"
        "      order: 5"
    ) in blueprint
    assert (
        "target: !KeyOf cygnusloom-enrollment-invite\n"
        "      stage: !KeyOf cygnusloom-enrollment-user-write\n"
        "      order: 20"
    ) in blueprint


def test_update_script_applies_the_blueprint_explicitly() -> None:
    update = (ROOT / "scripts" / "update.sh").read_text()
    applied = (
        "docker compose exec -T authentik-server ak apply_blueprint "
        "/blueprints/custom/cygnusloom.yaml"
    )
    assert applied in update
    assert update.index("docker compose up -d --build --remove-orphans") < update.index(applied)


def test_entrypoint_waits_for_database_dns() -> None:
    entrypoint = ROOT / "deploy" / "entrypoint.sh"
    subprocess.run(["sh", "-n", str(entrypoint)], check=True, capture_output=True, text=True)
    assert "retrying migrations" in entrypoint.read_text()


def test_dockerfile_copies_the_files_the_wheel_build_needs() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
    needed = {Path(source).name for source in wheel["force-include"]}
    needed.update(Path(name).name for name in pyproject["project"]["license-files"])

    lines = (ROOT / "Dockerfile").read_text().splitlines()
    install = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("RUN uv sync") and "--no-install-project" not in line
    )
    copied = {
        Path(token).name
        for line in lines[:install]
        if line.startswith("COPY ")
        for token in line.split()[1:-1]
        if not token.startswith("--")
    }
    assert needed <= copied
    # The image must import the wheel, where CREDITS.md is force-included, not the source tree.
    assert "--no-editable" in lines[install]
