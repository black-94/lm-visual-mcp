"""OpenCode provider (direct API, no local CLI).

Calls an OpenAI-compatible chat-completions endpoint (by default the opencode
GO-plan cloud endpoint ``https://opencode.ai/zen/go/v1``) with the API key
resolved from configuration
(``api_key`` / ``api_key_env`` / ``OPENCODE_API_KEY``). Local images are
inlined as ``image_url`` data URLs, so nothing is installed locally and no
``opencode`` CLI is needed.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
from typing import Optional

import aiohttp

from ...errors import ProviderUnavailableError
from ..schema import normalize_result
from ..types import (
    ImageRequest,
    ProviderFailureReason,
    ProviderResult,
    ProviderStatus,
    ProviderUsage,
)
from .base import Provider
from .json_output import extract_json
from .ratelimit import RateLimiter

logger = logging.getLogger("lm_visual_mcp.vision.providers.opencode")

DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1"
DEFAULT_API_KEY_ENV = "OPENCODE_API_KEY"
DEFAULT_MODEL = "mimo-v2.5"  # GO-plan multimodal default; override in config


class OpenCodeProvider(Provider):
    name = "opencode"

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 120.0,
        session: Optional[aiohttp.ClientSession] = None,
        limiter: Optional[RateLimiter] = None,
    ) -> None:
        super().__init__(limiter=limiter)
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or DEFAULT_MODEL
        self.effort = effort
        self._api_key = api_key
        self.timeout = timeout
        # session injectable for tests.
        self._session = session

    # -- session -------------------------------------------------------------
    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or getattr(self._session, "closed", False):
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    # -- probe ---------------------------------------------------------------
    async def probe(self, request: Optional[ImageRequest] = None) -> ProviderStatus:
        if not self._api_key:
            return ProviderStatus(
                name=self.name,
                available=False,
                reason=ProviderFailureReason.API_KEY_MISSING,
                message="opencode API key is not configured",
            )
        return ProviderStatus(
            name=self.name,
            available=True,
            model=self.model,
            vision_capability="available" if request and request.images else "unknown",
        )

    # -- analyze -------------------------------------------------------------
    async def _analyze(self, request: ImageRequest) -> ProviderResult:
        if not self._api_key:
            raise ProviderUnavailableError(
                ProviderFailureReason.API_KEY_MISSING,
                "opencode API key is not configured",
            )
        payload = self._build_payload(request)
        session = self._get_session()
        try:
            async with session.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
            ) as resp:
                body = await resp.read()
                if resp.status >= 400:
                    raise self._classify_status(resp.status, body)
                doc = json.loads(body)
        except ProviderUnavailableError:
            raise
        except asyncio.TimeoutError as exc:
            raise ProviderUnavailableError(
                ProviderFailureReason.TIMEOUT, f"opencode request timed out: {exc}"
            ) from exc
        except aiohttp.ClientConnectorError as exc:
            raise ProviderUnavailableError(
                ProviderFailureReason.TEMPORARY_FAILURE,
                f"cannot reach opencode endpoint: {exc}",
            ) from exc
        except (aiohttp.ClientError, ValueError) as exc:
            # ValueError: JSON decode failure of a 2xx body.
            raise ProviderUnavailableError(
                ProviderFailureReason.TEMPORARY_FAILURE, f"opencode request failed: {exc}"
            ) from exc

        text = _assistant_text(doc)
        if not text:
            raise ProviderUnavailableError(
                ProviderFailureReason.TEMPORARY_FAILURE,
                "opencode returned no assistant message",
            )
        try:
            data = extract_json(text)
        except Exception:  # noqa: BLE001
            data = {"answer": text.strip()}
        if not isinstance(data, dict):
            data = {"answer": str(data) if data else text.strip()}
        normalized = normalize_result(data)
        return ProviderResult(
            provider=self.name,
            result=normalized.to_dict(),
            model=self.model,
            usage=_usage_of(doc),
            raw=text,
        )

    def _build_payload(self, request: ImageRequest) -> dict:
        content: list[dict] = []
        for img in request.images:
            if img.local_path:
                data = Path(img.local_path).read_bytes()
                mime = img.mime_type or "image/png"
                data_url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
                content.append({"type": "image_url", "image_url": {"url": data_url}})
            elif img.url:
                content.append({"type": "image_url", "image_url": {"url": img.url}})
        content.append({"type": "text", "text": request.user_prompt})

        message: dict = {"role": "user", "content": content}
        body: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": request.system_prompt}, message],
        }
        if request.output_schema:
            # OpenAI-compatible structured output (json_schema). Endpoints that
            # ignore it still return JSON thanks to the prompt rules.
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "vision_result", "schema": request.output_schema},
            }
        if self.effort:
            body["reasoning_effort"] = self.effort
        return body

    # -- error mapping -------------------------------------------------------
    @staticmethod
    def _classify_status(status: int, body: bytes) -> ProviderUnavailableError:
        snippet = body.decode("utf-8", errors="replace")[:300]
        if status in (401, 403):
            return ProviderUnavailableError(
                ProviderFailureReason.NOT_AUTHENTICATED,
                f"opencode endpoint rejected credentials (HTTP {status}): {snippet}",
            )
        if status == 404:
            return ProviderUnavailableError(
                ProviderFailureReason.INVALID_MODEL,
                f"opencode model or endpoint not found (HTTP 404): {snippet}",
            )
        if status == 429:
            return ProviderUnavailableError(
                ProviderFailureReason.QUOTA_EXHAUSTED,
                f"opencode rate limited (HTTP 429): {snippet}",
            )
        if status >= 500:
            return ProviderUnavailableError(
                ProviderFailureReason.TEMPORARY_FAILURE,
                f"opencode endpoint error (HTTP {status}): {snippet}",
            )
        return ProviderUnavailableError(
            ProviderFailureReason.INVALID_INPUT,
            f"opencode rejected request (HTTP {status}): {snippet}",
        )

    @staticmethod
    def _classify_exc(exc: Exception) -> ProviderUnavailableError:
        if isinstance(exc, asyncio.TimeoutError):
            return ProviderUnavailableError(
                ProviderFailureReason.TIMEOUT, f"opencode request timed out: {exc}"
            )
        if isinstance(exc, aiohttp.ClientConnectorError):
            return ProviderUnavailableError(
                ProviderFailureReason.TEMPORARY_FAILURE,
                f"cannot reach opencode endpoint: {exc}",
            )
        return ProviderUnavailableError(
            ProviderFailureReason.TEMPORARY_FAILURE, f"opencode request failed: {exc}"
        )


def _assistant_text(doc: dict) -> Optional[str]:
    try:
        content = doc["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return "".join(texts) or None
    return None


def _usage_of(doc: dict) -> ProviderUsage:
    usage = doc.get("usage") or {}
    return ProviderUsage(
        input_tokens=usage.get("prompt_tokens"),
        output_tokens=usage.get("completion_tokens"),
        total_tokens=usage.get("total_tokens"),
    )
