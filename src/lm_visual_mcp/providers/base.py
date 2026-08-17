"""Provider base class with shared infrastructure and two behavior groups.

Two behavior groups, each a pass-through point the subclasses override only when
they implement that capability:

- **IMAGE** (``probe_image`` / ``analyze_image`` / ``_analyze_image``): every
  provider that can analyze images implements ``_analyze_image``; ``analyze_image``
  is a rate-limited template. Base ``probe_image`` reports available - subclasses
  override when they need a real check. Default base image behavior is
  *not available* - there is no generic image algorithm here.
- **CLASSIFIER** (``rewrite_classifier_request`` / ``rewrite_classifier_response``):
  the default is transparent passthrough (body returned unchanged, ``changed=False``).
  Only providers that explicitly implement classifier handling override these.
  The shared algorithms live in :mod:`.classifier` for those providers to reuse.

No algorithm default is baked into this class - it carries only the machinery
every provider shares (its own per-provider rate limiter, the optional ``mode``
selector for vendor providers) and declares the behavior method signatures.
"""

from __future__ import annotations

from typing import Optional

from ..errors import ProviderUnavailableError
from .ratelimit import RateLimiter
from .types import (
    ImageRequest,
    ProviderFailureReason,
    ProviderRequest,
    ProviderResponse,
    ProviderResult,
    ProviderStatus,
)


class Provider:
    """Base class for concrete providers; owns its rate limiter."""

    name: str = "provider"

    def __init__(
        self,
        *,
        limiter: Optional[RateLimiter] = None,
        mode: Optional[str] = None,
    ) -> None:
        self._limiter = limiter
        self._mode = mode

    # -- IMAGE behavior group ----------------------------------------------
    # The honest default: base has no image algorithm, so it is not available.
    async def probe_image(self, request: Optional[ImageRequest] = None) -> ProviderStatus:
        return ProviderStatus(name=self.name, available=True)

    async def analyze_image(self, request: ImageRequest) -> ProviderResult:
        """Rate-limited template; final, not meant to be overridden.

        When a limiter is set, a full limit raises ``RATE_LIMITED`` so the
        router falls back immediately instead of queueing.
        """
        if self._limiter is None:
            return await self._analyze_image(request)
        if not self._limiter.try_acquire():
            raise ProviderUnavailableError(
                ProviderFailureReason.RATE_LIMITED,
                f"{self.name} rate limit reached (rpm/concurrency)",
            )
        try:
            return await self._analyze_image(request)
        finally:
            self._limiter.release()

    async def _analyze_image(self, request: ImageRequest) -> ProviderResult:
        raise NotImplementedError

    # -- CLASSIFIER behavior group -----------------------------------------
    # Transparent passthrough by default. Only providers that explicitly
    # implement classifier handling override these (see providers/classifier.py).
    async def rewrite_classifier_request(
        self, request: ProviderRequest
    ) -> tuple[bytes, bool]:
        return request.body, False

    async def rewrite_classifier_response(
        self, response: ProviderResponse
    ) -> tuple[bytes, bool]:
        return response.body, False