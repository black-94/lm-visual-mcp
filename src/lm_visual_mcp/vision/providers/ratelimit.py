"""Provider-neutral rate limiter used inside every provider instance.

Two independent limits, either or both configurable per provider:

- ``rpm``: sliding-window requests-per-minute. Full window -> limited.
- ``concurrency``: max in-flight requests. All slots busy -> limited.

``try_acquire`` is deliberately non-blocking: when a limit is reached the
caller raises ``RATE_LIMITED`` and the router falls back to the next provider
instead of queueing.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Callable, Optional

_WINDOW_SECONDS = 60.0


class RateLimiter:
    def __init__(
        self,
        *,
        rpm: Optional[int] = None,
        concurrency: Optional[int] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if rpm is not None and rpm <= 0:
            raise ValueError("rpm must be positive")
        if concurrency is not None and concurrency <= 0:
            raise ValueError("concurrency must be positive")
        self.rpm = rpm
        self.concurrency = concurrency
        self._clock = clock
        self._window: deque[float] = deque()
        self._in_flight = 0

    @property
    def enabled(self) -> bool:
        return self.rpm is not None or self.concurrency is not None

    def try_acquire(self) -> bool:
        """Reserve one slot. Returns False immediately when a limit is reached."""
        if not self.enabled:
            return True
        now = self._clock()
        if self.rpm is not None:
            while self._window and now - self._window[0] >= _WINDOW_SECONDS:
                self._window.popleft()
            if len(self._window) >= self.rpm:
                return False
        if self.concurrency is not None and self._in_flight >= self.concurrency:
            return False
        if self.rpm is not None:
            self._window.append(now)
        if self.concurrency is not None:
            self._in_flight += 1
        return True

    def release(self) -> None:
        """Give back one concurrency slot (no-op for rpm-only limiters)."""
        if self.concurrency is not None and self._in_flight > 0:
            self._in_flight -= 1
