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
the memory and CPU ceilings still apply.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SETTINGS_ENV = "CYGNUSLOOM_TEST_SETTINGS"
CGROUP_ROOT = Path("/sys/fs/cgroup")
# A worker's peak memory was under 400 MiB in a full suite run. Round up so the memory ceiling
# never limits the worker pool before the CPU share does.
MEMORY_PER_WORKER = 512 * 1024 * 1024
CPU_SHARE = 2  # at most half the CPUs
MEMORY_SHARE = 2  # at most half the RAM, never counting swap


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


def resource_limits() -> tuple[int, int | None]:
    """The number of workers and a memory ceiling in bytes, or None when it is unknown.

    The CPU budget is half the logical CPUs, less whatever the machine is already using.
    The memory budget is half the RAM, or what is available after a reserve for the system
    and other work, whichever is smaller.
    """
    cores = cpu_count()
    half_cores = max(1, cores // CPU_SHARE)
    free_cores = max(1, int(cores - load_average()))
    workers = min(half_cores, free_cores)

    memory = meminfo()
    total = memory.get("MemTotal", 0)
    if not total:
        return workers, None
    half = total // MEMORY_SHARE
    available = memory.get("MemAvailable", total)
    reserve = max(1024 * 1024 * 1024, total // 10)
    usable = max(available - reserve, 0)
    ceiling = min(half, max(usable, MEMORY_PER_WORKER))
    workers = max(1, min(workers, ceiling // MEMORY_PER_WORKER))
    return workers, ceiling


def enter_cgroup(memory_ceiling: int, workers: int) -> Path | None:
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
    try:
        (cgroup / "memory.max").write_text(str(memory_ceiling), encoding="utf-8")
        limited = True
    except OSError:
        pass
    try:
        # cpu.max takes a quota and a period, both in microseconds: one CPU per 100000.
        (cgroup / "cpu.max").write_text(f"{workers * 100_000} 100_000", encoding="utf-8")
        limited = True
    except OSError:
        pass
    if not limited:
        try:
            cgroup.rmdir()
        except OSError:
            pass
        return None
    return cgroup


def leave_cgroup(cgroup: Path | None) -> None:
    """Remove the cgroup once the process it held has ended."""
    if cgroup is None:
        return
    try:
        cgroup.rmdir()
    except OSError:
        pass


def pytest_command(args: list[str], cgroup: Path | None) -> list[str]:
    """The pytest command, wrapped to enter the cgroup before exec when there is one."""
    if cgroup is None:
        return [sys.executable, "-m", "pytest", *args]
    launcher = (
        "import os, sys\n"
        "try:\n"
        "    open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "except OSError as error:\n"
        "    print(f'warning: the test cgroup is unavailable: {error}', file=sys.stderr)\n"
        "os.execv(sys.executable, [sys.executable, '-m', 'pytest', *sys.argv[2:]])\n"
    )
    return [sys.executable, "-c", launcher, str(cgroup / "cgroup.procs"), *args]


def has_workers(args: list[str]) -> bool:
    for argument in args:
        if argument in {"-n", "--numprocesses"}:
            return True
        if argument.startswith("--numprocesses=") or (
            argument.startswith("-n") and len(argument) > 2
        ):
            return True
    return False


def has_distribution(args: list[str]) -> bool:
    return any(a == "--dist" or a.startswith("--dist=") for a in args)


def main() -> int:
    args = sys.argv[1:]
    workers, memory_ceiling = resource_limits()
    if not has_workers(args):
        args = [f"-n{workers}", *args]
    if not has_distribution(args):
        args.append("--dist=worksteal")

    cgroup = None if memory_ceiling is None else enter_cgroup(memory_ceiling, workers)
    if memory_ceiling is None:
        print(
            f"pytest: {workers} worker(s); memory ceiling unavailable on this platform", flush=True
        )
    else:
        share = "with a cgroup ceiling" if cgroup is not None else "worker count only"
        print(
            f"pytest: {workers} worker(s), at most {memory_ceiling // (1024 * 1024)} MiB "
            f"of RAM ({share})",
            flush=True,
        )

    env = os.environ.copy()
    env[SETTINGS_ENV] = "1"
    root = Path(__file__).resolve().parent.parent
    try:
        return subprocess.run(
            pytest_command(args, cgroup),
            cwd=root,
            env=env,
            check=False,
        ).returncode
    finally:
        leave_cgroup(cgroup)


if __name__ == "__main__":
    raise SystemExit(main())
