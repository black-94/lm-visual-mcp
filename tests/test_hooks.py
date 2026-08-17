"""Hook pipeline tests: continue / rewrite / intercept, classifier behavior.

Classifier detection / normalization pure functions now live in
``lm_visual_mcp.providers.classifier``; the hook only detects + delegates to the
provider router.
"""

from __future__ import annotations

import json

from lm_visual_mcp.providers.base import Provider
from lm_visual_mcp.providers.classifier import (
    disable_auto_classifier_thinking,
    is_auto_classifier_request,
    is_auto_classifier_stage1_request,
    normalize_auto_classifier_response,
)
from lm_visual_mcp.providers.router import ProviderRouter
from lm_visual_mcp.providers.types import ProviderRequest, ProviderResponse
from lm_visual_mcp.server.classifier_hook import ClassifierHook
from lm_visual_mcp.server.hooks import HookContext, HookPipeline, HookResponse, HookResult


def ctx(body: bytes = b"{}", protocol: str = "anthropic") -> HookContext:
    return HookContext(
        method="POST", url="http://up/v1/messages", headers={"content-type": "application/json"},
        body=body, state={"protocol": protocol},
    )


# -- pipeline ---------------------------------------------------------------


class MarkingHook:
    def __init__(self, name: str, result_factory) -> None:
        self.name = name
        self._factory = result_factory
        self.seen: list[bytes] = []

    async def process(self, ctx: HookContext) -> HookResult:
        self.seen.append(ctx.body)
        return self._factory(ctx)

    async def process_response(self, ctx, status, headers, body):
        return None


async def test_pipeline_passthrough_when_no_hooks():
    c = ctx(b"abc")
    assert await HookPipeline([]).run(c) is None
    assert c.body == b"abc"


async def test_pipeline_rewrites_flow_downstream():
    h2 = MarkingHook("b", lambda c: HookResult.passthrough())
    h1 = MarkingHook("a", lambda c: HookResult.rewrite(b"rewritten"))
    c = ctx(b"orig")
    assert await HookPipeline([h1, h2]).run(c) is None
    assert h2.seen == [b"rewritten"]
    assert c.body == b"rewritten"


async def test_pipeline_intercept_stops_chain():
    response = HookResponse(status=418, headers={}, body=b"nope")
    h2 = MarkingHook("b", lambda c: HookResult.passthrough())
    h1 = MarkingHook("a", lambda c: HookResult.intercept(response))
    c = ctx(b"orig")
    out = await HookPipeline([h1, h2]).run(c)
    assert out is response
    assert h2.seen == []  # never reached


# -- classifier detection ----------------------------------------------------


def classifier_body(*, tools=None, stop_sequences=None, thinking=None) -> bytes:
    doc = {
        "model": "claude-x",
        "system": "You are a security monitor for autonomous AI coding agents.\nDecide.",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 32,
    }
    if tools is not None:
        doc["tools"] = tools
    if stop_sequences is not None:
        doc["stop_sequences"] = stop_sequences
    if thinking is not None:
        doc["thinking"] = thinking
    return json.dumps(doc).encode()


def test_detects_classifier_request():
    assert is_auto_classifier_request(classifier_body())
    assert not is_auto_classifier_request(classifier_body(tools=[{"name": "x"}]))
    assert not is_auto_classifier_request(json.dumps({"model": "m"}).encode())
    assert not is_auto_classifier_request(b"not json")


def test_detects_stage1_by_stop_sequence():
    assert is_auto_classifier_stage1_request(classifier_body(stop_sequences=["</block>"]))
    assert not is_auto_classifier_stage1_request(classifier_body())


def test_normalize_response_restores_stop_framing():
    body = json.dumps(
        {
            "type": "message",
            "content": [{"type": "text", "text": "<block>yes"}],
            "stop_reason": "end_turn",
        }
    ).encode()
    out, changed = normalize_auto_classifier_response(body)
    assert changed
    doc = json.loads(out)
    assert doc["content"] == [{"type": "text", "text": "<block>yes"}]
    assert doc["stop_reason"] == "stop_sequence"
    assert doc["stop_sequence"] == "</block>"


def test_normalize_ignores_thinking_blocks():
    """A gateway prepending a thinking block must not confuse the verdict regex."""
    body = json.dumps(
        {
            "type": "message",
            "content": [
                {"type": "thinking", "thinking": "let me decide <block>no"},
                {"type": "text", "text": "<block>yes"},
            ],
            "stop_reason": "end_turn",
        }
    ).encode()
    out, changed = normalize_auto_classifier_response(body)
    assert changed
    doc = json.loads(out)
    assert doc["content"] == [{"type": "text", "text": "<block>yes"}]
    assert doc["stop_reason"] == "stop_sequence"


def test_normalize_response_leaves_ambiguous_verdicts_alone():
    body = json.dumps(
        {
            "type": "message",
            "content": [{"type": "text", "text": "<block>yes"}, {"type": "text", "text": "<block>no"}],
        }
    ).encode()
    out, changed = normalize_auto_classifier_response(body)
    assert not changed and out is body


def test_disable_thinking_sets_thinking_disabled():
    body = classifier_body()
    out, changed = disable_auto_classifier_thinking(body)
    assert changed
    assert json.loads(out)["thinking"] == {"type": "disabled"}


# -- classifier hook ---------------------------------------------------------


class ClassifierCapable(Provider):
    """A provider with classifier capability (like gemini/opencode/volcengine)."""

    name = "gemini"

    async def rewrite_classifier_request(self, request: ProviderRequest):
        return disable_auto_classifier_thinking(request.body)

    async def rewrite_classifier_response(self, response: ProviderResponse):
        return normalize_auto_classifier_response(response.body)


class NoopProvider(Provider):
    """A CLI-style provider with NO classifier capability (default passthrough)."""

    name = "agy"


class StubAdapter:
    """Minimal ProtocolAdapter for model extraction."""

    path = "anthropic"

    def model_of(self, body: bytes):
        try:
            return json.loads(body).get("model")
        except (ValueError, UnicodeDecodeError):
            return None


def make_hook(provider=None, models=None, adapters=None):
    provider = provider or NoopProvider()
    router = ProviderRouter(
        {provider.name: provider},
        image_chain=[],
        classifier_chain=[provider.name],
    )
    return ClassifierHook(router=router, models=models,
                          adapters=adapters or {"anthropic": StubAdapter()})


async def test_classifier_hook_delegates_thinking_disable_and_marks_stage1():
    hook = make_hook(ClassifierCapable())
    body = classifier_body(stop_sequences=["</block>"])
    c = ctx(body)
    result = await hook.process(c)
    assert result.action == "continue"
    assert json.loads(result.body)["thinking"] == {"type": "disabled"}
    assert c.state["classifier_stage1"] is True
    assert c.state["read_response_body"] is True
    assert c.state["classifier_provider"] == "gemini"


async def test_classifier_hook_passthrough_when_no_provider_handles():
    # agy/codex-style provider has no classifier capability -> passthrough.
    hook = make_hook(NoopProvider())
    c = ctx(classifier_body(stop_sequences=["</block>"]))
    result = await hook.process(c)
    assert result.action == "continue" and result.body is None
    # no provider touched it -> response pass must be inert too
    assert "classifier_provider" not in c.state


async def test_classifier_hook_ignores_non_classifier():
    hook = make_hook()
    c = ctx(json.dumps({"model": "m"}).encode())
    result = await hook.process(c)
    assert result.action == "continue" and result.body is None
    assert "classifier_stage1" not in c.state


async def test_classifier_hook_ignores_other_protocols():
    hook = make_hook()
    c = ctx(classifier_body(stop_sequences=["</block>"]), protocol="openai/chat")
    result = await hook.process(c)
    assert result.action == "continue" and result.body is None


async def test_classifier_hook_model_allowlist_miss_passthrough():
    hook = make_hook(ClassifierCapable(), models=["other-model"])
    c = ctx(classifier_body(stop_sequences=["</block>"]))  # model claude-x not listed
    result = await hook.process(c)
    assert result.action == "continue" and result.body is None
    assert "classifier_stage1" not in c.state


async def test_classifier_hook_model_allowlist_hit_routes():
    hook = make_hook(ClassifierCapable(), models=["claude-x"])
    c = ctx(classifier_body(stop_sequences=["</block>"]))
    result = await hook.process(c)
    assert result.action == "continue" and result.body is not None
    assert c.state["classifier_provider"] == "gemini"


async def test_classifier_hook_process_response_normalizes():
    hook = make_hook(ClassifierCapable())
    c = ctx(classifier_body(stop_sequences=["</block>"]))
    await hook.process(c)
    upstream = json.dumps(
        {"type": "message", "content": [{"type": "text", "text": "<block>no"}], "stop_reason": "end_turn"}
    ).encode()
    out = await hook.process_response(
        c, 200, {"Content-Type": "application/json", "Content-Encoding": "gzip"}, upstream
    )
    assert out is not None
    status, headers, body = out
    assert status == 200
    assert "Content-Encoding" not in headers
    assert json.loads(body)["stop_reason"] == "stop_sequence"


async def test_classifier_hook_process_response_skips_unmarked():
    hook = make_hook(ClassifierCapable())
    c = ctx(b"{}")
    out = await hook.process_response(c, 200, {"Content-Type": "application/json"}, b"{}")
    assert out is None