from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from collections.abc import Callable

from fastapi import HTTPException

from app.auth.security import token_hash


class LoginRateLimiter:
    """Process-local login limiter.

    Cloud Run currently runs a single application instance/process, so this adds a
    backend boundary that cannot be bypassed by calling the public Cloud Run URL
    directly. Netlify rate limiting remains an additional edge layer.
    """

    def __init__(
        self,
        *,
        global_limit: int = 120,
        global_window_seconds: int = 60,
        email_limit: int = 8,
        email_window_seconds: int = 15 * 60,
        max_email_buckets: int = 4096,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.global_limit = global_limit
        self.global_window_seconds = global_window_seconds
        self.email_limit = email_limit
        self.email_window_seconds = email_window_seconds
        self.max_email_buckets = max_email_buckets
        self.clock = clock
        self._lock = asyncio.Lock()
        self._global: deque[float] = deque()
        self._emails: dict[str, deque[float]] = {}

    @staticmethod
    def _prune(bucket: deque[float], now: float, window: int) -> None:
        cutoff = now - window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

    @staticmethod
    def _limited(retry_after: float) -> HTTPException:
        return HTTPException(
            status_code=429,
            detail="Too many login attempts",
            headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
        )

    def _retry_after(self, bucket: deque[float], now: float, window: int) -> float:
        return (bucket[0] + window - now) if bucket else 1

    def _cleanup_email_buckets(self, now: float) -> None:
        if len(self._emails) <= self.max_email_buckets:
            return
        for key, bucket in list(self._emails.items()):
            self._prune(bucket, now, self.email_window_seconds)
            if not bucket:
                del self._emails[key]
        while len(self._emails) > self.max_email_buckets:
            self._emails.pop(next(iter(self._emails)))

    async def acquire(self, normalized_email: str) -> None:
        now = self.clock()
        key = token_hash(normalized_email)
        async with self._lock:
            self._prune(self._global, now, self.global_window_seconds)
            if len(self._global) >= self.global_limit:
                raise self._limited(self._retry_after(
                    self._global, now, self.global_window_seconds
                ))

            bucket = self._emails.setdefault(key, deque())
            self._prune(bucket, now, self.email_window_seconds)
            if len(bucket) >= self.email_limit:
                raise self._limited(self._retry_after(
                    bucket, now, self.email_window_seconds
                ))

            self._global.append(now)
            bucket.append(now)
            self._cleanup_email_buckets(now)

    async def success(self, normalized_email: str) -> None:
        # A successful proof of the password clears only that account bucket.
        # The global budget remains consumed to cap CPU even for valid requests.
        key = token_hash(normalized_email)
        async with self._lock:
            self._emails.pop(key, None)


login_rate_limiter = LoginRateLimiter()
