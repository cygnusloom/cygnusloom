"""Ephemeral "is typing" hints for a campaign's composer.

Typing is not part of the campaign log: it changes with every keystroke and expires on its own.
One server process keeps the hints in memory, the same way `turn_busy` tracks a running turn.
A deployment with several server processes may miss a hint written to another process; the next
live snapshot corrects it, and a missing hint costs nothing.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

TYPING_SECONDS = 5.0
NAME_LIMIT = 120


@dataclass(frozen=True)
class Typer:
    """A player whose composer had text a moment ago."""

    user_id: int
    name: str


class TypingRegistry:
    """The players typing in each campaign, forgotten after `seconds` without a heartbeat."""

    def __init__(self, seconds: float = TYPING_SECONDS) -> None:
        self._seconds = seconds
        self._lock = threading.Lock()
        self._typing: dict[int, dict[int, tuple[str, float]]] = {}

    def note(self, campaign_id: int, user_id: int, name: str) -> None:
        """Remember that the account has text in the campaign's message box."""
        label = name.strip()[:NAME_LIMIT] or f"account {user_id}"
        with self._lock:
            self._prune(campaign_id)
            players = self._typing.setdefault(campaign_id, {})
            players[user_id] = (label, time.monotonic())

    def stop(self, campaign_id: int, user_id: int) -> None:
        """Forget a hint at once, when the box is cleared, sent, or the page goes away."""
        with self._lock:
            players = self._typing.get(campaign_id)
            if players is None:
                return
            players.pop(user_id, None)
            if not players:
                self._typing.pop(campaign_id, None)

    def active(self, campaign_id: int) -> tuple[Typer, ...]:
        """Everyone typing in the campaign, in the order they started."""
        with self._lock:
            self._prune(campaign_id)
            players = self._typing.get(campaign_id)
            if not players:
                return ()
            return tuple(Typer(user_id, name) for user_id, (name, _) in players.items())

    def _prune(self, campaign_id: int) -> None:
        now = time.monotonic()
        players = self._typing.get(campaign_id)
        if not players:
            return
        for user_id, (_, seen) in list(players.items()):
            if now - seen > self._seconds:
                players.pop(user_id, None)
        if not players:
            self._typing.pop(campaign_id, None)
