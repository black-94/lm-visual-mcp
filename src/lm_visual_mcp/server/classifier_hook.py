"""Classifier request hook (Claude Code Auto-mode interoperability).

Classifier calls use the normal Anthropic Messages endpoint, so URL or model
name alone cannot identify them. Family detection relies on the classifier's
security-monitor system prompt and absence of tools. The ``</block>`` stop
sequence identifies the binary first stage whose response framing is known.
Some Anthropic-compatible gateways ignore that stop sequence and/or prepend a
thinking content block. Claude Code treats those otherwise-successful responses
as classifier-unavailable. The normalizer restores the response shape that an
Anthropic stop sequence would have produced.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .hooks import Hook, HookContext, HookResult

_SYSTEM_MARKER = "You are a security monitor for autonomous AI coding agents."
_STOP_SEQUENCE = "</block>"
_VERDICT_RE = re.compile(r"<block>\s*(yes|no)\b(?:\s*</block>)?", re.IGNORECASE)


def is_auto_classifier_request(body: bytes) -> bool:
    """Return whether ``body`` belongs to Claude Code's Auto classifier.

    Model aliases, max_tokens and prompt length have changed across Claude Code
    releases and gateways, so they are intentionally not used as identifiers.
    """
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    if not isinstance(doc, dict):
        return False
    tools = doc.get("tools")
    if tools not in (None, []):
        return False
    return _SYSTEM_MARKER in _text_of(doc.get("system"))


def is_auto_classifier_stage1_request(body: bytes) -> bool:
    """Return whether ``body`` uses the known binary stage-one contract."""
    if not is_auto_classifier_request(body):
        return False
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    stops = doc.get("stop_sequences")
    return isinstance(stops, list) and _STOP_SEQUENCE in stops


def normalize_auto_classifier_response(body: bytes) -> tuple[bytes, bool]:
    """Restore Anthropic stop-sequence semantics for a classifier response.

    Returns ``(body, changed)``. A response without an unambiguous verdict is
    left untouched; the hook must never invent a safety decision.
    """
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return body, False
    if not isinstance(doc, dict) or doc.get("type") != "message":
        return body, False
    content = doc.get("content")
    if not isinstance(content, list):
        return body, False

    verdicts: set[str] = set()
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        verdicts.update(match.group(1).lower() for match in _VERDICT_RE.finditer(text))
    if len(verdicts) != 1:
        return body, False
    verdict = verdicts.pop()

    normalized = dict(doc)
    # Anthropic omits the matched stop sequence from generated text and reports
    # it through stop_reason/stop_sequence. Claude Code's classifier parser
    # expects this exact framing.
    normalized["content"] = [{"type": "text", "text": f"<block>{verdict}"}]
    normalized["stop_reason"] = "stop_sequence"
    normalized["stop_sequence"] = _STOP_SEQUENCE
    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return encoded, encoded != body


def disable_auto_classifier_thinking(body: bytes) -> tuple[bytes, bool]:
    """Set ``thinking.type`` to ``disabled`` on a classifier request.

    The caller must first establish that this is a classifier request. The
    function is defensive and leaves non-object JSON untouched.
    """
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return body, False
    if not isinstance(doc, dict):
        return body, False
    disabled = {"type": "disabled"}
    if doc.get("thinking") == disabled:
        return body, False
    doc["thinking"] = disabled
    encoded = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return encoded, True


class ClassifierHook(Hook):
    name = "classifier"

    def __init__(self, *, disable_thinking: bool = True) -> None:
        self.disable_thinking = disable_thinking

    async def process(self, ctx: HookContext) -> HookResult:
        if ctx.state.get("protocol") != "anthropic":
            return HookResult.passthrough()
        body = ctx.body
        if not is_auto_classifier_request(body):
            return HookResult.passthrough()
        if is_auto_classifier_stage1_request(body):
            # Stage-1 responses need verdict normalization, so ask the
            # forwarder to buffer the response body for this request.
            ctx.state["classifier_stage1"] = True
            ctx.state["read_response_body"] = True
        if self.disable_thinking:
            body, changed = disable_auto_classifier_thinking(body)
            if changed:
                return HookResult.rewrite(body)
        return HookResult.passthrough()

    async def process_response(self, ctx, status, headers, body):
        if not ctx.state.get("classifier_stage1") or status != 200:
            return None
        ctype = ""
        for key, value in headers.items():
            if key.lower() == "content-type":
                ctype = value
                break
        if "json" not in ctype.lower():
            return None
        rewritten, changed = normalize_auto_classifier_response(body)
        if not changed:
            return None
        # The body has been rewritten as decoded JSON; encodings and validators
        # tied to the upstream bytes no longer describe what we send.
        out_headers = dict(headers)
        for key in ("Content-Encoding", "Content-MD5", "ETag"):
            out_headers.pop(key, None)
        return status, out_headers, rewritten


def _text_of(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    texts: list[str] = []
    for block in value:
        if isinstance(block, str):
            texts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            texts.append(block["text"])
    return "\n".join(texts)
