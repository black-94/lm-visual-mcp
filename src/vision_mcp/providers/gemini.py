"""Gemini API provider (google-genai).

Never accepts an API key from a tool call — the key is resolved from
configuration (SecretStr / api_key_env / GEMINI_API_KEY) and redacted
everywhere.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Optional

from ..errors import ProviderUnavailableError
from ..models import ProviderFailureReason, ProviderResult, ProviderStatus, ProviderUsage, VisionRequest
from ..schema import VisionResult, normalize_result
from .base import VisionProvider

DEFAULT_MODEL = "gemini-2.0-flash"


class GeminiProvider(VisionProvider):
    name = "gemini"

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 120.0,
        client=None,
    ) -> None:
        self.model = model or DEFAULT_MODEL
        self._api_key = api_key
        self.timeout = timeout
        # client injectable for tests.
        self._client = client

    # -- client ------------------------------------------------------------
    def _get_client(self):
        if self._client is not None:
            return self._client
        from google import genai

        return genai.Client(api_key=self._api_key)

    # -- probe -------------------------------------------------------------
    async def probe(self, request: Optional[VisionRequest] = None) -> ProviderStatus:
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
    async def analyze(self, request: VisionRequest) -> ProviderResult:
        if not self._api_key:
            raise ProviderUnavailableError(
                ProviderFailureReason.API_KEY_MISSING,
                "gemini API key is not configured",
            )
        if request.videos:
            raise ProviderUnavailableError(
                ProviderFailureReason.UNSUPPORTED_MEDIA,
                "gemini video via VisionRequest is not supported in v1",
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

    async def _generate(self, request: VisionRequest):
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
            config.response_schema = request.output_schema

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
                ProviderFailureReason.NOT_AUTHENTICATED, "gemini permission denied"
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


def _safe_getattr(obj, name, default=None):
    for node in [obj]:
        val = getattr(node, name, None)
        if val is not None:
            return val
    return default


def _num(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None