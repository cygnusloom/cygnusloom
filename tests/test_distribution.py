from __future__ import annotations

import shutil
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required to build the source archive")
def test_sdist_excludes_untracked_runtime_and_private_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    for name in ("pyproject.toml", ".gitignore"):
        shutil.copy2(ROOT / name, project / name)
    for name in (
        "README.md",
        ".env.example",
        "src/cygnusforge/__init__.py",
        "tests/test_example.py",
    ):
        path = project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")
    for name in (
        "runtime.log",
        "private-notes.txt",
        ".env",
        ".env.local",
        ".copilot_testing",
        "session.db",
        "session.db-journal",
        "src/cygnusforge/runtime.log",
        "src/cygnusforge/.env.local",
        "src/cygnusforge/session.db-journal",
        "src/cygnusforge/session.sqlite3",
    ):
        (project / name).write_text("private fixture\n", encoding="utf-8")

    dist = tmp_path / "dist"
    result = subprocess.run(
        ["uv", "build", "--offline", "--sdist", "--out-dir", str(dist)],
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
        PurePosixPath(".env.example"),
        PurePosixPath("src/cygnusforge/__init__.py"),
        PurePosixPath("tests/test_example.py"),
    } <= paths
    assert not paths.intersection(
        {
            PurePosixPath("runtime.log"),
            PurePosixPath("private-notes.txt"),
            PurePosixPath(".env"),
            PurePosixPath(".env.local"),
            PurePosixPath(".copilot_testing"),
            PurePosixPath("session.db"),
            PurePosixPath("session.db-journal"),
            PurePosixPath("src/cygnusforge/runtime.log"),
            PurePosixPath("src/cygnusforge/.env.local"),
            PurePosixPath("src/cygnusforge/session.db-journal"),
            PurePosixPath("src/cygnusforge/session.sqlite3"),
        }
    )
