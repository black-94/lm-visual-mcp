"""Provider base class with built-in per-provider rate limiting.

Every concrete provider extends :class:`Provider` and implements ``_analyze``.
The public ``analyze`` is a template: it first tries the provider's own rate
limiter (non-blocking) and raises ``RATE_LIMITED`` when a limit is hit, so the
router can immediately fall back to the next provider in the chain. Rate limits
therefore live inside each provider - each one may be configured differently.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from ...errors import ProviderUnavailableError
from ..types import (
    ImageRequest,
    ProviderFailureReason,
    ProviderResult,
    ProviderStatus,
)
from .ratelimit import RateLimiter


@runtime_checkable
class VisionProvider(Protocol):
    """A visual-analysis backend (agy, codex, gemini, opencode, ...)."""

    name: str

    async def probe(self, request: Optional[ImageRequest] = None) -> ProviderStatus:
        """Report whether this provider is available for ``request``."""
        ...

    async def analyze(self, request: ImageRequest) -> ProviderResult:
        """Analyze ``request`` and return the unified structured result."""
        ...


class Provider:
    """Base class for concrete providers; owns its rate limiter."""

    name: str = "provider"

    def __init__(self, *, limiter: Optional[RateLimiter] = None) -> None:
        self._limiter = limiter

    # -- public API (final) --------------------------------------------------
    async def analyze(self, request: ImageRequest) -> ProviderResult:
        if self._limiter is None:
            return await self._analyze(request)
        if not self._limiter.try_acquire():
            raise ProviderUnavailableError(
                ProviderFailureReason.RATE_LIMITED,
                f"{self.name} rate limit reached (rpm/concurrency)",
            )
        try:
            return await self._analyze(request)
        finally:
            self._limiter.release()

    async def probe(self, request: Optional[ImageRequest] = None) -> ProviderStatus:
        return ProviderStatus(name=self.name, available=True)

    # -- subclass hook -------------------------------------------------------
    async def _analyze(self, request: ImageRequest) -> ProviderResult:
        raise NotImplementedError
