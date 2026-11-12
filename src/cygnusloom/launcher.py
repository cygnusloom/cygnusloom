"""The desktop app: a local server and a QtWebEngine window."""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
from collections.abc import MutableMapping
from datetime import datetime
from pathlib import Path

import uvicorn
from alembic import command as alembic_command

from cygnusloom import __version__
from cygnusloom.cli import migration_config
from cygnusloom.desktop import DesktopUnavailable, run_window

APP_TITLE = "Cygnus Loom"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_DIR_NAME = "logs"
RUN_LOG_PREFIX = "run-"
RUN_LOG_SUFFIX = ".log"
# One log file per run; old runs are discarded so diagnostics stay bounded.
RUN_LOGS_KEPT = 10
START_TIMEOUT_SECONDS = 30
STOP_TIMEOUT_SECONDS = 10
# Qt/PySide6 require at most this GLIBCXX version. A newer host is backward compatible.
REQUIRED_GLIBCXX = b"GLIBCXX_3.4.29"


def data_dir(override: str | None = None) -> Path:
    """Where the desktop app keeps its database, secret key, logs, and web cache."""
    if override:
        return Path(override).expanduser().resolve()
    configured = os.environ.get("CYGNUSLOOM_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "CygnusLoom"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "CygnusLoom"
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "cygnusloom"


def database_url(root: Path) -> str:
    return f"sqlite:///{(root / 'cygnusloom.db').as_posix()}"


def _secret_key(root: Path) -> str:
    path = root / "secret.key"
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = secrets.token_hex(32)
    path.write_text(value, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError as exc:
        logging.getLogger(__name__).debug("could not restrict %s to the owner: %s", path, exc)
    return value


def logs_dir(root: Path) -> Path:
    """Where the desktop app keeps one log file per run."""
    return root / LOG_DIR_NAME


def _run_log_path(root: Path) -> Path:
    """A fresh log file for this run. The fixed-width timestamp keeps name order = run order."""
    directory = logs_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return directory / f"{RUN_LOG_PREFIX}{stamp}-{os.getpid()}{RUN_LOG_SUFFIX}"


def _prune_run_logs(directory: Path, keep: int = RUN_LOGS_KEPT) -> None:
    """Keep only the newest ``keep`` run logs. A failed delete is reported, not raised, so a
    locked file from another process never stops the app from starting."""
    runs = sorted(directory.glob(f"{RUN_LOG_PREFIX}*{RUN_LOG_SUFFIX}"))
    for path in runs[: max(0, len(runs) - keep)]:
        try:
            path.unlink()
        except OSError as exc:
            logging.getLogger(__name__).warning("could not remove the old log %s: %s", path, exc)


def _configure_logging(root: Path, level: str) -> Path:
    """Log this run's backend and page output to a fresh file and to stdout.

    One file per run under ``<data dir>/logs`` (the newest ten runs are kept) makes a past
    session easy to inspect. The console handler writes to stdout so the lines the server and
    the web page produce are visible when the app is started from a terminal.
    """
    path = _run_log_path(root)
    handlers: list[logging.Handler] = [logging.FileHandler(path, encoding="utf-8")]
    # A windowed build may have no console at all; the file still has everything.
    stream = sys.stdout if sys.stdout is not None else sys.stderr
    if stream is not None:
        handlers.append(logging.StreamHandler(stream))
    logging.basicConfig(
        level=level.strip().upper(),
        format=LOG_FORMAT,
        handlers=handlers,
        force=True,
    )
    # The app's own requests are not worth a line each, but failures still are.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    _prune_run_logs(path.parent)
    return path


def _install_exception_logging() -> None:
    """Send uncaught exceptions to the run log.

    A windowed build has no console, so the default stderr traceback would be lost. Qt reports
    exceptions raised inside its slots through ``sys.excepthook``, and the server runs in a
    thread, so both hooks are covered.
    """
    logger = logging.getLogger("cygnusloom.uncaught")

    def log_uncaught(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical("uncaught exception", exc_info=(exc_type, exc_value, exc_tb))

    def log_thread_uncaught(args: threading.ExceptHookArgs) -> None:
        if issubclass(args.exc_type, SystemExit):
            return
        name = args.thread.name if args.thread is not None else "unknown"
        logger.critical(
            "uncaught exception in thread %s",
            name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = log_uncaught
    threading.excepthook = log_thread_uncaught


def _free_port(host: str) -> int:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _ignore_model_environment(env: MutableMapping[str, str]) -> None:
    """The desktop edition owns its model catalog. Do not inherit a developer's shell models
    (or their API keys); the first-run wizard asks for one model instead."""
    for key in list(env):
        if key in ("CYGNUSLOOM_LLM_PROVIDERS", "CYGNUSLOOM_LLM_DEFAULT_MODEL") or (
            key.startswith("CYGNUSLOOM_LLM_") and key.endswith(("_API_KEY", "_BASE_URL"))
        ):
            del env[key]


def _environment(root: Path, base_url: str, level: str, *, headless: bool) -> dict[str, str]:
    """The settings of the local server the desktop app runs."""
    return {
        "CYGNUSLOOM_SINGLE_PLAYER": "1",
        # The window adds the local server list and remote-server login. A headless
        # diagnostic server has no window to open them from.
        "CYGNUSLOOM_DESKTOP": "0" if headless else "1",
        "CYGNUSLOOM_DATA_DIR": str(root),
        "CYGNUSLOOM_DATABASE_URL": database_url(root),
        "CYGNUSLOOM_SECRET_KEY": _secret_key(root),
        "CYGNUSLOOM_BASE_URL": base_url,
        "CYGNUSLOOM_ENVIRONMENT": "development",
        "CYGNUSLOOM_AUTO_CREATE_SCHEMA": "0",
        "CYGNUSLOOM_SECURE_COOKIES": "0",
        "CYGNUSLOOM_TEST_LOGIN": "0",
        "CYGNUSLOOM_TEST_LOGIN_ADMIN": "0",
        "CYGNUSLOOM_LOG_LEVEL": level,
    }


def _loaded_library_path(soname: str) -> Path | None:
    """The path the process actually loaded for `soname`, from /proc/self/maps."""
    try:
        ctypes.CDLL(soname)
    except OSError:
        return None
    try:
        lines = Path("/proc/self/maps").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        for field in reversed(line.split()):
            name = Path(field).name
            if field.startswith("/") and (name == soname or name.startswith(soname + ".")):
                return Path(field)
    return None


def _library_has_symbol(path: Path, symbol: bytes) -> bool:
    """Whether a shared library contains a versioned symbol. An unreadable file is treated as
    usable, so an unusual system is never blocked by the preflight check."""
    try:
        return symbol in path.read_bytes()
    except OSError as exc:
        logging.getLogger(__name__).debug("could not read %s: %s", path, exc)
        return True


def _linux_runtime_problem() -> str:
    """A message when the host's libstdc++ is too old for QtWebEngine, or an empty string."""
    if not sys.platform.startswith("linux"):
        return ""
    soname = ctypes.util.find_library("stdc++") or "libstdc++.so.6"
    path = _loaded_library_path(soname)
    if path is None or _library_has_symbol(path, REQUIRED_GLIBCXX):
        return ""
    return (
        "Cygnus Loom needs the system C++ runtime (libstdc++) to provide "
        "GLIBCXX_3.4.29 or newer. This system's libstdc++ is too old. "
        "Install or update the C++ runtime, then start the game again."
    )


def _show_error(message: str) -> None:
    """Show a native error dialog when possible, and always keep the message in stderr."""
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.MessageBoxW(0, message, APP_TITLE, 0x10)
        except Exception:
            logging.getLogger(__name__).debug("could not show a native error dialog", exc_info=True)
    elif sys.platform.startswith("linux"):
        for command, arguments in (
            ("zenity", ["--error", "--text", message]),
            ("kdialog", ["--error", message]),
        ):
            executable = shutil.which(command)
            if executable is None:
                continue
            try:
                subprocess.run(
                    [executable, *arguments],
                    check=False,
                    timeout=60,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                logging.getLogger(__name__).debug(
                    "could not show a native error dialog with %s", executable, exc_info=True
                )
            break
    if sys.stderr is not None:
        print(message, file=sys.stderr)


class _InstanceLock:
    """One window, one server. A second launch reports the running URL and exits."""

    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            if sys.platform == "win32":
                import msvcrt

                handle.seek(0)
                if handle.read(1) == "":
                    handle.write("\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        if sys.platform == "win32":
            import msvcrt

            try:
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError as exc:
                logging.getLogger(__name__).debug("could not unlock the instance lock: %s", exc)
        self._handle.close()
        self._handle = None


class _Server(uvicorn.Server):
    """Signals readiness after startup, so the window never opens on a dead port."""

    def __init__(self, config: uvicorn.Config):
        super().__init__(config)
        self.ready = threading.Event()

    async def startup(self, sockets=None) -> None:
        await super().startup(sockets=sockets)
        self.ready.set()


def _runtime_file(root: Path) -> Path:
    return root / "runtime.json"


def _write_runtime(root: Path, url: str) -> None:
    _runtime_file(root).write_text(json.dumps({"url": url, "pid": os.getpid()}), encoding="utf-8")


def _read_runtime(root: Path) -> str:
    try:
        data = json.loads(_runtime_file(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    url = data.get("url") if isinstance(data, dict) else None
    return url if isinstance(url, str) else ""


def _clear_runtime(root: Path) -> None:
    try:
        _runtime_file(root).unlink()
    except OSError as exc:
        logging.getLogger(__name__).debug("could not remove runtime.json: %s", exc)


def play(
    *,
    data_dir_path: str | None = None,
    port: int = 0,
    host: str = "127.0.0.1",
    headless: bool = False,
    log_level: str | None = None,
) -> int:
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    if not headless:
        problem = _linux_runtime_problem()
        if problem:
            _show_error(problem)
            return 1
    root = data_dir(data_dir_path)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError as exc:
        logging.getLogger(__name__).debug("could not restrict %s to the owner: %s", root, exc)
    level = (log_level or os.environ.get("CYGNUSLOOM_LOG_LEVEL", "INFO")).strip().upper()
    lock = _InstanceLock(root / "instance.lock")
    if not lock.acquire():
        running = _read_runtime(root)
        _show_error(
            f"Cygnus Loom is already running at {running}."
            if running
            else "Cygnus Loom is already running."
        )
        return 0
    # A second launch that exits above is not a run, so only a real start gets a log file.
    log_path = _configure_logging(root, level)
    _install_exception_logging()
    log = logging.getLogger(__name__)
    log.info("Cygnus Loom %s starting; data folder %s; log file %s", __version__, root, log_path)
    server: _Server | None = None
    thread: threading.Thread | None = None
    try:
        actual_port = port or _free_port(host)
        base_url = f"http://127.0.0.1:{actual_port}"
        _ignore_model_environment(os.environ)
        os.environ.update(_environment(root, base_url, level, headless=headless))
        alembic_command.upgrade(migration_config(os.environ["CYGNUSLOOM_DATABASE_URL"]), "head")
        config = uvicorn.Config(
            "cygnusloom.web.app:app_from_env",
            factory=True,
            host=host,
            port=actual_port,
            log_level=level.lower(),
            log_config=None,
            access_log=False,
        )
        server = _Server(config)
        thread = threading.Thread(target=server.run, name="cygnusloom-server", daemon=True)
        thread.start()
        if not server.ready.wait(timeout=START_TIMEOUT_SECONDS):
            raise RuntimeError("the local server did not start")
        _write_runtime(root, base_url)
        log.info("serving %s from %s", base_url, root)
        if headless:
            print(f"Cygnus Loom is running at {base_url}")
            try:
                thread.join()
            except KeyboardInterrupt:
                server.should_exit = True
                thread.join(timeout=STOP_TIMEOUT_SECONDS)
            return 0
        try:
            run_window(base_url, APP_TITLE, root)
        finally:
            server.should_exit = True
            thread.join(timeout=STOP_TIMEOUT_SECONDS)
        return 0
    except DesktopUnavailable as exc:
        _show_error(str(exc))
        return 1
    except Exception as exc:
        log.exception("Cygnus Loom failed to start")
        _show_error(f"Cygnus Loom could not start: {exc}")
        return 1
    finally:
        if server is not None and server.ready.is_set() and not server.should_exit:
            server.should_exit = True
            if thread is not None:
                thread.join(timeout=STOP_TIMEOUT_SECONDS)
        _clear_runtime(root)
        lock.release()
