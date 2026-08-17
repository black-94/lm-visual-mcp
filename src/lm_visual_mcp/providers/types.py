"""Provider-layer domain types (image + classifier).

These types are the only currency exchanged between the router and providers,
and between the request hooks and the router's classifier methods. The MCP
layer never sees provider details, and providers never see MCP tool names.

Image request/response objects carry the base info the behavior methods need:
an :class:`ImageRequest` names the originating ``model`` and ``source_protocol``
(if any) so providers can decide how to handle it; the classifier methods take
opaque ``ProviderRequest`` / ``ProviderResponse`` objects (protocol / url /
model / headers / body) instead of raw bytes.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


class ProviderFailureReason(enum.StrEnum):
    """Why a provider failed or is unavailable."""

    # Fallback-eligible by default.
    COMMAND_NOT_FOUND = "command_not_found"
    NOT_AUTHENTICATED = "not_authenticated"
    API_KEY_MISSING = "api_key_missing"
    QUOTA_EXHAUSTED = "quota_exhausted"
    RATE_LIMITED = "rate_limited"
    UNSUPPORTED_MEDIA = "unsupported_media"
    TIMEOUT = "timeout"
    TEMPORARY_FAILURE = "temporary_failure"
    PERMISSION_DENIED = "permission_denied"

    # NOT fallback-eligible by default.
    INVALID_INPUT = "invalid_input"
    INVALID_MODEL = "invalid_model"
    CONFIG_ERROR = "config_error"

    @classmethod
    def fallback_eligible_defaults(cls) -> "set[ProviderFailureReason]":
        return {
            cls.COMMAND_NOT_FOUND,
            cls.NOT_AUTHENTICATED,
            cls.API_KEY_MISSING,
            cls.QUOTA_EXHAUSTED,
            cls.RATE_LIMITED,
            cls.UNSUPPORTED_MEDIA,
            cls.TIMEOUT,
            cls.TEMPORARY_FAILURE,
            cls.PERMISSION_DENIED,
        }


@dataclass
class ImageInput:
    """A single image that the vision request should inspect."""

    source: str
    local_path: Optional[str] = None
    url: Optional[str] = None
    mime_type: Optional[str] = None

    @classmethod
    def from_source(cls, source: str) -> "ImageInput":
        return cls(source=source)


@dataclass
class ImageRequest:
    """Everything a provider needs to produce a structured image analysis.

    ``system_prompt`` and ``user_prompt`` are already fully specialized by the
    prompt layer - the provider must not invent its own framing.

    ``model`` (when set) is the model of the proxied request that this image
    work originated from (filled by the image hook); ``source_protocol`` the
    protocol it arrived over ("anthropic" | "openai" | ...). Providers may use
    these to specialize behavior; ``None`` means "no originating request".
    """

    system_prompt: str
    user_prompt: str
    images: list[ImageInput] = field(default_factory=list)

    # JSON Schema for the final structured output (normalized to a plain dict).
    output_schema: Optional[dict] = None

    # Provider working directory (already prepared by the caller).
    workdir: Optional[Path] = None
    timeout: Optional[float] = None

    # Originating request context (image hook only; None for direct MCP calls).
    model: Optional[str] = None
    source_protocol: Optional[str] = None


@dataclass
class ProviderStatus:
    """Result of ``provider.probe_image(...)``."""

    name: str
    available: bool
    reason: Optional[ProviderFailureReason] = None
    message: Optional[str] = None
    model: Optional[str] = None
    vision_capability: Optional[str] = None  # "available" | "unsupported" | "unknown"


@dataclass
class ProviderUsage:
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    thinking_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    total_tokens: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "thinking_tokens": self.thinking_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class ProviderResult:
    """Structured output + usage returned by a single provider."""

    provider: str
    result: dict
    model: Optional[str] = None
    usage: ProviderUsage = field(default_factory=ProviderUsage)
    raw: Optional[str] = None  # raw provider output, kept for debugging only


@dataclass
class FallbackRecord:
    provider: str
    reason: str
    message: str


@dataclass
class ClassifierResult:
    """A classifier verdict produced by a provider's own model.

    ``verdict`` is the decisive ``"yes"`` / ``"no"`` (``"yes"`` = takeover /
    safety violation detected). ``raw`` keeps the provider's original output for
    debugging. An ambiguous / failed call must return ``None`` instead of a
    result - callers must never invent a safety decision.
    """

    provider: str
    model: Optional[str]
    verdict: str
    raw: Optional[str] = None


@dataclass
class ProviderRequest:
    """A classifier request handed to ``Provider.rewrite_classifier_request``.

    ``protocol`` is the proxy protocol path (e.g. "anthropic"),
    ``url`` the upstream target, ``model`` the request's target model, and
    ``body`` the raw (still-encoded) request bytes.
    """

    protocol: str
    url: str
    model: str
    headers: dict[str, str]
    body: bytes


@dataclass
class ProviderResponse:
    """A classifier response handed to ``Provider.rewrite_classifier_response``."""

    url: str
    status: int
    headers: dict[str, str]
    body: bytes