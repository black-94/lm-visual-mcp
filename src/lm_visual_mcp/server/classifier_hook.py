"""Classifier request hook (Claude Code Auto-mode interoperability).

The hook is only a *detector* plus a *delegator*: it recognises a Claude Code
auto-mode classifier request via the security-monitor system prompt and then
delegates all rewriting to the provider chain through the router. It performs no
built-in thinking-disable or normalization - providers implement those (the pure
helpers live in :mod:`lm_visual_mcp.providers.classifier`).

Classifier calls use the normal Anthropic Messages endpoint, so URL or model
name alone cannot identify them. Detection relies on the classifier's
security-monitor system prompt and absence of tools. The ``</block>`` stop
sequence identifies the binary first stage whose response framing is known;
responses for that stage need (and may get) verdict normalization, so the hook
requests the forwarder buffer the response body.

A classifier request is only routed when either ``models`` is empty (all models)
or the request's target model is in the allowlist. When no provider on the
classifier chain rewrites anything, the request/response is passed through
verbatim - no built-in fallback normalization.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..providers.classifier import (
    build_classifier_response,
    is_auto_classifier_request,
    is_auto_classifier_stage1_request,
)
from ..providers.router import ProviderRouter
from ..providers.types import ProviderRequest, ProviderResponse
from .hooks import Hook, HookContext, HookResponse, HookResult

logger = logging.getLogger("lm_visual_mcp.server.classifier_hook")
from .protocols import ProtocolAdapter


class ClassifierHook(Hook):
    name = "classifier"

    def __init__(
        self,
        *,
        router: ProviderRouter,
        adapters: dict[str, ProtocolAdapter] | None = None,
        models: Optional[list[str]] = None,
    ) -> None:
        self.router = router
        self.adapters = adapters or {}
        # Model allowlist: empty = apply to all models; non-empty = only listed.
        self.models = models or []

    async def process(self, ctx: HookContext) -> HookResult:
        if ctx.state.get("protocol") != "anthropic":
            return HookResult.passthrough()
        body = ctx.body
        if not is_auto_classifier_request(body):
            return HookResult.passthrough()

        model = self._model_of(ctx)
        if self.models and model not in self.models:
            # Not in the allowlist -> forward the classifier request untouched.
            return HookResult.passthrough()

        request = ProviderRequest(
            protocol="anthropic",
            url=ctx.url,
            model=model or "",
            headers=ctx.headers,
            body=body,
        )

        # First try: a provider's own model produces a verdict and short-circuits
        # (no upstream forward). Any failure / ambiguity advances the chain.
        result = await self.router.classifier_verdict(request)
        if result is not None:
            if is_auto_classifier_stage1_request(body):
                ctx.state["classifier_stage1"] = True
            logger.info(
                "classifier intercepted by %s (model=%s) verdict=%s",
                result.provider, result.model, result.verdict,
            )
            return HookResult.intercept(
                HookResponse(
                    status=200,
                    headers={"Content-Type": "application/json"},
                    body=build_classifier_response(result),
                )
            )

        # Fallback: no provider produced a verdict -> forward upstream with the
        # byte-rewrite behavior (disable thinking / normalize), as before.
        if is_auto_classifier_stage1_request(body):
            # A provider may normalize the response verdict, so ask the
            # forwarder to buffer the response body for this request.
            ctx.state["classifier_stage1"] = True
            ctx.state["read_response_body"] = True
        request, request_provider = await self.router.classify_request(request)
        if request_provider is None:
            # No provider on the classifier chain handled it -> passthrough.
            return HookResult.passthrough()
        # Remember which provider took the request so the response pass knows a
        # rewrite actually happened (don't invent a provider for responses).
        ctx.state["classifier_provider"] = request_provider
        if request.body != body:
            return HookResult.rewrite(request.body)
        return HookResult.passthrough()

    async def process_response(self, ctx, status, headers, body):
        provider_name = ctx.state.get("classifier_provider")
        if not provider_name or status != 200:
            return None
        ctype = ""
        for key, value in headers.items():
            if key.lower() == "content-type":
                ctype = value
                break
        if "json" not in ctype.lower():
            return None
        response = ProviderResponse(
            url=ctx.url,
            status=status,
            headers=dict(headers),
            body=body,
        )
        response, _ = await self.router.classify_response(response)
        rewritten = response.body
        if rewritten == body:
            return None
        # The body has been rewritten as decoded JSON; encodings and validators
        # tied to the upstream bytes no longer describe what we send.
        out_headers = dict(headers)
        for key in ("Content-Encoding", "Content-MD5", "ETag"):
            out_headers.pop(key, None)
        return status, out_headers, rewritten

    def _model_of(self, ctx: HookContext) -> Optional[str]:
        """Extract the target model from the request, if the body carries it."""
        adapter = self.adapters.get(ctx.state.get("protocol", ""))
        if adapter is None:
            return None
        try:
            return adapter.model_of(ctx.body)
        except Exception:  # noqa: BLE001 - best-effort model extraction
            return None