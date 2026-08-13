"""Provider implementations and registry."""

from __future__ import annotations

from typing import Optional

from ..config import ProviderConfig, AppConfig
from ..models import VisionRequest
from .base import VisionProvider
from .agy import AgyProvider
from .codex import CodexProvider
from .gemini import GeminiProvider
from .opencode import OpenCodeProvider

__all__ = [
    "VisionProvider",
    "AgyProvider",
    "CodexProvider",
    "GeminiProvider",
    "OpenCodeProvider",
    "build_provider",
]


def build_provider(name: str, provider_cfg: ProviderConfig, cfg: AppConfig, runner=None) -> VisionProvider:
    """Construct a single provider from its config block."""
    timeout = cfg.runtime.timeout
    effort = provider_cfg.effort
    if name == "agy":
        return AgyProvider(
            command=provider_cfg.command, model=provider_cfg.model, effort=effort,
            timeout=timeout, runner=runner,
        )
    if name == "codex":
        return CodexProvider(
            command=provider_cfg.command, model=provider_cfg.model, effort=effort,
            timeout=timeout, runner=runner,
        )
    if name == "gemini":
        import os
        api_key = provider_cfg.effective_api_key() or os.environ.get("GEMINI_API_KEY")
        return GeminiProvider(
            model=provider_cfg.model, effort=effort,
            api_key=api_key, timeout=timeout,
        )
    if name == "opencode":
        return OpenCodeProvider(
            command=provider_cfg.command, model=provider_cfg.model, effort=effort,
            timeout=timeout, runner=runner,
        )
    raise ValueError(f"unknown provider: {name}")


def build_registry(cfg: AppConfig, runner=None) -> dict[str, VisionProvider]:
    """Build the configured (enabled) providers keyed by name."""
    registry: dict[str, VisionProvider] = {}
    for name in cfg.providers.order:
        provider_cfg = cfg.providers.get(name)
        if provider_cfg is None:
            continue
        if not provider_cfg.enabled:
            continue
        registry[name] = build_provider(name, provider_cfg, cfg, runner=runner)
    return registry