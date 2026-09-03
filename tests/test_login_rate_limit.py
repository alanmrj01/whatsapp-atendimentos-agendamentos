from __future__ import annotations

import pytest

from app.auth.rate_limit import LoginRateLimiter


@pytest.mark.asyncio
async def test_per_email_limit_and_window_expiry() -> None:
    now = [100.0]
    limiter = LoginRateLimiter(
        global_limit=100,
        global_window_seconds=60,
        email_limit=2,
        email_window_seconds=30,
        clock=lambda: now[0],
    )

    await limiter.acquire("user@example.com")
    await limiter.acquire("user@example.com")

    with pytest.raises(Exception) as caught:
        await limiter.acquire("user@example.com")
    assert getattr(caught.value, "status_code", None) == 429
    assert caught.value.headers["Retry-After"] == "30"

    now[0] += 31
    await limiter.acquire("user@example.com")


@pytest.mark.asyncio
async def test_success_clears_only_email_bucket() -> None:
    now = [100.0]
    limiter = LoginRateLimiter(
        global_limit=10,
        global_window_seconds=60,
        email_limit=1,
        email_window_seconds=60,
        clock=lambda: now[0],
    )

    await limiter.acquire("user@example.com")
    await limiter.success("user@example.com")
    await limiter.acquire("user@example.com")

    # Global budget remains consumed even after a valid login.
    assert len(limiter._global) == 2
    assert "user@example.com" not in repr(limiter._emails)


@pytest.mark.asyncio
async def test_global_limit_covers_distinct_emails() -> None:
    now = [100.0]
    limiter = LoginRateLimiter(
        global_limit=2,
        global_window_seconds=20,
        email_limit=10,
        email_window_seconds=60,
        clock=lambda: now[0],
    )

    await limiter.acquire("a@example.com")
    await limiter.acquire("b@example.com")

    with pytest.raises(Exception) as caught:
        await limiter.acquire("c@example.com")
    assert getattr(caught.value, "status_code", None) == 429
    assert caught.value.headers["Retry-After"] == "20"

    now[0] += 21
    await limiter.acquire("c@example.com")
