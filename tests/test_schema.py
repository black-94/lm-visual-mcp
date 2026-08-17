"""Result normalization tests (unified schema coercion)."""

from __future__ import annotations

from lm_visual_mcp.vision.schema import normalize_result


def test_unknown_enum_types_coerced_to_other():
    """A provider-invented type (e.g. "shape") must not drop the whole list."""
    result = normalize_result(
        {
            "summary": "s",
            "answer": "a",
            "observations": [{"type": "shape", "text": "blue square", "confidence": 0.9}],
            "elements": [{"label": "square", "type": "shape"}],
        }
    )
    assert result.observations[0].type == "other"
    assert result.observations[0].text == "blue square"
    assert result.elements[0].type == "other"
    assert not result.warnings


def test_unknown_top_level_keys_land_in_details():
    result = normalize_result({"summary": "s", "answer": "a", "images": ["one", "two"]})
    assert result.details["images"] == ["one", "two"]


def test_malformed_input_degrades_gracefully():
    result = normalize_result({"summary": "s", "observations": "not a list"})
    assert result.answer == "s"
    assert result.observations == []
    assert any("warning" in w for w in result.warnings)


def test_sanitize_does_not_mutate_caller_dict():
    raw = {"summary": "s", "answer": "a", "observations": [{"type": "shape", "text": "t"}]}
    normalize_result(raw)
    assert raw["observations"][0]["type"] == "shape"
