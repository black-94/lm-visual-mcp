"""Provider registry tests: config-driven chain construction, no hardcoding."""

from __future__ import annotations

from lm_visual_mcp.config import AppConfig, ProviderEntryConfig, VisionConfig
from lm_visual_mcp.vision.providers import PROVIDER_TYPES, build_chain, build_provider
from lm_visual_mcp.vision.providers.agy import AgyProvider
from lm_visual_mcp.vision.providers.codex import CodexProvider
from lm_visual_mcp.vision.providers.gemini import GeminiProvider
from lm_visual_mcp.vision.providers.opencode import OpenCodeProvider


def test_registry_contains_expected_types():
    assert set(PROVIDER_TYPES) == {"agy", "codex", "gemini", "opencode"}


def test_build_provider_by_type():
    cfg = VisionConfig()
    p = build_provider(
        ProviderEntryConfig(name="my-gemini", type="gemini"), cfg
    )
    assert isinstance(p, GeminiProvider)
    assert p.name == "my-gemini"  # config name wins for multi-instance setups


def test_build_chain_order_and_enabled():
    cfg = VisionConfig(
        providers=[
            ProviderEntryConfig(name="c1", type="codex"),
            ProviderEntryConfig(name="a1", type="agy", enabled=False),
            ProviderEntryConfig(name="g1", type="gemini"),
        ]
    )
    chain = build_chain(cfg)
    assert [p.name for p in chain] == ["c1", "g1"]
    assert isinstance(chain[0], CodexProvider)
    assert isinstance(chain[1], GeminiProvider)


def test_rate_limit_wired_into_provider():
    cfg = VisionConfig()
    p = build_provider(
        ProviderEntryConfig(
            name="g", type="gemini", rate_limit={"rpm": 10, "concurrency": 2}
        ),
        cfg,
    )
    assert p._limiter is not None
    assert p._limiter.rpm == 10 and p._limiter.concurrency == 2


def test_no_rate_limit_by_default():
    cfg = VisionConfig()
    p = build_provider(ProviderEntryConfig(name="g", type="gemini"), cfg)
    assert p._limiter is None


def test_opencode_is_api_provider_with_key_env(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "k-test")
    cfg = AppConfig()
    p = build_provider(
        ProviderEntryConfig(name="oc", type="opencode"), cfg.vision
    )
    assert isinstance(p, OpenCodeProvider)
    assert p._api_key == "k-test"
