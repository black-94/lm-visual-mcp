"""Router tests: fallback order, disabled providers, error policy."""

from __future__ import annotations

import pytest

from vision_mcp.config import AppConfig
from vision_mcp.errors import AllProvidersFailedError, ProviderUnavailableError
from vision_mcp.models import (
    ProviderFailureReason,
    ProviderResult,
    ProviderStatus,
    ProviderUsage,
    VisionRequest,
)
from vision_mcp.router import ProviderRouter


class FakeProvider:
    def __init__(self, name, *, available=True, fail_reason=None, operable=False):
        self.name = name
        self.available = available
        self.fail_reason = fail_reason
        self.operable = operable
        self.calls = 0

    async def probe(self, request=None):
        if not self.available:
            return ProviderStatus(name=self.name, available=False,
                                  reason=ProviderFailureReason.COMMAND_NOT_FOUND,
                                  message="not found")
        return ProviderStatus(name=self.name, available=True, model=f"{self.name}-m")

    async def analyze(self, request):
        self.calls += 1
        if self.fail_reason is not None:
            raise ProviderUnavailableError(self.fail_reason, f"{self.name} failed",
                                           operable=self.operable)
        return ProviderResult(provider=self.name, result={"answer": self.name},
                              model=f"{self.name}-m", usage=ProviderUsage())


def _router(providers, order, fallback_enabled=True):
    cfg = AppConfig(
        providers={"order": order},
        fallback={"enabled": fallback_enabled},
    )
    registry = {p.name: p for p in providers}
    return ProviderRouter(cfg, registry), cfg


def _req():
    return VisionRequest(system_prompt="s", user_prompt="u")


async def test_agy_success():
    agy = FakeProvider("agy")
    codex = FakeProvider("codex")
    router, _ = _router([agy, codex], ["agy", "codex"])
    out = await router.route(_req())
    assert out.provider == "agy"
    assert out.result["answer"] == "agy"
    assert out.fallbacks == []


async def test_agy_missing_falls_to_codex():
    agy = FakeProvider("agy", available=False)
    codex = FakeProvider("codex")
    router, _ = _router([agy, codex], ["agy", "codex"])
    out = await router.route(_req())
    assert out.provider == "codex"
    assert len(out.fallbacks) == 1
    assert out.fallbacks[0].provider == "agy"
    assert out.fallbacks[0].reason == "command_not_found"


async def test_agy_failure_falls_to_codex():
    agy = FakeProvider("agy", fail_reason=ProviderFailureReason.TEMPORARY_FAILURE)
    codex = FakeProvider("codex")
    router, _ = _router([agy, codex], ["agy", "codex"])
    out = await router.route(_req())
    assert out.provider == "codex"
    assert out.fallbacks[0].provider == "agy"


async def test_codex_missing_falls_to_gemini():
    agy = FakeProvider("agy", available=False)
    codex = FakeProvider("codex", available=False)
    gem = FakeProvider("gemini")
    router, _ = _router([agy, codex, gem], ["agy", "codex", "gemini"])
    out = await router.route(_req())
    assert out.provider == "gemini"
    assert len(out.fallbacks) == 2


async def test_gemini_key_missing_falls_to_opencode():
    gem = FakeProvider("gemini", available=False)
    oc = FakeProvider("opencode")
    router, _ = _router([gem, oc], ["gemini", "opencode"])
    out = await router.route(_req())
    assert out.provider == "opencode"


async def test_disabled_provider_ignored():
    # Only 'codex' is in the registry (agy/config disabled).
    codex = FakeProvider("codex")
    cfg = AppConfig(providers={"order": ["agy", "codex"]})
    router = ProviderRouter(cfg, {"codex": codex})
    out = await router.route(_req())
    assert out.provider == "codex"


async def test_configured_order_respected():
    # Order says gemini first even though agy is enabled.
    gem = FakeProvider("gemini")
    agy = FakeProvider("agy")
    cfg = AppConfig(providers={"order": ["gemini", "agy"]})
    router = ProviderRouter(cfg, {"gemini": gem, "agy": agy})
    out = await router.route(_req())
    assert out.provider == "gemini"


async def test_fallback_disabled_stops():
    agy = FakeProvider("agy", available=False)
    codex = FakeProvider("codex")
    router, _ = _router([agy, codex], ["agy", "codex"], fallback_enabled=False)
    with pytest.raises(AllProvidersFailedError):
        await router.route(_req())


async def test_non_fallback_error_stops():
    agy = FakeProvider("agy", fail_reason=ProviderFailureReason.INVALID_MODEL, operable=True)
    codex = FakeProvider("codex")
    router, _ = _router([agy, codex], ["agy", "codex"])
    with pytest.raises(ProviderUnavailableError):
        await router.route(_req())
    # codex never reached
    assert codex.calls == 0


async def test_all_unavailable_clear_error():
    agy = FakeProvider("agy", available=False)
    codex = FakeProvider("codex", available=False)
    router, _ = _router([agy, codex], ["agy", "codex"])
    with pytest.raises(AllProvidersFailedError, match="all providers failed"):
        await router.route(_req())


async def test_no_providers():
    router, _ = _router([], ["agy"])
    with pytest.raises(AllProvidersFailedError):
        await router.route(_req())