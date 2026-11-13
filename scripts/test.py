#!/usr/bin/env python3
"""Run the test suite with a share of this machine's resources.

The suite is written for pytest-xdist. This runner sizes the worker pool from the CPU and
memory that are free right now and, where the platform allows it, enforces a hard limit for
the whole run. It never takes more than half the machine's CPUs or RAM (swap excluded), and
it leaves room for whatever else is running when the machine is busy.

The runner sets ``CYGNUSLOOM_TEST_SETTINGS=1``. ``tests/conftest.py`` refuses a bare pytest
run without that marker and points at the instructions instead. CI sets the marker itself and
runs pytest directly, because a CI runner is dedicated to the job and needs no limits.

Usage::

    uv run python scripts/test.py [pytest arguments...]

Any arguments are passed to pytest unchanged. An explicit ``-n``/``--numprocesses`` is kept;
the memory and CPU ceilings still apply. Set ``CYGNUSLOOM_TEST_TIMEOUT`` to a number of
seconds to change the wall-clock limit for the whole run (0 disables it); a run that hangs is
stopped and reported instead of waiting forever.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

SETTINGS_ENV = "CYGNUSLOOM_TEST_SETTINGS"
TIMEOUT_ENV = "CYGNUSLOOM_TEST_TIMEOUT"
CGROUP_ROOT = Path("/sys/fs/cgroup")
# A worker's peak memory was under 400 MiB in a full suite run. Round up so the memory ceiling
# never limits the worker pool before the CPU share does.
MEMORY_PER_WORKER = 512 * 1024 * 1024
# The pytest controller is small, but it needs its own share while the workers run.
MEMORY_FOR_CONTROLLER = 256 * 1024 * 1024
# Keep at least this many workers when the machine can hold them. A single worker makes a
# full run several times slower, which is exactly the pathological case this runner must not
# fall into; two still leave room for other work.
MIN_WORKERS = 2
CPU_SHARE = 2  # at most half the CPUs
MEMORY_SHARE = 2  # at most half the RAM, never counting swap
DEFAULT_TIMEOUT = 30 * 60  # a hung run is stopped after half an hour
CPU_SAMPLE_SECONDS = 0.1
CGROUP_WRITE_ATTEMPTS = 5


def cpu_count() -> int:
    """The CPUs this process may use, respecting a container's CPU quota."""
    count = os.cpu_count() or 1
    try:
        quota, period = (CGROUP_ROOT / "cpu.max").read_text(encoding="utf-8").split()
        if quota != "max":
            count = min(count, max(1, int(quota) // int(period)))
    except (OSError, ValueError, ZeroDivisionError):
        pass
    return count


def load_average() -> float:
    try:
        return os.getloadavg()[0]
    except (AttributeError, OSError):
        return 0.0


def _cpu_times() -> tuple[int, int] | None:
    """The busy and total CPU jiffies on the machine, or None when unavailable."""
    try:
        line = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return None
    fields = line.split()
    if not fields or fields[0] != "cpu":
        return None
    try:
        values = [int(field) for field in fields[1:]]
    except ValueError:
        return None
    if len(values) < 4:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values) - idle, sum(values)


def busy_cpu_count() -> float | None:
    """How many CPUs other work is using right now, sampled over a short interval.

    The one-minute load average keeps counting a run that has already finished, so a second
    test run in the same minute would collapse to one worker even on an idle machine. A short
    sample measures what is running now instead.
    """
    first = _cpu_times()
    if first is None:
        return None
    time.sleep(CPU_SAMPLE_SECONDS)
    second = _cpu_times()
    if second is None:
        return None
    total = second[1] - first[1]
    if total <= 0:
        return None
    busy = max(second[0] - first[0], 0)
    return busy / total * (os.cpu_count() or 1)


def meminfo() -> dict[str, int]:
    """``MemTotal`` and ``MemAvailable`` in bytes, when the platform reports them. A container's
    memory limit, when it has one, replaces a larger host total."""
    values: dict[str, int] = {}
    try:
        lines = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        name, _, rest = line.partition(":")
        words = rest.split()
        if words:
            values[name] = int(words[0]) * 1024
    try:
        limit = (CGROUP_ROOT / "memory.max").read_text(encoding="utf-8").strip()
    except OSError:
        return values
    if limit != "max":
        try:
            ceiling = int(limit)
        except ValueError:
            return values
        if "MemTotal" in values:
            values["MemTotal"] = min(values["MemTotal"], ceiling)
        if "MemAvailable" in values:
            values["MemAvailable"] = min(values["MemAvailable"], ceiling)
    return values


def explicit_workers(args: list[str]) -> int | None:
    """The worker count a ``-n``/``--numprocesses`` argument names, or None when it names none.

    ``0`` is returned as ``0`` and the ``auto`` forms as ``-1``: pytest-xdist decides those
    itself, so the runner leaves the argument alone and uses its own CPU budget for the
    ceilings.
    """
    value: str | None = None
    for index, argument in enumerate(args):
        if argument in {"-n", "--numprocesses"} and index + 1 < len(args):
            value = args[index + 1]
        elif argument.startswith("--numprocesses="):
            value = argument.split("=", 1)[1]
        elif argument.startswith("-n") and len(argument) > 2:
            value = argument[2:].lstrip("=")
    if value is None:
        return None
    value = value.strip().lower()
    if value in {"auto", "logical", "physical"}:
        return -1
    try:
        return max(0, int(value))
    except ValueError:
        return -1


def has_workers(args: list[str]) -> bool:
    return explicit_workers(args) is not None


def has_distribution(args: list[str]) -> bool:
    return any(a == "--dist" or a.startswith("--dist=") for a in args)


def xdist_distribution() -> str | None:
    """The distribution to ask pytest-xdist for, or None when xdist cannot run the suite.

    Running serially is slower, but it is much better than failing to start because the
    installed environment lacks the plugin. `worksteal` needs xdist 2.2 or newer; an older
    plugin gets plain `load`, which every xdist version understands.
    """
    if importlib.util.find_spec("xdist") is None:
        return None
    try:
        version = importlib.metadata.version("pytest-xdist")
    except importlib.metadata.PackageNotFoundError:
        return "load"
    match = re.match(r"(\d+)\.(\d+)", version)
    if match is None:
        return "load"
    major, minor = int(match.group(1)), int(match.group(2))
    if (major, minor) >= (2, 2):
        return "worksteal"
    return "load"


def resource_limits(args: list[str] | None = None) -> tuple[int, int | None]:
    """The number of workers and a memory ceiling in bytes, or None when it is unknown.

    The CPU budget is half the logical CPUs, less whatever the machine is already using.
    The memory budget is half the RAM, or what is available after a small reserve for the
    system and other work, whichever is smaller. An explicit ``-n`` is kept, even when the
    memory ceiling is low, because the caller asked for it; the ceiling still applies.
    """
    args = list(args or [])
    cores = cpu_count()
    half_cores = max(1, cores // CPU_SHARE)
    busy = busy_cpu_count()
    if busy is None:
        free_cores = max(1, int(cores - load_average()))
    else:
        free_cores = max(1, round(cores - busy))
    workers = min(half_cores, free_cores)

    memory = meminfo()
    total = memory.get("MemTotal", 0)
    requested = explicit_workers(args)
    if not total:
        if requested is not None and requested > 0:
            return requested, None
        return max(min(MIN_WORKERS, half_cores), workers), None

    half = total // MEMORY_SHARE
    available = memory.get("MemAvailable", total)
    # MemAvailable already excludes everything else that is running; the reserve only needs
    # to hold the pytest controller and ordinary kernel caches, not a second helping of the
    # memory other work is using.
    reserve = max(MEMORY_FOR_CONTROLLER, total // 64)
    usable = max(available - reserve, 0)
    ceiling = min(half, max(usable, MEMORY_PER_WORKER))
    if requested is not None and requested > 0:
        return requested, ceiling
    memory_workers = max(1, usable // MEMORY_PER_WORKER)
    workers = max(min(MIN_WORKERS, half_cores), workers)
    return max(1, min(workers, memory_workers)), ceiling


def _set_cgroup_limit(path: Path, value: str) -> bool:
    """Write one limit and verify it took effect, retrying a transient refusal.

    Creating a cgroup and enabling its controllers is asynchronous on some kernels; the first
    write to ``cpu.max`` can fail with EINVAL even though the same write succeeds a moment
    later. Ignoring that would silently run without the ceiling the runner promised.
    """
    for attempt in range(CGROUP_WRITE_ATTEMPTS):
        try:
            path.write_text(value, encoding="utf-8")
        except OSError:
            if attempt + 1 < CGROUP_WRITE_ATTEMPTS:
                time.sleep(0.02)
                continue
            return False
        try:
            return path.read_text(encoding="utf-8").strip() == value
        except OSError:
            return False
    return False


def _remove_cgroup(cgroup: Path) -> None:
    """Remove one cgroup, ignoring a group that is still in use or already gone."""
    try:
        cgroup.rmdir()
    except OSError:
        pass


def clean_stale_cgroups() -> None:
    """Remove empty cgroups left behind by a run that was killed before it could clean up."""
    if not CGROUP_ROOT.is_dir():
        return
    for cgroup in CGROUP_ROOT.glob("cygnusloom-tests-*"):
        try:
            if (cgroup / "cgroup.procs").read_text(encoding="utf-8").strip():
                continue
        except OSError:
            continue
        _remove_cgroup(cgroup)


def enter_cgroup(memory_ceiling: int | None, workers: int, cores: int) -> Path | None:
    """Make a cgroup with the CPU and memory ceilings, when the platform allows it.

    The pytest process is moved into it just before it starts, so this runner stays outside
    and can remove the group when the run ends. Returns None when this platform or account
    cannot make one, in which case the worker count alone keeps the run within its share.
    """
    if not CGROUP_ROOT.is_dir():
        return None
    cgroup = CGROUP_ROOT / f"cygnusloom-tests-{os.getpid()}"
    try:
        cgroup.mkdir()
    except OSError:
        return None
    limited = False
    if memory_ceiling is not None:
        limited = _set_cgroup_limit(cgroup / "memory.max", str(memory_ceiling))
    # cpu.max takes a quota and a period, both in microseconds: one CPU per 100000. The
    # controller needs a little CPU of its own, but the promise is at most half the machine.
    quota_cores = min(workers + 1, max(1, cores // CPU_SHARE), max(workers, 1))
    if _set_cgroup_limit(cgroup / "cpu.max", f"{quota_cores * 100_000} 100_000"):
        limited = True
    if not limited:
        _remove_cgroup(cgroup)
        return None
    return cgroup


def leave_cgroup(cgroup: Path | None) -> None:
    """Remove the cgroup once the process it held has ended."""
    if cgroup is None:
        return
    _remove_cgroup(cgroup)


def pytest_command(args: list[str], cgroup: Path | None) -> list[str]:
    """The pytest command, wrapped to enter the cgroup before exec when there is one."""
    if cgroup is None:
        return [sys.executable, "-m", "pytest", *args]
    launcher = (
        "import os, sys\n"
        "try:\n"
        "    with open(sys.argv[1], 'w') as procs:\n"
        "        procs.write(str(os.getpid()))\n"
        "except OSError as error:\n"
        "    print(f'warning: the test cgroup is unavailable: {error}', file=sys.stderr)\n"
        "os.execv(sys.executable, [sys.executable, '-m', 'pytest', *sys.argv[2:]])\n"
    )
    return [sys.executable, "-c", launcher, str(cgroup / "cgroup.procs"), *args]


def run_timeout() -> int:
    """The wall-clock limit for the whole run, in seconds; 0 disables it."""
    value = os.environ.get(TIMEOUT_ENV)
    if value is None or not value.strip():
        return DEFAULT_TIMEOUT
    try:
        return max(0, int(value))
    except ValueError:
        print(f"warning: ignoring {TIMEOUT_ENV}={value!r}; using {DEFAULT_TIMEOUT} seconds")
        return DEFAULT_TIMEOUT


def run_pytest(command: list[str], root: Path, env: dict[str, str], timeout: int) -> int:
    """Run pytest in its own session so a stuck run can be stopped with all its workers.

    A worker that hangs would otherwise keep the whole run alive forever, which is the other
    pathological case the runner has to survive. The child gets its own process group so a
    timeout or Ctrl-C reaches pytest and its workers, not this runner's process group.
    """
    process = subprocess.Popen(command, cwd=root, env=env, start_new_session=True)
    try:
        return process.wait(timeout=timeout or None)
    except subprocess.TimeoutExpired:
        print(
            f"pytest: stopping the run after {timeout} seconds; "
            f"set {TIMEOUT_ENV}=0 to disable the limit",
            file=sys.stderr,
            flush=True,
        )
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        process.wait()
        return 124
    except KeyboardInterrupt:
        try:
            os.killpg(process.pid, signal.SIGINT)
        except OSError:
            pass
        try:
            return process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            process.wait()
            return 130


def main() -> int:
    args = sys.argv[1:]
    if importlib.util.find_spec("pytest") is None:
        print(
            "pytest: this interpreter has no pytest; run `uv run python scripts/test.py` "
            "or use the project's .venv.",
            file=sys.stderr,
        )
        return 1

    clean_stale_cgroups()
    cores = cpu_count()
    distribution = xdist_distribution()
    workers, memory_ceiling = resource_limits(args)
    requested = explicit_workers(args)
    if distribution is None:
        print("pytest: pytest-xdist is not installed; running serially", flush=True)
    else:
        if not has_workers(args):
            args = [f"-n{workers}", *args]
        if not has_distribution(args):
            args.append(f"--dist={distribution}")
        if requested is not None and requested > 0:
            workers = requested

    cgroup = enter_cgroup(memory_ceiling, workers, cores)
    limits = []
    if memory_ceiling is not None:
        limits.append(f"at most {memory_ceiling // (1024 * 1024)} MiB of RAM")
    quota = min(workers + 1, max(1, cores // CPU_SHARE), max(workers, 1))
    limits.append(f"at most {quota} CPU(s)")
    if cgroup is not None:
        limits.append("enforced with a cgroup")
    elif memory_ceiling is not None:
        limits.append("worker count only")
    print(
        f"pytest: {workers} worker(s), {'; '.join(limits)}"
        + ("" if distribution is None else f", --dist={distribution}"),
        flush=True,
    )

    env = os.environ.copy()
    env[SETTINGS_ENV] = "1"
    root = Path(__file__).resolve().parent.parent
    try:
        return run_pytest(pytest_command(args, cgroup), root, env, run_timeout())
    finally:
        leave_cgroup(cgroup)


if __name__ == "__main__":
    code = main()
    if code < 0:
        code = 128 - code
    raise SystemExit(code)
