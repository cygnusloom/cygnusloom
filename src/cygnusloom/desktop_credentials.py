"""Saved identity-provider logins for the desktop app.

The password never reaches a Cygnus Loom server. The desktop app fills the provider's own login
form and leaves the account, password, MFA, and password reset with the identity provider. Values
live in the operating system's credential store through ``keyring``; when no store is available
the desktop app still works, but it cannot remember a password.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

SERVICE = "cygnusloom"


class CredentialUnavailable(RuntimeError):
    """This computer has no usable credential store, or the store refused the request."""


@dataclass(frozen=True)
class Credential:
    username: str
    password: str


class CredentialStore:
    """One saved identity-provider login per server URL, stored in the OS keyring."""

    def __init__(self, backend: Any | None = None):
        self._backend = backend

    def _module(self) -> Any:
        if self._backend is not None:
            return self._backend
        try:
            import keyring
        except ImportError as exc:  # pragma: no cover - the desktop extra installs it
            raise CredentialUnavailable(
                "Password saving needs the desktop install. Log in without saving instead."
            ) from exc
        return keyring

    def available(self) -> bool:
        try:
            module = self._module()
            backend = module.get_keyring() if hasattr(module, "get_keyring") else module
        except Exception:  # noqa: BLE001 - any keyring failure is local
            return False
        name = str(getattr(backend, "name", "") or "").lower()
        return "fail" not in name and "null" not in name

    def get(self, server_url: str) -> Credential | None:
        module = self._module()
        try:
            raw = module.get_password(SERVICE, server_url)
        except Exception as exc:
            raise CredentialUnavailable("The saved password could not be read.") from exc
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        username = data.get("username") if isinstance(data, dict) else None
        password = data.get("password") if isinstance(data, dict) else None
        if not isinstance(username, str) or not isinstance(password, str):
            return None
        if not username or not password:
            return None
        return Credential(username=username, password=password)

    def save(self, server_url: str, username: str, password: str) -> None:
        if not username or not password:
            raise CredentialUnavailable("Enter both the username and the password.")
        module = self._module()
        try:
            module.set_password(
                SERVICE,
                server_url,
                json.dumps({"username": username, "password": password}),
            )
        except Exception as exc:
            raise CredentialUnavailable(
                "This computer could not save the password. Log in without saving instead."
            ) from exc

    def delete(self, server_url: str) -> None:
        module = self._module()
        try:
            module.delete_password(SERVICE, server_url)
        except Exception as exc:
            missing = getattr(getattr(module, "errors", None), "PasswordDeleteError", None)
            if missing is not None and isinstance(exc, missing):
                return
            raise CredentialUnavailable("The saved password could not be removed.") from exc
