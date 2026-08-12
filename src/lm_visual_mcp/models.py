"""Provider-neutral domain models.

These types are the *only* currency exchanged between tools, the router and
providers. Providers never see MCP tool names/schemas; tools never see
provider CLI details. This keeps the provider abstraction clean and lets us add
new providers (AcpProvider, ClaudeProvider, ...) without touching the tools.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union


class ProviderFailureReason(enum.StrEnum):
    """Why a provider failed or is unavailable."""

    # Fallback-eligible by default.
    COMMAND_NOT_FOUND = "command_not_found"
    NOT_AUTHENTICATED = "not_authenticated"
    API_KEY_MISSING = "api_key_missing"
    QUOTA_EXHAUSTED = "quota_exhausted"
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
class VideoInput:
    """A single video that the vision request should inspect."""

    source: str
    local_path: Optional[str] = None
    url: Optional[str] = None
    mime_type: Optional[str] = None

    @classmethod
    def from_source(cls, source: str) -> "VideoInput":
        return cls(source=source)


@dataclass
class VisionRequest:
    """Everything a provider needs to produce a structured visual analysis.

    ``system_prompt`` and ``user_prompt`` are already fully specialized by the
    prompt layer — the provider must not invent its own framing.
    """

    system_prompt: str
    user_prompt: str

    images: list[ImageInput] = field(default_factory=list)
    videos: list[VideoInput] = field(default_factory=list)

    # JSON Schema for the final structured output (normalized to a plain dict).
    output_schema: Optional[dict] = None

    # Provider working directory (already prepared by the workspace service).
    workdir: Optional[Path] = None
    timeout: Optional[float] = None


@dataclass
class ProviderStatus:
    """Result of ``provider.probe(...)``."""

    name: str
    available: bool
    reason: Optional[ProviderFailureReason] = None
    message: Optional[str] = None
    model: Optional[str] = None
    # Local file agents support vision media; CLI agents may not.
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
    # The unified structured result (see lm_visual_mcp.schema.VisionResult).
    result: dict
    model: Optional[str] = None
    usage: ProviderUsage = field(default_factory=ProviderUsage)
    raw: Optional[str] = None  # raw provider output, kept for debugging only


@dataclass
class FallbackRecord:
    provider: str
    reason: str
    message: str


DEFAULT_PROVIDER_ORDER: tuple[str, ...] = (
    "agy",
    "codex",
    "gemini",
    "opencode",
)

#: Input types accepted by tools for image/video sources.
MediaSource = Union[ImageInput, VideoInput]