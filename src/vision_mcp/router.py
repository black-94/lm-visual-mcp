"""Provider Router.

Iterates the configured provider order, probing each enabled provider and
analyzing with the first one that succeeds. Fallback policy is driven entirely
by server configuration — the LLM never selects a provider.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .config import AppConfig
from .errors import AllProvidersFailedError, ProviderUnavailableError
from .models import (
    DEFAULT_PROVIDER_ORDER,
    FallbackRecord,
    ProviderResult,
    ProviderStatus,
    VisionRequest,
)
from .providers import VisionProvider, build_registry

logger = logging.getLogger("vision_mcp.router")


@dataclass
class RoutedResponse:
    provider: str
    model: Optional[str]
    result: dict
    usage: dict
    fallbacks: list[FallbackRecord] = field(default_factory=list)
    duration_ms: float = 0.0


class ProviderRouter:
    def __init__(self, cfg: AppConfig, registry: Optional[dict[str, VisionProvider]] = None) -> None:
        self.cfg = cfg
        self.registry = registry if registry is not None else build_registry(cfg)
        self.order = [n for n in cfg.providers.order if n in self.registry]

    # -- public API --------------------------------------------------------
    async def route(self, request: VisionRequest) -> RoutedResponse:
        if not self.order:
            raise AllProvidersFailedError("no enabled providers configured")

        fallback_enabled = self.cfg.fallback.enabled
        fallback_on = self.cfg.fallback.reasons()
        fallbacks: list[FallbackRecord] = []
        start = time.monotonic()

        for name in self.order:
            provider = self.registry[name]
            status = await provider.probe(request)
            if not status.available:
                record = FallbackRecord(name, status.reason.value if status.reason else "unavailable",
                                        status.message or "unavailable")
                fallbacks.append(record)
                logger.info("provider %s unavailable: %s", name, status.message)
                if not fallback_enabled:
                    break
                continue

            try:
                result = await provider.analyze(request)
            except ProviderUnavailableError as exc:
                if not fallback_enabled or not exc.is_fallback_eligible(fallback_on):
                    raise
                fallbacks.append(FallbackRecord(name, exc.reason.value, exc.message))
                logger.info("provider %s failed, falling back: %s", name, exc.message)
                continue

            duration_ms = (time.monotonic() - start) * 1000.0
            return RoutedResponse(
                provider=result.provider,
                model=result.model,
                result=result.result,
                usage=result.usage.to_dict(),
                fallbacks=fallbacks,
                duration_ms=round(duration_ms, 1),
            )

        # None succeeded.
        if fallbacks:
            detail = "; ".join(f"{f.provider}:{f.reason}" for f in fallbacks)
            raise AllProvidersFailedError(f"all providers failed: {detail or 'no provider available'}")
        raise AllProvidersFailedError("no provider could serve the request")

    # -- introspection ------------------------------------------------------
    async def status(self) -> list[ProviderStatus]:
        out: list[ProviderStatus] = []
        for name in (self.cfg.providers.order or list(DEFAULT_PROVIDER_ORDER)):
            provider = self.registry.get(name)
            if provider is None:
                out.append(
                    ProviderStatus(name=name, available=False, reason=None,
                                   message="disabled or not configured")
                )
                continue
            status = await provider.probe()
            out.append(status)
        return out