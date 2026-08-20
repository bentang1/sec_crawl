"""A thread-safe token-bucket limiter shared across all SEC requests."""

from __future__ import annotations

import threading
import time


class RateLimiter:
    """Caps throughput to `rate` operations/second, safe to share across threads."""

    def __init__(self, rate: float, burst: int | None = None):
        self.rate = rate
        self.capacity = burst or max(1, int(rate))
        self._tokens = float(self.capacity)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._last_refill = now
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                deficit = 1 - self._tokens
                wait_time = deficit / self.rate
            time.sleep(wait_time)
