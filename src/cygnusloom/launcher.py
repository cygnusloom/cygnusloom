"""The single-player edition: a local server and a QtWebEngine window."""

from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import sys
import threading
from collections.abc import MutableMapping
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn
from alembic import command as alembic_command

from cygnusloom.cli import migration_config
from cygnusloom.desktop import DesktopUnavailable, run_window

APP_TITLE = "Cygnus Loom"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
MAX_LOG_BYTES = 2_000_000
LOG_BACKUPS = 3
START_TIMEOUT_SECONDS = 30
STOP_TIMEOUT_SECONDS = 10


def data_dir(override: str | None = None) -> Path:
    """Where the single-player edition keeps its database, secret key, logs, and web cache."""
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
    except OSError:
        pass
    return value


def _configure_logging(root: Path, level: str) -> None:
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "cygnusloom.log",
        maxBytes=MAX_LOG_BYTES,
        backupCount=LOG_BACKUPS,
        encoding="utf-8",
    )
    handlers: list[logging.Handler] = [handler]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=level.strip().upper(), format=LOG_FORMAT, handlers=handlers)
    # The app's own requests are not worth a line each, but failures still are.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


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
            except OSError:
                pass
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
    except OSError:
        pass


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
    root = data_dir(data_dir_path)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    level = (log_level or os.environ.get("CYGNUSLOOM_LOG_LEVEL", "INFO")).strip().upper()
    _configure_logging(root, level)
    log = logging.getLogger(__name__)
    lock = _InstanceLock(root / "instance.lock")
    if not lock.acquire():
        running = _read_runtime(root)
        print(
            f"Cygnus Loom is already running at {running}."
            if running
            else "Cygnus Loom is already running."
        )
        return 0
    server: _Server | None = None
    thread: threading.Thread | None = None
    try:
        actual_port = port or _free_port(host)
        base_url = f"http://127.0.0.1:{actual_port}"
        _ignore_model_environment(os.environ)
        os.environ.update(
            CYGNUSLOOM_SINGLE_PLAYER="1",
            CYGNUSLOOM_DATA_DIR=str(root),
            CYGNUSLOOM_DATABASE_URL=database_url(root),
            CYGNUSLOOM_SECRET_KEY=_secret_key(root),
            CYGNUSLOOM_BASE_URL=base_url,
            CYGNUSLOOM_ENVIRONMENT="development",
            CYGNUSLOOM_AUTO_CREATE_SCHEMA="0",
            CYGNUSLOOM_SECURE_COOKIES="0",
            CYGNUSLOOM_TEST_LOGIN="0",
            CYGNUSLOOM_TEST_LOGIN_ADMIN="0",
            CYGNUSLOOM_LOG_LEVEL=level,
        )
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
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        log.exception("Cygnus Loom failed to start")
        print(f"Cygnus Loom could not start: {exc}", file=sys.stderr)
        return 1
    finally:
        if server is not None and server.ready.is_set() and not server.should_exit:
            server.should_exit = True
            if thread is not None:
                thread.join(timeout=STOP_TIMEOUT_SECONDS)
        _clear_runtime(root)
        lock.release()
