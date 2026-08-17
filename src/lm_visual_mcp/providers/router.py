"""Provider router: ordered dual chains with fallback.

The router knows nothing about concrete providers - it only sees the
:class:`~lm_visual_mcp.providers.base.Provider` interface and the ordered chains
handed to it (built from configuration by build_chain in the provider registry).

The router does NOT dispatch by capability. Image and classifier each walk their
own configured chain:

- **IMAGE** (``analyze_image``) walks ``image_chain`` in order, "first success
  wins": a provider that fails with a fallback-eligible reason (including its own
  ``RATE_LIMITED``) is skipped and the next provider serves the request.
- **CLASSIFIER** (``classify_request`` / ``classify_response``) walks
  ``classifier_chain`` in order, "first changed wins": the first provider whose
  rewrite returns ``changed=True`` takes effect; providers without classifier
  capability return ``(body, False)`` (transparent passthrough) so they are
  transparently skipped. If no provider changed anything the request/response is
  passed through verbatim - the router performs no built-in normalization.

``providers`` is a ``{name: Provider}`` mapping of the top-level configured
instances; the chains are ordered name lists referencing it. A chain entry that
is absent from the mapping is skipped without error.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from ..errors import AllProvidersFailedError, ProviderUnavailableError
from .base import Provider
from .types import (
    ClassifierResult,
    FallbackRecord,
    ImageRequest,
    ProviderFailureReason,
    ProviderRequest,
    ProviderResponse,
    ProviderResult,
    ProviderStatus,
)

logger = logging.getLogger("lm_visual_mcp.providers.router")


@dataclass
class RoutedResult:
    provider: str
    model: Optional[str]
    result: dict
    usage: dict
    fallbacks: list[FallbackRecord] = field(default_factory=list)
    duration_ms: float = 0.0


class ProviderRouter:
    def __init__(
        self,
        providers: Mapping[str, Provider],
        *,
        image_chain: Sequence[str],
        classifier_chain: Sequence[str],
        fallback_enabled: bool = True,
        fallback_on: Optional[set[ProviderFailureReason]] = None,
    ) -> None:
        self.providers = dict(providers)
        self.image_chain = self._resolve(image_chain)
        self.classifier_chain = self._resolve(classifier_chain)
        self.fallback_enabled = fallback_enabled
        self.fallback_on = (
            fallback_on
            if fallback_on is not None
            else ProviderFailureReason.fallback_eligible_defaults()
        )

    def _resolve(self, chain: Sequence[str]) -> list[Provider]:
        """Resolve a chain of names to Provider instances, skipping unknowns."""
        resolved: list[Provider] = []
        for name in chain:
            prov = self.providers.get(name)
            if prov is None:
                logger.warning("chain references undefined provider %r; skipping", name)
                continue
            resolved.append(prov)
        return resolved

    # -- IMAGE ---------------------------------------------------------------
    async def analyze_image(self, request: ImageRequest) -> RoutedResult:
        if not self.image_chain:
            raise AllProvidersFailedError("no enabled image providers configured")

        fallbacks: list[FallbackRecord] = []
        start = time.monotonic()
        logger.info(
            "router.analyze_image start, image_chain=%s (images=%d)",
            [p.name for p in self.image_chain],
            len(request.images),
        )

        for provider in self.image_chain:
            status = await provider.probe_image(request)
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
                result = await provider.analyze_image(request)
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

    # -- CLASSIFIER ----------------------------------------------------------
    async def classify_request(
        self, request: ProviderRequest
    ) -> tuple[ProviderRequest, Optional[str]]:
        """Walk ``classifier_chain``; first ``changed`` provider takes effect.

        Returns ``(request, provider_name_or_None)``. The caller (ClassiferHook)
        remembers the provider name in its state so the response pass delegates
        to classifier handling only when a request rewrite actually happened.
        """
        current = request
        for provider in self.classifier_chain:
            try:
                body, changed = await provider.rewrite_classifier_request(current)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "provider %s classifier request rewrite failed: %s",
                    provider.name, exc,
                )
                continue
            if changed:
                logger.info("provider %s rewrote classifier request", provider.name)
                return _with_body(current, body), provider.name
        return current, None

    async def classify_response(
        self, response: ProviderResponse
    ) -> tuple[ProviderResponse, Optional[str]]:
        """Walk ``classifier_chain``; first ``changed`` provider takes effect."""
        current = response
        for provider in self.classifier_chain:
            try:
                body, changed = await provider.rewrite_classifier_response(current)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "provider %s classifier response rewrite failed: %s",
                    provider.name, exc,
                )
                continue
            if changed:
                logger.info("provider %s rewrote classifier response", provider.name)
                current = _with_body(current, body)
                return current, provider.name
        return current, None

    async def classifier_verdict(self, request: ProviderRequest) -> Optional[ClassifierResult]:
        """Walk ``classifier_chain``; first provider whose own model returns a
        definitive verdict wins (real-inference short-circuit).

        Providers without the ``classify`` capability (CLI agents, base) are
        skipped. Any exception or ambiguous result advances the chain. Returns
        ``None`` when no provider produced a verdict - the caller then falls
        back to the byte-rewrite/forward path.
        """
        for provider in self.classifier_chain:
            classify = getattr(provider, "classify", None)
            if classify is None:
                continue
            try:
                # ``classify_image`` applies the provider's shared rate limiter
                # (same per-provider quota as image analysis), returning None on
                # rate-limit or when the provider yields no verdict.
                result = await provider.classify_image(request)
            except Exception as exc:  # noqa: BLE001 - advance the chain
                logger.warning("provider %s classify failed: %s", provider.name, exc)
                continue
            if result is not None:
                logger.info("provider %s classified: %s", provider.name, result.verdict)
                return result
        return None

    # -- introspection --------------------------------------------------------
    async def status(self) -> list[ProviderStatus]:
        results: list[ProviderStatus] = []
        for name in self.providers:
            results.append(await self.providers[name].probe_image())
        return results

    def image_chain_names(self) -> list[str]:
        return [p.name for p in self.image_chain]

    def classifier_chain_names(self) -> list[str]:
        return [p.name for p in self.classifier_chain]


def _with_body(request_or_response, body: bytes):
    """Return a copy of the request/response with a new body."""
    if isinstance(request_or_response, ProviderRequest):
        return ProviderRequest(
            protocol=request_or_response.protocol,
            url=request_or_response.url,
            model=request_or_response.model,
            headers=request_or_response.headers,
            body=body,
        )
    return ProviderResponse(
        url=request_or_response.url,
        status=request_or_response.status,
        headers=request_or_response.headers,
        body=body,
    )