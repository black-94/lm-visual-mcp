"""Router chain-fallback tests (including rate-limit downgrade)."""

from __future__ import annotations

import pytest

from lm_visual_mcp.errors import AllProvidersFailedError, ProviderUnavailableError
from lm_visual_mcp.vision.providers.base import Provider
from lm_visual_mcp.vision.providers.ratelimit import RateLimiter
from lm_visual_mcp.vision.router import VisionRouter
from lm_visual_mcp.vision.types import (
    ImageRequest,
    ProviderFailureReason,
    ProviderResult,
    ProviderStatus,
)


def make_request() -> ImageRequest:
    return ImageRequest(system_prompt="s", user_prompt="u", images=[])


class FakeProvider(Provider):
    def __init__(self, name: str, *, result="ok", exc: Exception | None = None,
                 unavailable: bool = False, limiter: RateLimiter | None = None) -> None:
        super().__init__(limiter=limiter)
        self.name = name
        self.calls = 0
        self._result = result
        self._exc = exc
        self._unavailable = unavailable

    async def probe(self, request=None) -> ProviderStatus:
        return ProviderStatus(
            name=self.name, available=not self._unavailable,
            reason=ProviderFailureReason.COMMAND_NOT_FOUND if self._unavailable else None,
        )

    async def _analyze(self, request: ImageRequest) -> ProviderResult:
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return ProviderResult(provider=self.name, result={"answer": self._result})


async def test_first_provider_wins():
    a, b = FakeProvider("a"), FakeProvider("b")
    routed = await VisionRouter([a, b]).analyze(make_request())
    assert routed.provider == "a"
    assert a.calls == 1 and b.calls == 0


async def test_fallback_on_eligible_failure():
    exc = ProviderUnavailableError(ProviderFailureReason.TIMEOUT, "timed out")
    a, b = FakeProvider("a", exc=exc), FakeProvider("b")
    routed = await VisionRouter([a, b]).analyze(make_request())
    assert routed.provider == "b"
    assert [f.provider for f in routed.fallbacks] == ["a"]


async def test_rate_limited_provider_downgrades():
    """A provider whose own limiter is exhausted must not run; next serves."""
    clock = {"now": 0.0}

    def fake_clock() -> float:
        return clock["now"]

    limiter = RateLimiter(rpm=1, clock=fake_clock)
    a = FakeProvider("a", limiter=limiter)
    b = FakeProvider("b")
    router = VisionRouter([a, b])

    routed = await router.analyze(make_request())
    assert routed.provider == "a" and a.calls == 1

    # a's rpm window is now full -> RATE_LIMITED -> falls back to b.
    routed = await router.analyze(make_request())
    assert routed.provider == "b"
    assert a.calls == 1 and b.calls == 1
    assert routed.fallbacks[0].reason == "rate_limited"


async def test_non_eligible_failure_raises():
    exc = ProviderUnavailableError(ProviderFailureReason.INVALID_MODEL, "bad model")
    a, b = FakeProvider("a", exc=exc), FakeProvider("b")
    with pytest.raises(ProviderUnavailableError):
        await VisionRouter([a, b]).analyze(make_request())
    assert b.calls == 0


async def test_fallback_disabled_raises():
    exc = ProviderUnavailableError(ProviderFailureReason.TEMPORARY_FAILURE, "boom")
    a, b = FakeProvider("a", exc=exc), FakeProvider("b")
    with pytest.raises(ProviderUnavailableError):
        await VisionRouter([a, b], fallback_enabled=False).analyze(make_request())


async def test_unavailable_provider_skipped():
    a = FakeProvider("a", unavailable=True)
    b = FakeProvider("b")
    routed = await VisionRouter([a, b]).analyze(make_request())
    assert routed.provider == "b"
    assert a.calls == 0


async def test_all_fail():
    exc = ProviderUnavailableError(ProviderFailureReason.TEMPORARY_FAILURE, "boom")
    a, b = FakeProvider("a", exc=exc), FakeProvider("b", exc=exc)
    with pytest.raises(AllProvidersFailedError):
        await VisionRouter([a, b]).analyze(make_request())


async def test_empty_chain():
    with pytest.raises(AllProvidersFailedError):
        await VisionRouter([]).analyze(make_request())
