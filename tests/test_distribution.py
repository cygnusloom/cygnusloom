from __future__ import annotations

import shutil
import subprocess
import tarfile
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required to build distributions")
def test_distributions_exclude_untracked_runtime_and_private_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    for name in ("pyproject.toml", ".gitignore"):
        shutil.copy2(ROOT / name, project / name)
    for name in (
        "README.md",
        "CREDITS.md",
        "LICENSE",
        ".env.example",
        "src/cygnusloom/__init__.py",
        "src/cygnusloom/asset.txt",
        "tests/test_example.py",
    ):
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")
    excluded = (
        "runtime.log",
        "private-notes.txt",
        ".env",
        ".env.local",
        ".copilot_testing",
        "session.db",
        "session.db-journal",
        "src/cygnusloom/runtime.log",
        "src/cygnusloom/runtime/deep.log",
        "src/cygnusloom/.env",
        "src/cygnusloom/.env.local",
        "src/cygnusloom/private.env",
        "src/cygnusloom/session.db",
        "src/cygnusloom/session.db-wal",
        "src/cygnusloom/session.db-journal",
        "src/cygnusloom/session.sqlite3",
        "src/cygnusloom/session.sqlite3-wal",
    )
    for name in excluded:
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private fixture\n", encoding="utf-8")

    dist = tmp_path / "dist"
    for target in ("sdist", "wheel"):
        result = subprocess.run(
            ["uv", "build", "--offline", f"--{target}", "--out-dir", str(dist)],
            cwd=project,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
    (archive,) = dist.glob("*.tar.gz")
    with tarfile.open(archive, "r:gz") as source:
        paths = {PurePosixPath(*PurePosixPath(name).parts[1:]) for name in source.getnames()}

    assert {
        PurePosixPath("README.md"),
        PurePosixPath("CREDITS.md"),
        PurePosixPath(".env.example"),
        PurePosixPath("src/cygnusloom/__init__.py"),
        PurePosixPath("src/cygnusloom/asset.txt"),
        PurePosixPath("tests/test_example.py"),
    } <= paths
    assert not paths.intersection(PurePosixPath(name) for name in excluded)

    (wheel,) = dist.glob("*.whl")
    with ZipFile(wheel) as binary:
        wheel_paths = {PurePosixPath(name) for name in binary.namelist()}
    assert {
        PurePosixPath("cygnusloom/__init__.py"),
        PurePosixPath("cygnusloom/asset.txt"),
        PurePosixPath("cygnusloom/CREDITS.md"),
    } <= wheel_paths
    wheel_excluded = {
        PurePosixPath(name).relative_to("src") for name in excluded if name.startswith("src/")
    }
    assert not wheel_paths.intersection(wheel_excluded)
