"""Error hierarchy for vision_mcp.

All errors raised by the server inherit from :class:`VisionError` so the MCP
layer can translate them into a single, safe, redacted message. Sensitive
details (API keys, tokens, full credential-bearing stderr) must never appear
in exception messages.
"""

from __future__ import annotations

from typing import Optional

from .models import ProviderFailureReason


class VisionError(Exception):
    """Base class for all vision_mcp errors."""


class ConfigError(VisionError):
    """Invalid, missing or conflicting server configuration."""


class MediaError(VisionError):
    """Media resolution / validation / download failure."""


class ProviderError(VisionError):
    """A provider raised an error that is not eligible for fallback."""


class ProviderUnavailableError(ProviderError):
    """A provider could not handle the request.

    Carries a :class:`ProviderFailureReason` and a safe human-readable message.
    The router decides whether this is eligible for fallback.
    """

    def __init__(
        self,
        reason: ProviderFailureReason,
        message: str,
        *,
        operable: bool = False,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        # operable=True means the provider is fundamentally broken for this
        # request (invalid model, config error, ...) and continuing to the next
        # provider is pointless.
        self.operable = operable

    def is_fallback_eligible(self, fallback_on: Optional[set[ProviderFailureReason]]) -> bool:
        if fallback_on is None:
            return self.reason in ProviderFailureReason.fallback_eligible_defaults()
        return self.reason in fallback_on


class AllProvidersFailedError(VisionError):
    """Every enabled provider rejected or failed the request (or none enabled)."""