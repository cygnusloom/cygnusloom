from __future__ import annotations

import time
from collections import defaultdict, deque

WINDOW_SECONDS = 15 * 60
MAX_ATTEMPTS = 10


class RateLimiter:
    """In-memory sliding window limiter keyed by client address. Single process only."""

    def __init__(self, limit: int = MAX_ATTEMPTS, window: float = WINDOW_SECONDS):
        self._limit = limit
        self._window = window
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return False
        hits.append(now)
        return True

    def reset(self) -> None:
        self._hits.clear()
