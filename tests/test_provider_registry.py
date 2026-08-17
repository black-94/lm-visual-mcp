"""Provider registry tests: config-driven construction + dual-chain build, no hardcoding."""

from __future__ import annotations

from lm_visual_mcp.config import AppConfig, ProviderEntryConfig
from lm_visual_mcp.providers import (
    PROVIDER_TYPES,
    build_provider,
    build_router,
    resolve_providers,
)
from lm_visual_mcp.providers.agy import AgyProvider
from lm_visual_mcp.providers.codex import CodexProvider
from lm_visual_mcp.providers.gemini import GeminiProvider
from lm_visual_mcp.providers.opencode import OpenCodeProvider
from lm_visual_mcp.providers.volcengine import VolcengineProvider


def test_registry_contains_expected_types():
    assert set(PROVIDER_TYPES) == {"agy", "codex", "gemini", "opencode", "volcengine"}


def test_build_provider_by_type():
    cfg = AppConfig()
    p = build_provider(ProviderEntryConfig(name="my-gemini", type="gemini"), cfg)
    assert isinstance(p, GeminiProvider)
    assert p.name == "my-gemini"  # config name wins for multi-instance setups


def test_build_provider_default_disable_thinking_none():
    cfg = AppConfig()
    p = build_provider(ProviderEntryConfig(name="g", type="gemini"), cfg)
    assert p.disable_thinking is None


def test_build_provider_passes_disable_thinking():
    cfg = AppConfig()
    p = build_provider(
        ProviderEntryConfig(name="g", type="gemini", disable_thinking=True), cfg
    )
    assert p.disable_thinking is True


def test_build_provider_passes_mode_and_base_url():
    cfg = AppConfig()
    p = build_provider(
        ProviderEntryConfig(name="oc", type="opencode", mode="zen"), cfg
    )
    assert isinstance(p, OpenCodeProvider)
    assert p.mode == "zen"
    assert p.base_url == "https://opencode.ai/zen/v1"


def test_build_provider_volcengine():
    cfg = AppConfig()
    p = build_provider(
        ProviderEntryConfig(name="vc", type="volcengine", mode="agent"), cfg
    )
    assert isinstance(p, VolcengineProvider)
    assert p.mode == "agent"


def test_resolve_providers_builds_only_enabled():
    cfg = AppConfig(
        providers=[
            ProviderEntryConfig(name="c1", type="codex"),
            ProviderEntryConfig(name="a1", type="agy", enabled=False),
            ProviderEntryConfig(name="g1", type="gemini"),
        ],
        vision={"image_chain": ["c1", "g1"], "classifier_chain": ["g1"]},
    )
    providers = resolve_providers(cfg)
    assert set(providers) == {"c1", "g1"}
    assert isinstance(providers["c1"], CodexProvider)
    assert isinstance(providers["g1"], GeminiProvider)


def test_build_router_builds_dual_chains():
    cfg = AppConfig(
        providers=[
            ProviderEntryConfig(name="c1", type="codex"),
            ProviderEntryConfig(name="g1", type="gemini"),
        ],
        vision={
            "image_chain": ["c1", "g1"],
            "classifier_chain": ["g1"],
        },
    )
    router = build_router(cfg)
    assert router.image_chain_names() == ["c1", "g1"]
    assert router.classifier_chain_names() == ["g1"]


def test_rate_limit_wired_into_provider():
    cfg = AppConfig()
    p = build_provider(
        ProviderEntryConfig(
            name="g", type="gemini", rate_limit={"rpm": 10, "concurrency": 2}
        ),
        cfg,
    )
    assert p._limiter is not None
    assert p._limiter.rpm == 10 and p._limiter.concurrency == 2


def test_no_rate_limit_by_default():
    cfg = AppConfig()
    p = build_provider(ProviderEntryConfig(name="g", type="gemini"), cfg)
    assert p._limiter is None


def test_opencode_is_api_provider_with_key_env(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "k-test")
    cfg = AppConfig()
    p = build_provider(ProviderEntryConfig(name="oc", type="opencode"), cfg)
    assert isinstance(p, OpenCodeProvider)
    assert p._api_key == "k-test"


def test_volcengine_key_env(monkeypatch):
    monkeypatch.setenv("VOLCENGINE_API_KEY", "k-test")
    cfg = AppConfig()
    p = build_provider(ProviderEntryConfig(name="vc", type="volcengine"), cfg)
    assert isinstance(p, VolcengineProvider)
    assert p.api_key == "k-test"