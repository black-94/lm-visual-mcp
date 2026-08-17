"""Hook pipeline tests: continue / rewrite / intercept, classifier behavior."""

from __future__ import annotations

import json

from lm_visual_mcp.server.classifier_hook import (
    ClassifierHook,
    disable_auto_classifier_thinking,
    is_auto_classifier_request,
    is_auto_classifier_stage1_request,
    normalize_auto_classifier_response,
)
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


def test_normalize_response_leaves_ambiguous_verdicts_alone():
    body = json.dumps(
        {
            "type": "message",
            "content": [{"type": "text", "text": "<block>yes"}, {"type": "text", "text": "<block>no"}],
        }
    ).encode()
    out, changed = normalize_auto_classifier_response(body)
    assert not changed and out is body


# -- classifier hook ---------------------------------------------------------


async def test_classifier_hook_disables_thinking_and_marks_stage1():
    hook = ClassifierHook(disable_thinking=True)
    body = classifier_body(stop_sequences=["</block>"])
    c = ctx(body)
    result = await hook.process(c)
    assert result.action == "continue"
    assert json.loads(result.body)["thinking"] == {"type": "disabled"}
    assert c.state["classifier_stage1"] is True
    assert c.state["read_response_body"] is True


async def test_classifier_hook_ignores_non_classifier():
    hook = ClassifierHook()
    c = ctx(json.dumps({"model": "m"}).encode())
    result = await hook.process(c)
    assert result.action == "continue" and result.body is None
    assert "classifier_stage1" not in c.state


async def test_classifier_hook_ignores_other_protocols():
    hook = ClassifierHook()
    c = ctx(classifier_body(stop_sequences=["</block>"]), protocol="openai/chat")
    result = await hook.process(c)
    assert result.action == "continue" and result.body is None


async def test_classifier_hook_process_response_normalizes():
    hook = ClassifierHook()
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
    hook = ClassifierHook()
    c = ctx(b"{}")
    out = await hook.process_response(c, 200, {"Content-Type": "application/json"}, b"{}")
    assert out is None
