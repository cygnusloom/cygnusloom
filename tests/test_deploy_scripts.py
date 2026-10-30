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
