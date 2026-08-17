"""Volcengine (火山引擎) provider (direct API).

One provider class, three modes selected by ``mode`` (all implemented):

- ``mode: "agent"``  -> Agent Plan   (Anthropic Messages dialect)  base ``https://ark.cn-beijing.volces.com/api/plan``
- ``mode: "coding"`` -> Coding Plan  (Anthropic Messages dialect)  base ``https://ark.cn-beijing.volces.com/api/coding``
- ``mode: "api"``    -> 火山基础推理    (OpenAI chat-completions dialect) base ``https://ark.cn-beijing.volces.com/api/v3``

``base_url`` explicitly given in config overrides the mode default. All three
modes are data-plane inference only - quota/usage control-plane calls
(``open.volcengineapi.com`` SigV4 GetAFPUsage/GetCodingPlanUsage) are out of
scope for this provider.

- ``agent`` / ``coding`` use the Anthropic Messages endpoint ``{base}/v1/messages``
  (cc-switch treats these two plan bases directly as a Claude provider's
  ``ANTHROPIC_BASE_URL``); local images are inlined as ``image`` base64
  content-blocks.
- ``api`` uses the OpenAI ``{base}/chat/completions`` endpoint; local images are
  inlined as ``image_url`` data URLs (same construction as opencode).

API keys are resolved from configuration (``api_key`` / ``api_key_env`` /
``VOLCENGINE_API_KEY``) and never taken from a tool call. Authorization is the
Bearer header on every mode.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
from typing import Optional

import aiohttp

from ..errors import ProviderUnavailableError
from .base import Provider
from .classifier import (
    classifier_text_messages,
    disable_auto_classifier_thinking,
    extract_verdict,
    normalize_auto_classifier_response,
)
from .json_output import extract_json
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

logger = logging.getLogger("lm_visual_mcp.vision.providers.volcengine")

DEFAULT_API_KEY_ENV = "VOLCENGINE_API_KEY"
DEFAULT_API_MODEL = "doubao-seed-1-6-250615"  # api tier; override in config
DEFAULT_MAX_TOKENS = 4096

#: Mode selector -> (base URL, dialect). ``base_url`` in config overrides.
_MODE_BASES = {
    "agent": "https://ark.cn-beijing.volces.com/api/plan",
    "coding": "https://ark.cn-beijing.volces.com/api/coding",
    "api": "https://ark.cn-beijing.volces.com/api/v3",
}
#: Anthropic Messages dialect for the two plan modes.
_ANTHROPIC_MODE = {"agent", "coding"}


class VolcengineProvider(Provider):
    name = "volcengine"

    def __init__(
        self,
        *,
        mode: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 120.0,
        disable_thinking: Optional[bool] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        session: Optional[aiohttp.ClientSession] = None,
        limiter: Optional[RateLimiter] = None,
    ) -> None:
        super().__init__(limiter=limiter, mode=mode)
        self.mode = (mode or "api").lower()
        if self.mode not in _MODE_BASES:
            raise ValueError(f"unknown volcengine mode {self.mode!r} (expected agent/coding/api)")
        self.base_url = (base_url or _MODE_BASES[self.mode]).rstrip("/")
        self.model = model
        self.effort = effort
        self.api_key = api_key
        self.timeout = timeout
        self.disable_thinking = disable_thinking
        self.max_tokens = max_tokens
        # session injectable for tests.
        self._session = session
        # Whether this mode speaks the Anthropic Messages or OpenAI dialect.
        self._anthropic = self.mode in _ANTHROPIC_MODE

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
    async def probe_image(self, request: Optional[ImageRequest] = None) -> ProviderStatus:
        if not self.api_key:
            return ProviderStatus(
                name=self.name,
                available=False,
                reason=ProviderFailureReason.API_KEY_MISSING,
                message="volcengine API key is not configured",
            )
        return ProviderStatus(
            name=self.name,
            available=True,
            model=self.model,
            vision_capability="available" if request and request.images else "unknown",
        )

    # -- analyze -------------------------------------------------------------
    async def _analyze_image(self, request: ImageRequest) -> ProviderResult:
        if not self.api_key:
            raise ProviderUnavailableError(
                ProviderFailureReason.API_KEY_MISSING,
                "volcengine API key is not configured",
            )
        payload = self._build_payload(request)
        endpoint = "v1/messages" if self._anthropic else "chat/completions"
        session = self._get_session()
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self._anthropic:
            headers["anthropic-version"] = "2023-06-01"

        try:
            async with session.post(
                f"{self.base_url}/{endpoint}",
                json=payload,
                headers=headers,
            ) as resp:
                body = await resp.read()
                if resp.status >= 400:
                    raise self._classify_status(resp.status, body)
                doc = json.loads(body)
        except ProviderUnavailableError:
            raise
        except asyncio.TimeoutError as exc:
            raise ProviderUnavailableError(
                ProviderFailureReason.TIMEOUT, f"volcengine request timed out: {exc}"
            ) from exc
        except aiohttp.ClientConnectorError as exc:
            raise ProviderUnavailableError(
                ProviderFailureReason.TEMPORARY_FAILURE,
                f"cannot reach volcengine endpoint: {exc}",
            ) from exc
        except (aiohttp.ClientError, ValueError) as exc:
            # ValueError: JSON decode failure of a 2xx body.
            raise ProviderUnavailableError(
                ProviderFailureReason.TEMPORARY_FAILURE, f"volcengine request failed: {exc}"
            ) from exc

        text = _assistant_text(doc, anthropic=self._anthropic)
        if not text:
            raise ProviderUnavailableError(
                ProviderFailureReason.TEMPORARY_FAILURE,
                "volcengine returned no assistant message",
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
            usage=_usage_of(doc, anthropic=self._anthropic),
            raw=text,
        )

    def _build_payload(self, request: ImageRequest) -> dict:
        if self._anthropic:
            return self._build_anthropic_payload(request)
        return self._build_openai_payload(request)

    # -- anthropic messages dialect (agent / coding) -------------------------
    def _build_anthropic_payload(self, request: ImageRequest) -> dict:
        content: list[dict] = []
        for img in request.images:
            if img.local_path:
                data = Path(img.local_path).read_bytes()
                content.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": img.mime_type or "image/png",
                            "data": base64.b64encode(data).decode("ascii"),
                        },
                    }
                )
            elif img.url:
                content.append(
                    {
                        "type": "image",
                        "source": {"type": "url", "url": img.url},
                    }
                )
        content.append({"type": "text", "text": request.user_prompt})

        body: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": content}],
        }
        if request.system_prompt:
            body["system"] = request.system_prompt
        if request.output_schema:
            # The plan dialects accept the Anthropic-ish structured output hint;
            # those that ignore it still return JSON thanks to the prompt rules.
            body["tool_choice"] = {"type": "tool", "name": "vision_result"}
            body["tools"] = [
                {
                    "name": "vision_result",
                    "description": "Return the structured vision analysis result.",
                    "input_schema": request.output_schema,
                }
            ]
        return body

    # -- openai chat-completions dialect (api) -------------------------------
    def _build_openai_payload(self, request: ImageRequest) -> dict:
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
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "vision_result", "schema": request.output_schema},
            }
        return body

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
        """Call volcengine's own backend model for a classifier verdict.

        Uses the Anthropic Messages dialect (agent/coding plans) with this
        provider's configured ``model``. Returns ``None`` on any failure or an
        ambiguous verdict so the router can fall through to the next provider.
        """
        if not self.api_key:
            return None
        system, turns = classifier_text_messages(request.body)
        if not self._anthropic or not turns:
            return None
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": role, "content": [{"type": "text", "text": text}]}
                for role, text in turns
            ],
        }
        if system:
            payload["system"] = system
        try:
            async with self._get_session().post(
                f"{self.base_url}/v1/messages",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "anthropic-version": "2023-06-01",
                },
            ) as resp:
                body = await resp.read()
                if resp.status >= 400:
                    raise self._classify_status(resp.status, body)
                doc = json.loads(body)
        except Exception:  # noqa: BLE001 - never break the classifier chain
            logger.warning("volcengine classifier inference failed", exc_info=True)
            return None
        text = _assistant_text(doc, anthropic=True) or ""
        verdict = extract_verdict(text)
        if verdict is None:
            return None
        return ClassifierResult(
            provider=self.name, model=self.model, verdict=verdict, raw=text
        )

    # -- error mapping -------------------------------------------------------
    @staticmethod
    def _classify_status(status: int, body: bytes) -> ProviderUnavailableError:
        snippet = body.decode("utf-8", errors="replace")[:300]
        if status in (401, 403):
            return ProviderUnavailableError(
                ProviderFailureReason.NOT_AUTHENTICATED,
                f"volcengine endpoint rejected credentials (HTTP {status}): {snippet}",
            )
        if status == 404:
            return ProviderUnavailableError(
                ProviderFailureReason.INVALID_MODEL,
                f"volcengine model or endpoint not found (HTTP 404): {snippet}",
            )
        if status == 429:
            return ProviderUnavailableError(
                ProviderFailureReason.QUOTA_EXHAUSTED,
                f"volcengine rate limited (HTTP 429): {snippet}",
            )
        if status >= 500:
            return ProviderUnavailableError(
                ProviderFailureReason.TEMPORARY_FAILURE,
                f"volcengine endpoint error (HTTP {status}): {snippet}",
            )
        return ProviderUnavailableError(
            ProviderFailureReason.INVALID_INPUT,
            f"volcengine rejected request (HTTP {status}): {snippet}",
        )


def _assistant_text(doc: dict, *, anthropic: bool) -> Optional[str]:
    if anthropic:
        content = doc.get("content")
        if isinstance(content, list):
            texts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            return "".join(texts) or None
        if isinstance(content, str):
            return content
        return None
    # openai chat-completions
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


def _usage_of(doc: dict, *, anthropic: bool) -> ProviderUsage:
    if anthropic:
        usage = doc.get("usage") or {}
        return ProviderUsage(
            input_tokens=_num(usage.get("input_tokens")),
            output_tokens=_num(usage.get("output_tokens")),
            cache_read_tokens=_num(usage.get("cache_read_input_tokens")),
            total_tokens=None,
        )
    usage = doc.get("usage") or {}
    return ProviderUsage(
        input_tokens=_num(usage.get("prompt_tokens")),
        output_tokens=_num(usage.get("completion_tokens")),
        total_tokens=_num(usage.get("total_tokens")),
    )


def _num(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None