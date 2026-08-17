"""Provider registry: type -> class, plus config-driven construction.

The registry is the only place that knows concrete provider implementations.
Configuration declares providers as top-level ``{name, type, ...}`` entries; the
dual chains (``image_chain`` / ``classifier_chain``) reference them by ``name``.
Adding a provider means implementing the class, adding one registry line, and
referencing it from configuration - the router and everything above it stay
provider-neutral.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from ..errors import ConfigError
from .agy import AgyProvider
from .base import Provider
from .codex import CodexProvider
from .gemini import GeminiProvider
from .opencode import DEFAULT_API_KEY_ENV as OPENCODE_API_KEY_ENV
from .opencode import OpenCodeProvider
from .ratelimit import RateLimiter
from .router import ProviderRouter
from .runner import SubprocessRunner
from .volcengine import DEFAULT_API_KEY_ENV as VOLCENGINE_API_KEY_ENV
from .volcengine import VolcengineProvider

if TYPE_CHECKING:  # pragma: no cover - type annotations only (breaks config cycle)
    from ..config import AppConfig, ProviderEntryConfig

#: Registry: provider ``type`` (config key) -> class.
PROVIDER_TYPES: dict[str, type] = {
    "agy": AgyProvider,
    "codex": CodexProvider,
    "gemini": GeminiProvider,
    "opencode": OpenCodeProvider,
    "volcengine": VolcengineProvider,
}

#: Default env-var name per type for API-key-based providers.
_DEFAULT_API_KEY_ENV = {
    "gemini": "GEMINI_API_KEY",
    "opencode": OPENCODE_API_KEY_ENV,
    "volcengine": VOLCENGINE_API_KEY_ENV,
}

#: Provider types that implement classifier capability (rewrite_*). Only these
#: read ``disable_thinking``; the local CLI providers (agy/codex) do not.
_CLASSIFIER_CAPABLE = {"gemini", "opencode", "volcengine"}
#: Provider types that accept a ``mode`` dialect selector.
_MODE_CAPABLE = {"opencode", "volcengine"}
#: CLI subprocess providers.
_CLI_TYPES = {"agy", "codex"}


def build_limiter(entry: ProviderEntryConfig) -> Optional[RateLimiter]:
    rl = entry.rate_limit
    if rl.rpm is None and rl.concurrency is None:
        return None
    return RateLimiter(rpm=rl.rpm, concurrency=rl.concurrency)


def build_provider(entry: ProviderEntryConfig, cfg: AppConfig, runner=None) -> Provider:
    """Construct a single provider from its config entry."""
    cls = PROVIDER_TYPES.get(entry.type)
    if cls is None:
        raise ConfigError(f"unknown provider type: {entry.type!r}")
    limiter = build_limiter(entry)
    timeout = entry.timeout or cfg.vision.timeout
    common = dict(model=entry.model, effort=entry.effort, timeout=timeout, limiter=limiter)

    if entry.type in _CLI_TYPES:
        common["command"] = entry.command
        if runner is not None:
            common["runner"] = runner
    if entry.type == "agy":
        common["vision_cache_ttl"] = entry.vision_cache_ttl
    if entry.type == "codex":
        common["sandbox"] = entry.sandbox
    if entry.type in _DEFAULT_API_KEY_ENV:
        api_key = entry.effective_api_key(_DEFAULT_API_KEY_ENV[entry.type])
        common["api_key"] = api_key
    if entry.type in _CLASSIFIER_CAPABLE:
        common["disable_thinking"] = entry.disable_thinking
    if entry.type in _MODE_CAPABLE:
        common["mode"] = entry.mode
        common["base_url"] = entry.base_url

    provider = cls(**common)
    # Registry name (from config) wins over the class default so multiple
    # instances of the same type stay distinguishable in results/logs.
    provider.name = entry.name
    return provider


def resolve_providers(cfg: AppConfig, runner=None) -> dict[str, Provider]:
    """Build every enabled top-level provider instance, keyed by name."""
    providers: dict[str, Provider] = {}
    for entry in cfg.providers:
        if not entry.enabled:
            continue
        providers[entry.name] = build_provider(entry, cfg, runner=runner)
    return providers


def build_router(cfg: AppConfig, runner=None) -> ProviderRouter:
    """Build the shared provider router using the dual configured chains."""
    providers = resolve_providers(cfg, runner=runner)
    return ProviderRouter(
        providers,
        image_chain=cfg.vision.image_chain,
        classifier_chain=cfg.vision.classifier_chain,
        fallback_enabled=cfg.vision.fallback.enabled,
        fallback_on=cfg.vision.fallback.reasons(),
    )


__all__ = [
    "Provider",
    "AgyProvider",
    "CodexProvider",
    "GeminiProvider",
    "OpenCodeProvider",
    "VolcengineProvider",
    "ProviderRouter",
    "PROVIDER_TYPES",
    "build_provider",
    "resolve_providers",
    "build_router",
    "SubprocessRunner",
]