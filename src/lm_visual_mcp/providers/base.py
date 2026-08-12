"""Provider abstractions.

:class:`VisionProvider` is the interface every provider implements. Providers
are 100% provider-neutral at the tool layer: they only ever see
:class:`VisionRequest` and return :class:`ProviderResult`.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from ..models import ProviderResult, ProviderStatus, VisionRequest


@runtime_checkable
class VisionProvider(Protocol):
    """A visual-analysis backend (AGY, Codex, Gemini, OpenCode, ...)."""

    name: str

    async def probe(self, request: Optional[VisionRequest] = None) -> ProviderStatus:
        """Report whether this provider is available for ``request``."""
        ...

    async def analyze(self, request: VisionRequest) -> ProviderResult:
        """Analyze ``request`` and return the unified structured result."""
        ...