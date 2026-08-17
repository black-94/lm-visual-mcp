"""Classifier detection / normalization pure functions.

Optional tool module for providers that explicitly implement classifier
handling. Nothing here is invoked by default - a provider reuses these only
when it overrides ``rewrite_classifier_request`` / ``rewrite_classifier_response``
(see the API providers gemini/opencode/volcengine). The request hooks use
``is_auto_classifier_request``/``is_auto_classifier_stage1_request`` purely to
detect a classifier call and decide whether to route it.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from .types import ClassifierResult

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
    left untouched; callers must never invent a safety decision. Only ``text``
    content blocks are considered, so a gateway that prepends a ``thinking``
    block cannot confuse the verdict regex.
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


def extract_verdict(text: str) -> Optional[str]:
    """Return the single decisive ``"yes"``/``"no"`` from a model's raw output.

    A classifier model is expected to answer with a ``<block>yes</block>`` /
    ``<block>no</block>`` block. Runs the verdict regex over the whole blob; when
    the matches don't resolve to exactly one distinct verdict (none, or both yes
    and no) it returns ``None`` - the caller must not guess.
    """
    matches = set(match.group(1).lower() for match in _VERDICT_RE.finditer(text))
    if len(matches) != 1:
        return None
    return matches.pop()


def build_classifier_response(result: ClassifierResult) -> bytes:
    """Frame a provider's own-model verdict as the Anthropic response CC expects.

    Mirrors the framing in :func:`normalize_auto_classifier_response` - CC's
    classifier parser wants the matched ``</block>`` stop sequence omitted from
    the text and reported via ``stop_reason`` / ``stop_sequence``.
    """
    doc = {
        "id": f"msg_{result.provider}",
        "type": "message",
        "role": "assistant",
        "model": result.model,
        "content": [{"type": "text", "text": f"<block>{result.verdict}"}],
        "stop_reason": "stop_sequence",
        "stop_sequence": _STOP_SEQUENCE,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def classifier_text_messages(body: bytes) -> tuple[str, list[tuple[str, str]]]:
    """Split a classifier request body into ``(system_text, [(role, text), ...])``.

    Classifier requests carry no images, so content is reduced to its text
    blocks. Returns an empty system string / message list for non-object JSON.
    """
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return "", []
    if not isinstance(doc, dict):
        return "", []
    system = _text_of(doc.get("system"))
    messages = doc.get("messages")
    turns: list[tuple[str, str]] = []
    if isinstance(messages, list):
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            content = msg.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                texts = [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and isinstance(block.get("text"), str)
                ]
                text = "\n".join(texts)
            else:
                text = ""
            if role and text:
                turns.append((str(role), text))
    return system, turns


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