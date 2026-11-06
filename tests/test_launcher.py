from __future__ import annotations

import sys
from pathlib import Path

import pytest

from cygnusloom import cli, launcher


def test_data_dir_uses_an_explicit_override(tmp_path):
    assert launcher.data_dir(str(tmp_path)) == tmp_path.resolve()


def test_data_dir_uses_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CYGNUSLOOM_DATA_DIR", str(tmp_path))
    assert launcher.data_dir() == tmp_path.resolve()


def test_database_url_round_trips_the_path(tmp_path):
    url = launcher.database_url(tmp_path)
    assert url == f"sqlite:///{(tmp_path / 'cygnusloom.db').as_posix()}"
    assert Path(url.removeprefix("sqlite:///")) == tmp_path / "cygnusloom.db"


def test_secret_key_is_created_once_with_owner_only_permissions(tmp_path):
    first = launcher._secret_key(tmp_path)
    second = launcher._secret_key(tmp_path)

    assert first == second
    assert len(first) == 64
    assert (tmp_path / "secret.key").stat().st_mode & 0o777 == 0o600


def test_one_process_holds_the_instance_lock(tmp_path):
    first = launcher._InstanceLock(tmp_path / "instance.lock")
    second = launcher._InstanceLock(tmp_path / "instance.lock")

    assert first.acquire() is True
    try:
        assert second.acquire() is False
    finally:
        first.release()
    assert second.acquire() is True
    second.release()


def test_free_port_is_usable():
    port = launcher._free_port("127.0.0.1")
    assert 1 <= port <= 65535


def test_library_symbol_probe_reads_the_file(tmp_path):
    library = tmp_path / "libstdc++.so.6"
    library.write_bytes(b"...GLIBCXX_3.4.29...")

    assert launcher._library_has_symbol(library, b"GLIBCXX_3.4.29") is True
    assert launcher._library_has_symbol(library, b"GLIBCXX_3.4.99") is False
    # An unreadable library must not block startup on an unusual system.
    assert launcher._library_has_symbol(tmp_path / "missing", b"GLIBCXX_3.4.29") is True


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux runtime check")
def test_the_host_runtime_satisfies_the_desktop_requirement():
    assert launcher._linux_runtime_problem() == ""


def test_the_desktop_spec_uses_the_host_system_libraries():
    spec = Path(__file__).resolve().parents[1] / "packaging" / "cygnusloom.spec"
    text = spec.read_text(encoding="utf-8")

    assert "libstdc++.so" in text
    assert "libgcc_s.so" in text
    assert "libfontconfig.so" in text
    assert "a.binaries = [" in text


def test_the_desktop_edition_does_not_inherit_models_from_the_shell():
    env = {
        "CYGNUSLOOM_LLM_PROVIDERS": "fake/scripted",
        "CYGNUSLOOM_LLM_DEFAULT_MODEL": "fake/scripted",
        "CYGNUSLOOM_LLM_DEEPSEEK_API_KEY": "secret",
        "CYGNUSLOOM_LLM_DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1",
        "CYGNUSLOOM_LLM_CONTEXT_TOKENS": "8192",
        "UNRELATED": "kept",
    }

    launcher._ignore_model_environment(env)

    assert env == {"CYGNUSLOOM_LLM_CONTEXT_TOKENS": "8192", "UNRELATED": "kept"}


def test_migrations_are_inside_the_package():
    config = cli.migration_config("sqlite:///unused.db")
    location = Path(config.get_main_option("script_location") or "")
    assert location.is_dir()
    assert (location / "versions" / "0001_initial.py").is_file()


def test_where_prints_the_data_folder(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CYGNUSLOOM_DATA_DIR", str(tmp_path))

    assert cli.main(["where"]) == 0

    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0] == str(tmp_path.resolve())
    assert lines[1] == launcher.database_url(tmp_path.resolve())
