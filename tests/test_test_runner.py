"""The project's pytest runner: worker sizing, fallbacks, and a run that cannot hang."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import sys
import time
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parent.parent / "scripts" / "test.py"
GIB = 1024 * 1024 * 1024


def _runner():
    spec = importlib.util.spec_from_file_location("cygnusloom_test_runner", RUNNER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runner():
    return _runner()


def _sizing(monkeypatch, runner, *, cores: int, busy: float | None, memory: dict[str, int]):
    monkeypatch.setattr(runner, "cpu_count", lambda: cores)
    monkeypatch.setattr(runner, "busy_cpu_count", lambda: busy)
    monkeypatch.setattr(runner, "load_average", lambda: 0.0)
    monkeypatch.setattr(runner, "meminfo", lambda: memory)
    return runner.resource_limits


def test_a_busy_machine_still_gets_at_least_two_workers(runner, monkeypatch):
    limits = _sizing(monkeypatch, runner, cores=12, busy=None, memory={})
    monkeypatch.setattr(runner, "load_average", lambda: 100.0)
    assert limits() == (2, None)


def test_one_core_is_never_doubled(runner, monkeypatch):
    limits = _sizing(monkeypatch, runner, cores=1, busy=0.0, memory={})
    assert limits() == (1, None)


def test_an_idle_machine_uses_half_its_cores(runner, monkeypatch):
    limits = _sizing(monkeypatch, runner, cores=12, busy=0.0, memory={})
    assert limits() == (6, None)


def test_low_free_memory_reduces_the_worker_pool(runner, monkeypatch):
    limits = _sizing(
        monkeypatch,
        runner,
        cores=12,
        busy=0.0,
        memory={"MemTotal": 16 * GIB, "MemAvailable": 2 * GIB},
    )
    workers, ceiling = limits()
    usable = 2 * GIB - runner.MEMORY_FOR_CONTROLLER
    assert workers == max(1, usable // runner.MEMORY_PER_WORKER)
    assert ceiling == usable


def test_a_tiny_machine_still_runs_one_worker(runner, monkeypatch):
    limits = _sizing(
        monkeypatch,
        runner,
        cores=12,
        busy=0.0,
        memory={"MemTotal": 4 * GIB, "MemAvailable": 100 * 1024 * 1024},
    )
    assert limits() == (1, runner.MEMORY_PER_WORKER)


def test_an_explicit_worker_count_is_kept(runner, monkeypatch):
    limits = _sizing(monkeypatch, runner, cores=12, busy=0.0, memory={})
    assert limits(["-n3"]) == (3, None)
    memory = {"MemTotal": 16 * GIB, "MemAvailable": 4 * GIB}
    limits = _sizing(monkeypatch, runner, cores=12, busy=0.0, memory=memory)
    assert limits(["--numprocesses=8"])[0] == 8


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ([], None),
        (["-n", "4"], 4),
        (["-n4"], 4),
        (["-n=5"], 5),
        (["--numprocesses", "3"], 3),
        (["--numprocesses=7"], 7),
        (["-nauto"], -1),
        (["--numprocesses", "logical"], -1),
        (["-n0"], 0),
    ],
)
def test_every_worker_spelling_is_understood(runner, arguments, expected):
    assert runner.explicit_workers(arguments) == expected
    assert runner.has_workers(arguments) is (expected is not None)


def test_the_runner_falls_back_to_serial_without_xdist(runner, monkeypatch):
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, package=None: None if name == "xdist" else real_find_spec(name, package),
    )
    assert runner.xdist_distribution() is None


@pytest.mark.parametrize(
    ("version", "expected"),
    [("3.8.0", "worksteal"), ("2.2.0", "worksteal"), ("2.1.0", "load"), ("1.9.0", "load")],
)
def test_an_old_xdist_gets_a_distribution_it_knows(runner, monkeypatch, version, expected):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, package=None: object())
    monkeypatch.setattr(importlib.metadata, "version", lambda name: version)
    assert runner.xdist_distribution() == expected


def test_an_existing_cgroup_limit_is_verified(runner, tmp_path):
    limit = tmp_path / "cpu.max"
    assert runner._set_cgroup_limit(limit, "200000 100000") is True
    assert limit.read_text(encoding="utf-8") == "200000 100000"
    assert runner._set_cgroup_limit(tmp_path / "missing" / "cpu.max", "1") is False


def test_stale_cgroups_are_cleaned_but_live_ones_are_kept(runner, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "CGROUP_ROOT", tmp_path)
    stale = tmp_path / "cygnusloom-tests-1"
    stale.mkdir()
    (stale / "cgroup.procs").write_text("", encoding="utf-8")
    live = tmp_path / "cygnusloom-tests-2"
    live.mkdir()
    (live / "cgroup.procs").write_text("123\n", encoding="utf-8")
    removed = []
    monkeypatch.setattr(runner, "_remove_cgroup", removed.append)

    runner.clean_stale_cgroups()

    assert removed == [stale]


def test_a_hung_run_is_stopped_with_its_workers(runner, tmp_path):
    started = time.monotonic()
    code = runner.run_pytest(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        tmp_path,
        {},
        0.5,
    )
    assert code == 124
    assert time.monotonic() - started < 10


@pytest.mark.parametrize(("raw", "expected"), [(None, 1800), ("", 1800), ("0", 0), ("90", 90)])
def test_the_timeout_can_be_configured(runner, monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(runner.TIMEOUT_ENV, raising=False)
    else:
        monkeypatch.setenv(runner.TIMEOUT_ENV, raw)
    assert runner.run_timeout() == expected
