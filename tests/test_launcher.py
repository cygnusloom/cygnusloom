from __future__ import annotations

import sys
from pathlib import Path

import pytest

from cygnusloom import cli, desktop, launcher


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


class _FakeSettings:
    def __init__(self, values: dict | None = None):
        self.values = dict(values or {})
        self.synced = False

    def value(self, key, default=None, type=None):
        return self.values.get(key, default)

    def setValue(self, key, value):
        self.values[key] = value

    def sync(self):
        self.synced = True


class _FakeWindow:
    def __init__(self):
        self.geometry = None
        self.resized = None
        self.minimum = None
        self.mode = None

    def restoreGeometry(self, geometry):
        self.geometry = geometry
        return True

    def resize(self, width, height):
        self.resized = (width, height)

    def setMinimumSize(self, width, height):
        self.minimum = (width, height)

    def show(self):
        self.mode = "normal"

    def showMaximized(self):
        self.mode = "maximized"

    def showFullScreen(self):
        self.mode = "fullscreen"

    def saveGeometry(self):
        return b"saved geometry"

    def isMaximized(self):
        return self.mode == "maximized"

    def isFullScreen(self):
        return self.mode == "fullscreen"


def test_window_state_file_lives_in_the_data_dir(tmp_path):
    assert desktop.window_state_path(tmp_path) == tmp_path / "window.ini"


def test_the_desktop_window_icon_is_shipped():
    icon = desktop.brand_icon_path()

    assert icon.is_file()
    assert icon.name == "cygnus-loom-256.png"


def test_a_first_launch_uses_the_default_window_size():
    window = _FakeWindow()

    desktop.restore_window_state(_FakeSettings(), window)

    assert window.resized == (desktop.WINDOW_WIDTH, desktop.WINDOW_HEIGHT)
    assert window.minimum == (desktop.MIN_WIDTH, desktop.MIN_HEIGHT)
    assert window.mode == "normal"


def test_the_window_remembers_its_geometry_and_maximized_state():
    settings = _FakeSettings({"geometry": b"old geometry", "maximized": True})
    window = _FakeWindow()

    desktop.restore_window_state(settings, window)

    assert window.geometry == b"old geometry"
    assert window.mode == "maximized"

    desktop.save_window_state(settings, window)
    assert settings.values == {
        "geometry": b"saved geometry",
        "maximized": True,
        "fullscreen": False,
    }
    assert settings.synced is True


def test_a_full_screen_window_is_restored_full_screen():
    window = _FakeWindow()

    desktop.restore_window_state(
        _FakeSettings({"geometry": b"old geometry", "fullscreen": True}), window
    )

    assert window.mode == "fullscreen"


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


def test_the_desktop_spec_uses_the_brand_icon():
    spec = Path(__file__).resolve().parents[1] / "packaging" / "cygnusloom.spec"
    text = spec.read_text(encoding="utf-8")

    assert "cygnus-loom.ico" in text
    assert "icon=str(EXE_ICON)" in text
    assert desktop.brand_icon_path().parent == (
        Path(__file__).resolve().parents[1] / "src" / "cygnusloom" / "web" / "static" / "brand"
    )


def test_the_desktop_edition_does_not_inherit_models_from_the_shell():
    env = {
        "CYGNUSLOOM_LLM_PROVIDERS": "fake/scripted",
        "CYGNUSLOOM_LLM_DEFAULT_MODEL": "fake/scripted",
        "CYGNUSLOOM_LLM_DEEPSEEK_API_KEY": "secret",
        "CYGNUSLOOM_LLM_DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1",
        "UNRELATED": "kept",
    }

    launcher._ignore_model_environment(env)

    assert env == {"UNRELATED": "kept"}


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
