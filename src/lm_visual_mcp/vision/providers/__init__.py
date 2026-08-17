"""Provider registry: type name -> provider class.

The registry is the only place that knows concrete provider implementations.
Configuration declares providers as ``{name, type, ...}`` entries; the chain is
built by looking up ``type`` here. Adding a provider means implementing the
class, adding one registry line, and referencing it from configuration - the
router and everything above it stay provider-neutral.
"""

from __future__ import annotations

from typing import Optional

from ...config import ProviderEntryConfig, VisionConfig
from ...errors import ConfigError
from .agy import AgyProvider
from .base import VisionProvider
from .codex import CodexProvider
from .gemini import GeminiProvider
from .opencode import DEFAULT_API_KEY_ENV as OPENCODE_API_KEY_ENV
from .opencode import OpenCodeProvider
from .ratelimit import RateLimiter
from .runner import SubprocessRunner

#: Registry: provider ``type`` (config key) -> class.
PROVIDER_TYPES: dict[str, type] = {
    "agy": AgyProvider,
    "codex": CodexProvider,
    "gemini": GeminiProvider,
    "opencode": OpenCodeProvider,
}

#: Default env-var name per type for API-key-based providers.
_DEFAULT_API_KEY_ENV = {
    "gemini": "GEMINI_API_KEY",
    "opencode": OPENCODE_API_KEY_ENV,
}


def build_limiter(entry: ProviderEntryConfig) -> Optional[RateLimiter]:
    rl = entry.rate_limit
    if rl.rpm is None and rl.concurrency is None:
        return None
    return RateLimiter(rpm=rl.rpm, concurrency=rl.concurrency)


def build_provider(entry: ProviderEntryConfig, cfg: VisionConfig, runner=None) -> VisionProvider:
    """Construct a single provider from its config entry."""
    cls = PROVIDER_TYPES.get(entry.type)
    if cls is None:
        raise ConfigError(f"unknown provider type: {entry.type!r}")
    limiter = build_limiter(entry)
    timeout = entry.timeout or cfg.timeout
    common = dict(model=entry.model, effort=entry.effort, timeout=timeout, limiter=limiter)

    if entry.type in ("agy", "codex"):
        common["command"] = entry.command
        if runner is not None:
            common["runner"] = runner
    if entry.type == "agy":
        common["vision_cache_ttl"] = entry.vision_cache_ttl
    if entry.type == "codex":
        common["sandbox"] = entry.sandbox
    if entry.type in ("gemini", "opencode"):
        api_key = entry.effective_api_key(_DEFAULT_API_KEY_ENV[entry.type])
        common["api_key"] = api_key
    if entry.type == "opencode":
        common["base_url"] = entry.base_url

    provider = cls(**common)
    # Registry name (from config) wins over the class default so multiple
    # instances of the same type stay distinguishable in results/logs.
    provider.name = entry.name
    return provider


def build_chain(cfg: VisionConfig, runner=None) -> list[VisionProvider]:
    """Build the ordered provider chain (the fallback order)."""
    names = [e.name for e in cfg.providers]
    if len(set(names)) != len(names):
        raise ConfigError("vision.providers contains duplicate names")
    chain: list[VisionProvider] = []
    for entry in cfg.providers:
        if not entry.enabled:
            continue
        chain.append(build_provider(entry, cfg, runner=runner))
    return chain


__all__ = [
    "VisionProvider",
    "AgyProvider",
    "CodexProvider",
    "GeminiProvider",
    "OpenCodeProvider",
    "PROVIDER_TYPES",
    "build_provider",
    "build_chain",
    "SubprocessRunner",
]
