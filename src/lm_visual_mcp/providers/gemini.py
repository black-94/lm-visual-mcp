"""Gemini API provider (google-genai).

Never accepts an API key from a tool call - the key is resolved from
configuration (SecretStr / api_key_env / GEMINI_API_KEY) and redacted
everywhere.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("lm_visual_mcp.providers.gemini")

from ..errors import ProviderUnavailableError
from .base import Provider
from .classifier import (
    classifier_text_messages,
    disable_auto_classifier_thinking,
    extract_verdict,
    normalize_auto_classifier_response,
)
from .ratelimit import RateLimiter
from .schema import normalize_result
from .types import (
    ClassifierResult,
    ImageRequest,
    ProviderFailureReason,
    ProviderRequest,
    ProviderResponse,
    ProviderResult,
    ProviderStatus,
    ProviderUsage,
)

DEFAULT_MODEL = "gemini-2.0-flash"


class GeminiProvider(Provider):
    name = "gemini"

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 120.0,
        disable_thinking: Optional[bool] = None,
        client=None,
        limiter: Optional[RateLimiter] = None,
    ) -> None:
        super().__init__(limiter=limiter)
        self.model = model or DEFAULT_MODEL
        self.effort = effort
        self._api_key = api_key
        self.timeout = timeout
        self.disable_thinking = disable_thinking
        # client injectable for tests.
        self._client = client

    # -- client ------------------------------------------------------------
    def _get_client(self):
        if self._client is not None:
            return self._client
        from google import genai

        return genai.Client(api_key=self._api_key)

    # -- probe -------------------------------------------------------------
    async def probe_image(self, request: Optional[ImageRequest] = None) -> ProviderStatus:
        if not self._api_key:
            return ProviderStatus(
                name=self.name,
                available=False,
                reason=ProviderFailureReason.API_KEY_MISSING,
                message="gemini API key is not configured",
            )
        return ProviderStatus(
            name=self.name,
            available=True,
            model=self.model,
            vision_capability="available",
        )

    # -- analyze -----------------------------------------------------------
    async def _analyze_image(self, request: ImageRequest) -> ProviderResult:
        if not self._api_key:
            raise ProviderUnavailableError(
                ProviderFailureReason.API_KEY_MISSING,
                "gemini API key is not configured",
            )
        try:
            response = await self._generate(request)
        except Exception as exc:  # noqa: BLE001
            raise self._classify(exc) from exc

        text = _safe_getattr(response, "text", None)
        if not text:
            raise ProviderUnavailableError(
                ProviderFailureReason.TEMPORARY_FAILURE,
                "gemini returned no text",
            )
        try:
            import json

            data = json.loads(text)
        except json.JSONDecodeError:
            data = {"answer": text.strip()}
        if not isinstance(data, dict):
            data = {"answer": str(data)}
        normalized = normalize_result(data)
        usage = self._extract_usage(response)
        return ProviderResult(
            provider=self.name,
            result=normalized.to_dict(),
            model=self.model,
            usage=usage,
            raw=text,
        )

    async def _generate(self, request: ImageRequest):
        from google import genai

        client = self._get_client()
        parts = []
        for img in request.images:
            if img.local_path:
                data = Path(img.local_path).read_bytes()
                parts.append(
                    genai.types.Part.from_bytes(
                        data=data, mime_type=img.mime_type or "image/png"
                    )
                )
            elif img.url:
                parts.append(genai.types.Part.from_uri(file_uri=img.url, mime_type=img.mime_type or "image/png"))
        if request.user_prompt:
            parts.append(request.user_prompt)

        config = genai.types.GenerateContentConfig(
            system_instruction=request.system_prompt,
            response_mime_type="application/json",
        )
        if request.output_schema:
            config.response_schema = _gemini_schema(request.output_schema)
        if self.effort:
            _EFFORT_MAP = {"low": "LOW", "medium": "MEDIUM", "high": "HIGH", "xhigh": "HIGH"}
            level = _EFFORT_MAP.get(self.effort.lower(), self.effort.upper())
            config.thinking_config = genai.types.ThinkingConfig(thinking_level=level)

        return await client.aio.models.generate_content(
            model=self.model,
            contents=parts,
            config=config,
        )

    @staticmethod
    def _extract_usage(response) -> ProviderUsage:
        meta = _safe_getattr(response, "usage_metadata", None)
        if meta is None:
            return ProviderUsage()
        return ProviderUsage(
            input_tokens=_num(_safe_getattr(meta, "prompt_token_count", None)),
            output_tokens=_num(_safe_getattr(meta, "candidates_token_count", None)),
            total_tokens=_num(_safe_getattr(meta, "total_token_count", None)),
            cache_read_tokens=_num(_safe_getattr(meta, "cached_content_token_count", None)),
        )

    @staticmethod
    def _classify(exc: Exception) -> ProviderUnavailableError:
        msg = str(exc).lower()
        if "api key" in msg and ("invalid" in msg or "not valid" in msg or "missing" in msg):
            return ProviderUnavailableError(
                ProviderFailureReason.API_KEY_MISSING, "gemini API key rejected"
            )
        if "permission" in msg or "forbidden" in msg or "403" in msg:
            return ProviderUnavailableError(
                ProviderFailureReason.PERMISSION_DENIED, "gemini permission denied"
            )
        if "quota" in msg or "429" in msg or "resource exhausted" in msg or "rate limit" in msg:
            return ProviderUnavailableError(
                ProviderFailureReason.QUOTA_EXHAUSTED, "gemini quota exhausted"
            )
        if "model" in msg and ("not found" in msg or "does not support" in msg):
            return ProviderUnavailableError(
                ProviderFailureReason.INVALID_MODEL, "gemini model invalid"
            )
        return ProviderUnavailableError(
            ProviderFailureReason.TEMPORARY_FAILURE, f"gemini request failed"
        )


# -- classifier ----------------------------------------------------------
    async def rewrite_classifier_request(
        self, request: ProviderRequest
    ) -> tuple[bytes, bool]:
        if not self.disable_thinking:
            return request.body, False
        return disable_auto_classifier_thinking(request.body)

    async def rewrite_classifier_response(
        self, response: ProviderResponse
    ) -> tuple[bytes, bool]:
        return normalize_auto_classifier_response(response.body)

    async def classify(self, request: ProviderRequest) -> "Optional[ClassifierResult]":
        """Call gemini's own backend model for a classifier verdict.

        Uses the google-genai SDK with this provider's configured ``model``.
        Returns ``None`` on any failure or an ambiguous verdict so the router can
        fall through.
        """
        if not self._api_key:
            return None
        system, turns = classifier_text_messages(request.body)
        if not turns:
            return None
        from google import genai

        client = self._get_client()
        kwargs: dict = {}
        if system:
            kwargs["system_instruction"] = system
        try:
            response = await client.aio.models.generate_content(
                model=self.model,
                contents=[
                    {"role": "model" if role == "assistant" else role, "parts": [{"text": text}]}
                    for role, text in turns
                ],
                config=genai.types.GenerateContentConfig(**kwargs),
            )
            text = _safe_getattr(response, "text", None) or ""
        except Exception:  # noqa: BLE001 - never break the classifier chain
            logger.warning("gemini classifier inference failed", exc_info=True)
            return None
        verdict = extract_verdict(text)
        if verdict is None:
            return None
        return ClassifierResult(
            provider=self.name, model=self.model, verdict=verdict, raw=text
        )


def _gemini_schema(schema: dict) -> dict:
    """Sanitize a JSON Schema for the Gemini Developer API ``response_schema``.

    The shared ``VISION_RESULT_SCHEMA`` (and describe schema) carry
    ``additionalProperties``; Gemini's Developer API rejects that keyword
    outright (it is Enterprise-Platform-only) and would error with a schema
    that is otherwise valid. Strip it recursively so the shared schema can be
    used directly, mirroring codex's strict ``build_codex_schema``.
    """
    if isinstance(schema, dict):
        return {k: _gemini_schema(v) for k, v in schema.items() if k != "additionalProperties"}
    if isinstance(schema, list):
        return [_gemini_schema(item) for item in schema]
    return schema


def _safe_getattr(obj, name, default=None):
    val = getattr(obj, name, None)
    return val if val is not None else default


def _num(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
