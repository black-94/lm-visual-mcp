"""Gemini provider schema handling (pure, no network)."""

from __future__ import annotations

from lm_visual_mcp.providers.gemini import _gemini_schema


def test_gemini_schema_strips_additional_properties_recursively():
    s = {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "details": {"type": "object", "additionalProperties": True},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["details"],
    }
    out = _gemini_schema(s)
    # Kill the Enterprise-only keyword at every depth; keep the rest intact.
    assert "additionalProperties" not in out
    assert "additionalProperties" not in out["properties"]["details"]
    assert out["type"] == "object"
    assert out["properties"]["tags"] == {"type": "array", "items": {"type": "string"}}
    assert out["required"] == ["details"]


def test_gemini_schema_leaves_plain_schema_unchanged():
    s = {"type": "object", "properties": {"summary": {"type": "string"}}}
    assert _gemini_schema(s) == s