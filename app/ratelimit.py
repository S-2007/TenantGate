"""A small fixed-window rate limiter for the public / expensive endpoints (Phase 6).

Keyed by (bucket, client IP). In-process: with several API instances each enforces its own
window, so the effective limit is N x the setting (D-042). That is acceptable for a portfolio
deployment with one instance; a shared limiter (Redis, or a Postgres table) is the upgrade.
No proxy header is trusted for the IP (same rule as the security logs), so behind a reverse
proxy the proxy must do its own limiting or the app must be told the proxy's address.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable

from fastapi import HTTPException, Request, status

from app.auth.dependencies import client_ip

security_log = logging.getLogger("app.security")


class RateLimiter:
    """``hit(bucket, key, limit, window_s)`` -> seconds to wait (0 = allowed)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, max_keys: int = 50_000) -> None:
        self._clock = clock
        self._max_keys = max_keys
        self._lock = threading.Lock()
        self._windows: dict[tuple[str, str], tuple[float, int]] = {}

    def hit(self, bucket: str, key: str, limit: int, window_s: float) -> float:
        now = self._clock()
        with self._lock:
            if len(self._windows) >= self._max_keys:  # bounded memory under a spray of IPs
                self._windows = (
                    {k: v for k, v in self._windows.items() if now - v[0] < window_s}
                    if len(self._windows) < 2 * self._max_keys
                    else {}
                )
            start, count = self._windows.get((bucket, key), (now, 0))
            if now - start >= window_s:
                start, count = now, 0
            if count >= limit:
                return max(0.0, window_s - (now - start))
            self._windows[(bucket, key)] = (start, count + 1)
            return 0.0

    def reset(self) -> None:
        with self._lock:
            self._windows.clear()


LIMITER = RateLimiter()


def enforce(request: Request, bucket: str, limit: int, window_s: float) -> None:
    """Raise 429 (with Retry-After) when this client exceeded ``limit`` per ``window_s``."""
    ip = client_ip(request)
    wait = LIMITER.hit(bucket, ip, limit, window_s)
    if wait > 0:
        security_log.warning("rate_limited bucket=%s ip=%s", bucket, ip)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "too many requests; try again later",
            headers={"Retry-After": str(max(1, math.ceil(wait)))},
        )
