"""Vision router: ordered chain of providers with fallback.

The router knows nothing about concrete providers - it only sees the
:class:`~lm_visual_mcp.vision.providers.base.VisionProvider` interface and the
ordered chain handed to it (built from configuration by the provider registry).
A provider that fails with a fallback-eligible reason (including its own
``RATE_LIMITED`` raised by the per-provider rate limiter) is skipped and the
next provider in the chain serves the request.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Sequence

from ..errors import AllProvidersFailedError, ProviderUnavailableError
from .providers.base import VisionProvider
from .types import (
    FallbackRecord,
    ImageRequest,
    ProviderFailureReason,
    ProviderResult,
    ProviderStatus,
)

logger = logging.getLogger("lm_visual_mcp.vision.router")


@dataclass
class RoutedResult:
    provider: str
    model: Optional[str]
    result: dict
    usage: dict
    fallbacks: list[FallbackRecord] = field(default_factory=list)
    duration_ms: float = 0.0


class VisionRouter:
    def __init__(
        self,
        providers: Sequence[VisionProvider],
        *,
        fallback_enabled: bool = True,
        fallback_on: Optional[set[ProviderFailureReason]] = None,
    ) -> None:
        self.providers = list(providers)
        self.fallback_enabled = fallback_enabled
        self.fallback_on = (
            fallback_on
            if fallback_on is not None
            else ProviderFailureReason.fallback_eligible_defaults()
        )

    # -- public API ----------------------------------------------------------
    async def analyze(self, request: ImageRequest) -> RoutedResult:
        if not self.providers:
            raise AllProvidersFailedError("no enabled providers configured")

        fallbacks: list[FallbackRecord] = []
        start = time.monotonic()
        logger.info(
            "router.analyze start, provider chain=%s (images=%d)",
            [p.name for p in self.providers],
            len(request.images),
        )

        for provider in self.providers:
            status = await provider.probe(request)
            if not status.available:
                record = FallbackRecord(
                    provider.name,
                    status.reason.value if status.reason else "unavailable",
                    status.message or "unavailable",
                )
                fallbacks.append(record)
                logger.info("provider %s unavailable: %s", provider.name, status.message)
                if not self.fallback_enabled:
                    break
                continue

            at = time.monotonic()
            try:
                logger.info(
                    "provider %s analyze start (model=%s)",
                    provider.name, getattr(provider, "model", None),
                )
                result = await provider.analyze(request)
                logger.info(
                    "provider %s analyze ok in %.0fms",
                    provider.name, (time.monotonic() - at) * 1000.0,
                )
            except ProviderUnavailableError as exc:
                logger.warning(
                    "provider %s analyze FAILED in %.0fms: %s",
                    provider.name, (time.monotonic() - at) * 1000.0, exc.message,
                )
                if not self.fallback_enabled or not self._is_fallback_eligible(exc):
                    raise
                fallbacks.append(FallbackRecord(provider.name, exc.reason.value, exc.message))
                logger.info("provider %s failed, falling back: %s", provider.name, exc.message)
                continue

            duration_ms = (time.monotonic() - start) * 1000.0
            return self._routed(result, fallbacks, duration_ms)

        if fallbacks:
            detail = "; ".join(f"{f.provider}:{f.reason}" for f in fallbacks)
            raise AllProvidersFailedError(f"all providers failed: {detail}")
        raise AllProvidersFailedError("no provider could serve the request")

    def _is_fallback_eligible(self, exc: ProviderUnavailableError) -> bool:
        return exc.reason in self.fallback_on

    @staticmethod
    def _routed(
        result: ProviderResult, fallbacks: list[FallbackRecord], duration_ms: float
    ) -> RoutedResult:
        return RoutedResult(
            provider=result.provider,
            model=result.model,
            result=result.result,
            usage=result.usage.to_dict(),
            fallbacks=fallbacks,
            duration_ms=round(duration_ms, 1),
        )

    # -- introspection --------------------------------------------------------
    async def status(self) -> list[ProviderStatus]:
        return [await p.probe() for p in self.providers]
