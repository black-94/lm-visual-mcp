"""Router tests: image chain fallback (incl. rate-limit downgrade) + classifier chain."""

from __future__ import annotations

import pytest

from lm_visual_mcp.errors import AllProvidersFailedError, ProviderUnavailableError
from lm_visual_mcp.providers.base import Provider
from lm_visual_mcp.providers.ratelimit import RateLimiter
from lm_visual_mcp.providers.router import ProviderRouter
from lm_visual_mcp.providers.types import (
    ImageRequest,
    ProviderFailureReason,
    ProviderRequest,
    ProviderResponse,
    ProviderResult,
    ProviderStatus,
)


def make_request() -> ImageRequest:
    return ImageRequest(system_prompt="s", user_prompt="u", images=[])


class FakeProvider(Provider):
    def __init__(self, name: str, *, result="ok", exc: Exception | None = None,
                 unavailable: bool = False, limiter: RateLimiter | None = None,
                 cascade_results=None) -> None:
        super().__init__(limiter=limiter)
        self.name = name
        self.calls = 0
        self._result = result
        self._exc = exc
        self._unavailable = unavailable
        self._cascade_results = cascade_results or []
        self.classifier_calls = 0

    async def probe_image(self, request=None) -> ProviderStatus:
        return ProviderStatus(
            name=self.name, available=not self._unavailable,
            reason=ProviderFailureReason.COMMAND_NOT_FOUND if self._unavailable else None,
        )

    async def _analyze_image(self, request: ImageRequest) -> ProviderResult:
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return ProviderResult(provider=self.name, result={"answer": self._result})


def make_router(providers, image_chain, classifier_chain=None):
    return ProviderRouter(
        {p.name: p for p in providers},
        image_chain=image_chain,
        classifier_chain=classifier_chain if classifier_chain is not None else [],
    )


# -- image chain -------------------------------------------------------------


async def test_first_provider_wins():
    a, b = FakeProvider("a"), FakeProvider("b")
    routed = await make_router([a, b], ["a", "b"]).analyze_image(make_request())
    assert routed.provider == "a"
    assert a.calls == 1 and b.calls == 0


async def test_fallback_on_eligible_failure():
    exc = ProviderUnavailableError(ProviderFailureReason.TIMEOUT, "timed out")
    a, b = FakeProvider("a", exc=exc), FakeProvider("b")
    routed = await make_router([a, b], ["a", "b"]).analyze_image(make_request())
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
    router = make_router([a, b], ["a", "b"])

    routed = await router.analyze_image(make_request())
    assert routed.provider == "a" and a.calls == 1

    # a's rpm window is now full -> RATE_LIMITED -> falls back to b.
    routed = await router.analyze_image(make_request())
    assert routed.provider == "b"
    assert a.calls == 1 and b.calls == 1
    assert routed.fallbacks[0].reason == "rate_limited"


async def test_non_eligible_failure_raises():
    exc = ProviderUnavailableError(ProviderFailureReason.INVALID_MODEL, "bad model")
    a, b = FakeProvider("a", exc=exc), FakeProvider("b")
    with pytest.raises(ProviderUnavailableError):
        await make_router([a, b], ["a", "b"]).analyze_image(make_request())
    assert b.calls == 0


async def test_fallback_disabled_raises():
    exc = ProviderUnavailableError(ProviderFailureReason.TEMPORARY_FAILURE, "boom")
    a, b = FakeProvider("a", exc=exc), FakeProvider("b")
    router = ProviderRouter(
        {"a": a, "b": b}, image_chain=["a", "b"], classifier_chain=[],
        fallback_enabled=False,
    )
    with pytest.raises(ProviderUnavailableError):
        await router.analyze_image(make_request())
    assert b.calls == 0


async def test_unavailable_provider_skipped():
    a = FakeProvider("a", unavailable=True)
    b = FakeProvider("b")
    routed = await make_router([a, b], ["a", "b"]).analyze_image(make_request())
    assert routed.provider == "b"
    assert a.calls == 0


async def test_chain_unknown_name_skipped():
    a = FakeProvider("a")
    routed = await make_router([a], ["missing", "a"]).analyze_image(make_request())
    assert routed.provider == "a"


async def test_all_fail():
    exc = ProviderUnavailableError(ProviderFailureReason.TEMPORARY_FAILURE, "boom")
    a, b = FakeProvider("a", exc=exc), FakeProvider("b", exc=exc)
    with pytest.raises(AllProvidersFailedError):
        await make_router([a, b], ["a", "b"]).analyze_image(make_request())


async def test_empty_image_chain():
    with pytest.raises(AllProvidersFailedError):
        await make_router([], []).analyze_image(make_request())


# -- classifier chain ---------------------------------------------------------


class ClassifierProvider(Provider):
    def __init__(self, name: str, *, request_body=None, response_body=None) -> None:
        super().__init__()
        self.name = name
        self._request_body = request_body  # bytes body to rewrite to, or None (no change)
        self._response_body = response_body
        self.req_calls = 0
        self.resp_calls = 0

    async def rewrite_classifier_request(self, request: ProviderRequest):
        self.req_calls += 1
        if self._request_body is None:
            return request.body, False
        return self._request_body, True

    async def rewrite_classifier_response(self, response: ProviderResponse):
        self.resp_calls += 1
        if self._response_body is None:
            return response.body, False
        return self._response_body, True


def make_req(body=b"orig"):
    return ProviderRequest(protocol="anthropic", url="http://x", model="m", headers={}, body=body)


class VerdictProvider(Provider):
    """A provider whose own model returns a classifier verdict."""

    def __init__(self, name: str, verdict=None, exc: Exception | None = None) -> None:
        super().__init__()
        self.name = name
        self._verdict = verdict  # None = unable -> returns None
        self._exc = exc
        self.calls = 0

    async def classify(self, request: ProviderRequest):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        if self._verdict is None:
            return None
        from lm_visual_mcp.providers.types import ClassifierResult

        return ClassifierResult(provider=self.name, model=f"{self.name}-m", verdict=self._verdict)


def make_resp(body=b"resp"):
    return ProviderResponse(url="http://x", status=200, headers={}, body=body)


async def test_classify_request_first_changed_wins():
    a = ClassifierProvider("a")  # no change
    b = ClassifierProvider("b", request_body=b"new")
    c = ClassifierProvider("c", request_body=b"also")
    req, provider = await make_router([a, b, c], [], ["a", "b", "c"]).classify_request(make_req())
    assert provider == "b"
    assert req.body == b"new"
    assert a.req_calls == 1 and b.req_calls == 1 and c.req_calls == 0


async def test_classify_request_none_changed_passthrough():
    a = ClassifierProvider("a")
    b = ClassifierProvider("b")
    req, provider = await make_router([a, b], [], ["a", "b"]).classify_request(make_req())
    assert provider is None
    assert req.body == b"orig"


async def test_classify_response_first_changed_wins():
    a = ClassifierProvider("a")
    b = ClassifierProvider("b", response_body=b"normalized")
    resp, provider = await make_router([a, b], [], ["a", "b"]).classify_response(make_resp())
    assert provider == "b"
    assert resp.body == b"normalized"


async def test_classify_response_none_changed_passthrough():
    a = ClassifierProvider("a")
    resp, provider = await make_router([a], [], ["a"]).classify_response(make_resp())
    assert provider is None
    assert resp.body == b"resp"


async def test_classifier_verdict_first_success_wins():
    a = VerdictProvider("a")           # unable -> None
    b = VerdictProvider("b", verdict="no")
    c = VerdictProvider("c", verdict="yes")
    result = await make_router([a, b, c], [], ["a", "b", "c"]).classifier_verdict(make_req())
    assert result is not None
    assert result.provider == "b"
    assert result.verdict == "no"
    assert a.calls == 1 and b.calls == 1 and c.calls == 0


async def test_classifier_verdict_advances_on_exception():
    a = VerdictProvider("a", exc=RuntimeError("boom"))
    b = VerdictProvider("b")
    c = VerdictProvider("c", verdict="yes")
    result = await make_router([a, b, c], [], ["a", "b", "c"]).classifier_verdict(make_req())
    assert result is not None and result.provider == "c"


async def test_classifier_verdict_none_passthrough():
    a = VerdictProvider("a")
    b = VerdictProvider("b")
    result = await make_router([a, b], [], ["a", "b"]).classifier_verdict(make_req())
    assert result is None
    assert a.calls == 1 and b.calls == 1


async def test_classifier_verdict_skips_non_capable_provider():
    # agy/codex-style provider (base classify -> None) is transparently skipped.
    plain = FakeProvider("plain")
    b = VerdictProvider("b", verdict="yes")
    result = await make_router([plain, b], [], ["plain", "b"]).classifier_verdict(make_req())
    assert result is not None and result.provider == "b"
    assert b.calls == 1


async def test_classifier_chain_independent_of_image_chain():
    # agy/codex-style CLI provider: no classifier capability (default passthrough).
    img = FakeProvider("cli")
    cls = ClassifierProvider("c")  # no change
    router = ProviderRouter(
        {"cli": img, "c": cls},
        image_chain=["cli"],
        classifier_chain=["c"],
    )
    routed = await router.analyze_image(make_request())
    assert routed.provider == "cli"
    req, req_provider = await router.classify_request(make_req())
    assert req_provider is None and req.body == b"orig"