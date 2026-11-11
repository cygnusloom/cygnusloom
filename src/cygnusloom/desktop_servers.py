"""The desktop app's list of remote Cygnus Loom servers.

The list belongs to this device rather than to any server. It holds addresses, labels, and the
identity provider's address; passwords live in the operating system's credential store instead
(see ``desktop_credentials``). Both the local server thread and the Qt window read this file, so
writes are atomic and serialized in the process.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import secrets
import tempfile
import threading
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

FILE_NAME = "servers.json"
MAX_SERVERS = 50
MAX_NAME = 120
MAX_ACCOUNT = 120
_FORMAT_VERSION = 1
_LOCK = threading.RLock()
_LOG = logging.getLogger(__name__)


class ServerError(Exception):
    """A server address or name the desktop app cannot use. Safe to show the player."""


@dataclass(frozen=True)
class Server:
    """One remote server. ``url`` is a normalized origin such as ``https://table.example.com``.
    ``account`` is the username whose password was saved, for display only."""

    id: str
    url: str
    name: str
    account: str = ""
    oidc_issuer: str = ""
    created_at: str = ""
    last_used_at: str = ""

    @property
    def origin(self) -> str:
        return self.url


def is_loopback_host(host: str) -> bool:
    """Whether a hostname points at this machine. Plain HTTP is only allowed there."""
    value = (host or "").strip().strip("[]").lower()
    if value in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def origin_of(value: str) -> str:
    """The canonical scheme, host, and port of a URL, without a path or credentials."""
    parsed = urlsplit((value or "").strip())
    if not parsed.scheme or not parsed.hostname:
        return ""
    scheme = parsed.scheme.lower()
    try:
        port = parsed.port
    except ValueError:
        return ""
    if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        port = None
    netloc = parsed.hostname.lower()
    if ":" in netloc:
        netloc = f"[{netloc}]"
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((scheme, netloc, "", "", ""))


def normalize_server_url(value: str) -> str:
    """A server address safe to store, or ``ServerError``.

    A missing scheme becomes HTTPS. Remote servers must use HTTPS; plain HTTP is accepted only
    for this machine. The path is dropped because Cygnus Loom's routes are absolute.
    """
    text = (value or "").strip()
    if not text:
        raise ServerError("Enter a server address.")
    if "://" not in text:
        text = f"https://{text}"
    parsed = urlsplit(text)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ServerError("A server address must start with https:// or http://.")
    if parsed.username or parsed.password:
        raise ServerError("Do not put a username or password in the server address.")
    if not parsed.hostname:
        raise ServerError("That server address has no host name.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ServerError("That server address has an invalid port.") from exc
    scheme = parsed.scheme.lower()
    if scheme == "http" and not is_loopback_host(parsed.hostname):
        raise ServerError("Use https:// for a server on another computer.")
    if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        port = None
    netloc = parsed.hostname
    if ":" in netloc:
        netloc = f"[{netloc}]"
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((scheme, netloc, "", "", ""))


def normalize_invite_url(value: str) -> str:
    """An Authentik invitation link. The flow eventually redirects to the Cygnus Loom server."""
    text = (value or "").strip()
    if not text:
        raise ServerError("Enter the invitation link.")
    parsed = urlsplit(text)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ServerError("An invitation link must start with https:// or http://.")
    if parsed.username or parsed.password:
        raise ServerError("That invitation link contains a username or password.")
    if not parsed.hostname:
        raise ServerError("That invitation link has no host name.")
    if parsed.scheme.lower() == "http" and not is_loopback_host(parsed.hostname):
        raise ServerError("Use an https:// invitation link from another computer.")
    if "/if/flow/" not in parsed.path:
        raise ServerError("That is not an Authentik invitation link.")
    if parsed.fragment:
        raise ServerError("That invitation link has an unexpected fragment.")
    return text


def clean_name(value: str, fallback: str = "") -> str:
    """A display name with surrounding space and control characters removed."""
    name = "".join(character for character in (value or "") if character.isprintable()).strip()
    name = name[:MAX_NAME]
    return name or fallback


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _default_name(url: str) -> str:
    parsed = urlsplit(url)
    return parsed.netloc or parsed.hostname or url


def _server_from_mapping(row: object) -> Server | None:
    if not isinstance(row, dict):
        return None
    server_id = row.get("id")
    url = row.get("url")
    if not isinstance(server_id, str) or not server_id or not isinstance(url, str) or not url:
        return None
    try:
        url = normalize_server_url(url)
    except ServerError:
        return None
    return Server(
        id=server_id,
        url=url,
        name=clean_name(row.get("name") if isinstance(row.get("name"), str) else "", url),
        account=clean_name(row.get("account") if isinstance(row.get("account"), str) else "", "")[
            :MAX_ACCOUNT
        ],
        oidc_issuer=origin_of(
            row.get("oidc_issuer") if isinstance(row.get("oidc_issuer"), str) else ""
        ),
        created_at=row.get("created_at") if isinstance(row.get("created_at"), str) else "",
        last_used_at=row.get("last_used_at") if isinstance(row.get("last_used_at"), str) else "",
    )


class ServerStore:
    """The server list in one JSON file. The file is not secret, but it is this device's data."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def list(self) -> list[Server]:
        with _LOCK:
            servers = self._read()
        return sorted(
            servers,
            key=lambda server: (
                server.last_used_at or server.created_at or "",
                server.name.lower(),
            ),
            reverse=True,
        )

    def get(self, server_id: str) -> Server:
        with _LOCK:
            for server in self._read():
                if server.id == server_id:
                    return server
        raise ServerError("That server is not in the list.")

    def find_by_url(self, url: str) -> Server | None:
        normalized = normalize_server_url(url)
        with _LOCK:
            return next((server for server in self._read() if server.url == normalized), None)

    def add(self, url: str, name: str = "", oidc_issuer: str = "") -> Server:
        normalized = normalize_server_url(url)
        with _LOCK:
            servers = self._read()
            existing = next((server for server in servers if server.url == normalized), None)
            if existing is not None:
                return existing
            if len(servers) >= MAX_SERVERS:
                raise ServerError(f"The server list is limited to {MAX_SERVERS} servers.")
            now = _now()
            server = Server(
                id=secrets.token_hex(12),
                url=normalized,
                name=clean_name(name, _default_name(normalized)),
                oidc_issuer=origin_of(oidc_issuer),
                created_at=now,
                last_used_at=now,
            )
            servers.append(server)
            self._write(servers)
            return server

    def rename(self, server_id: str, name: str) -> Server:
        with _LOCK:
            servers = self._read()
            for index, server in enumerate(servers):
                if server.id != server_id:
                    continue
                renamed = replace(server, name=clean_name(name, server.name))
                servers[index] = renamed
                self._write(servers)
                return renamed
        raise ServerError("That server is not in the list.")

    def remove(self, server_id: str) -> None:
        with _LOCK:
            servers = self._read()
            remaining = [server for server in servers if server.id != server_id]
            if len(remaining) == len(servers):
                raise ServerError("That server is not in the list.")
            self._write(remaining)

    def set_account(self, server_id: str, account: str) -> Server:
        return self._update(server_id, account=clean_name(account, "")[:MAX_ACCOUNT])

    def set_issuer(self, server_id: str, issuer: str) -> Server:
        return self._update(server_id, oidc_issuer=origin_of(issuer))

    def touch(self, server_id: str) -> Server:
        return self._update(server_id, last_used_at=_now())

    def _update(self, server_id: str, **changes: str) -> Server:
        with _LOCK:
            servers = self._read()
            for index, server in enumerate(servers):
                if server.id != server_id:
                    continue
                updated = replace(server, **changes)
                servers[index] = updated
                self._write(servers)
                return updated
        raise ServerError("That server is not in the list.")

    def _read(self) -> list[Server]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            _LOG.warning("could not read %s: %s", self.path, exc)
            return []
        try:
            data = json.loads(raw)
        except ValueError:
            _LOG.warning("the server list at %s is not valid JSON; starting a new list", self.path)
            try:
                self.path.replace(self.path.with_name(f"{self.path.name}.corrupt"))
            except OSError:
                pass
            return []
        rows = data.get("servers") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return []
        servers: list[Server] = []
        seen: set[str] = set()
        for row in rows:
            server = _server_from_mapping(row)
            if server is not None and server.id not in seen:
                servers.append(server)
                seen.add(server.id)
        return servers

    def _write(self, servers: list[Server]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _FORMAT_VERSION,
            "servers": [asdict(server) for server in servers],
        }
        handle, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
