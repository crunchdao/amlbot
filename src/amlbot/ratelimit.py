"""Client-side rate limiting.

AMLBot allows 5 requests per second per account and answers HTTP 429 above that. The limiters
here keep a client under that budget instead of relying on retries.

They only coordinate the clients that share an instance, inside one process. When several
processes or hosts use the same account (multiple web workers, for example), divide the budget
between them (``RateLimiter(limit=1)`` per worker with 5 workers) or route all traffic through a
single egress proxy that enforces it.
"""

from __future__ import annotations

import threading
import time
from collections import deque

import anyio

DEFAULT_LIMIT = 5
"""Documented AMLBot limit: requests per second per account."""


class _Window:
    """Sliding-window counter shared by both limiter flavours. Not thread-safe by itself."""

    def __init__(self, limit: int, period: float) -> None:
        if limit < 1:
            message = "limit must be >= 1"
            raise ValueError(message)

        if period <= 0:
            message = "period must be > 0"
            raise ValueError(message)

        self.limit: int = limit
        self.period: float = period
        self._timestamps: deque[float] = deque()

    def reserve(self, now: float) -> float:
        """Return how long to wait for a free slot; records the slot when the wait is 0."""
        while self._timestamps and now - self._timestamps[0] >= self.period:
            _ = self._timestamps.popleft()

        if len(self._timestamps) < self.limit:
            self._timestamps.append(now)
            return 0.0

        return self._timestamps[0] + self.period - now


class RateLimiter:
    """Thread-safe limiter for :class:`amlbot.Client`. Share one instance between clients."""

    def __init__(self, limit: int = DEFAULT_LIMIT, period: float = 1.0) -> None:
        """Allow at most ``limit`` requests in any ``period`` seconds."""
        self._window: _Window = _Window(limit, period)
        self._lock: threading.Lock = threading.Lock()

    def acquire(self) -> None:
        """Block until a request may be sent."""
        while True:
            with self._lock:
                wait = self._window.reserve(time.monotonic())

            if wait <= 0:
                return

            time.sleep(wait)


class AsyncRateLimiter:
    """Task-safe limiter for :class:`amlbot.AsyncClient`. Works under asyncio and trio."""

    def __init__(self, limit: int = DEFAULT_LIMIT, period: float = 1.0) -> None:
        """Allow at most ``limit`` requests in any ``period`` seconds."""
        self._window: _Window = _Window(limit, period)

    async def acquire(self) -> None:
        """Wait until a request may be sent."""
        while True:
            # Single event-loop thread: reserve() runs atomically between awaits.
            wait = self._window.reserve(time.monotonic())
            if wait <= 0:
                return

            await anyio.sleep(wait)
