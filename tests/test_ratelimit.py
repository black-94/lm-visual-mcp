"""Rate limiter tests (per-provider rpm + concurrency)."""

from __future__ import annotations

from lm_visual_mcp.vision.providers.ratelimit import RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_disabled_limiter_always_acquires():
    lim = RateLimiter()
    assert lim.enabled is False
    assert all(lim.try_acquire() for _ in range(100))


def test_rpm_window():
    clock = FakeClock()
    lim = RateLimiter(rpm=3, clock=clock)
    assert lim.try_acquire()
    assert lim.try_acquire()
    assert lim.try_acquire()
    assert lim.try_acquire() is False  # window full -> immediate refusal
    # Window slides: after 60s the oldest entry expires.
    clock.now += 60.1
    assert lim.try_acquire()


def test_concurrency_limit_and_release():
    clock = FakeClock()
    lim = RateLimiter(concurrency=1, clock=clock)
    assert lim.try_acquire()
    assert lim.try_acquire() is False  # slot busy
    lim.release()
    assert lim.try_acquire()  # slot freed


def test_rpm_only_has_no_concurrency_slot():
    lim = RateLimiter(rpm=10)
    lim.try_acquire()
    lim.release()  # no-op, must not raise or go negative


def test_invalid_config_rejected():
    import pytest

    with pytest.raises(ValueError):
        RateLimiter(rpm=0)
    with pytest.raises(ValueError):
        RateLimiter(concurrency=-1)
