"""Unified structured-output schema.

Every provider's free-form output is normalized into this single schema so the
MCP response envelope is identical regardless of which provider served the
request. bbox coordinates are normalized to 0..1000 as ``[x_min, y_min,
x_max, y_max]``. When a value cannot be determined, providers must not guess:
omit it and lower confidence instead.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

BBox = tuple[int, int, int, int]
ObservationType = Literal["text", "object", "ui", "error", "diagram", "data", "other"]
ElementType = Literal["ui_element", "object", "text", "other"]


class Observation(BaseModel):
    type: ObservationType = "other"
    text: str
    confidence: float = Field(default=0.5, ge=0.0, le=1.0, description="0..1")


class TextElement(BaseModel):
    text: str
    bbox: Optional[BBox] = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0, description="0..1")


class Element(BaseModel):
    label: str
    type: ElementType = "other"
    bbox: Optional[BBox] = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0, description="0..1")


class VisionResult(BaseModel):
    """The unified structured result returned by all providers."""

    summary: str = ""
    answer: str = ""
    observations: list[Observation] = Field(default_factory=list)
    texts: list[TextElement] = Field(default_factory=list)
    elements: list[Element] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # Specialized tools may add extra structured detail.
    details: dict = Field(default_factory=dict)

    def to_dict(self) -> dict:
        return self.model_dump(exclude_none=True)


# JSON Schema used to steer providers (e.g. --json-schema / --output-schema /
# google-genai response_schema). Derived from VisionResult.
VISION_RESULT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "answer": {"type": "string"},
        "observations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "text": {"type": "string"},
                    "confidence": {"type": "number"},
                },
            },
        },
        "texts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "bbox": {"type": "array", "items": {"type": "integer"}},
                    "confidence": {"type": "number"},
                },
            },
        },
        "elements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "type": {"type": "string"},
                    "bbox": {"type": "array", "items": {"type": "integer"}},
                    "confidence": {"type": "number"},
                },
            },
        },
        "warnings": {"type": "array", "items": {"type": "string"}},
        "details": {"type": "object"},
    },
    "required": ["summary", "answer"],
    "additionalProperties": True,
}


def build_codex_schema() -> dict:
    """Return a codex-compatible strict JSON Schema.

    ``codex exec --output-schema`` requires every object to declare
    ``additionalProperties: false`` and ``required`` covering every key, and
    forbids free-form ``details`` objects in ``required``.
    """
    base = dict(VISION_RESULT_SCHEMA)
    props = dict(base["properties"])
    props.pop("details", None)  # codex forbids free-form objects in required
    base["properties"] = props
    return _make_strict(base)


def _make_strict(node):
    if isinstance(node, dict):
        out = {}
        is_obj = node.get("type") == "object"
        for k, v in node.items():
            if k == "properties":
                strict_props = {pk: _make_strict(pv) for pk, pv in v.items()}
                out["properties"] = strict_props
                out["required"] = list(v.keys())
            elif k == "items":
                out["items"] = _make_strict(v)
            elif k in ("additionalProperties", "required"):
                continue  # strict mode regenerates these per object.
            else:
                out[k] = v
        if is_obj:
            out["additionalProperties"] = False
        return out
    if isinstance(node, list):
        return [_make_strict(x) for x in node]
    return node


def normalize_result(raw: dict) -> VisionResult:
    """Coerce arbitrary provider JSON into a :class:`VisionResult`.

    Unknown keys are preserved into ``details``. Malformed fields degrade
    gracefully instead of raising, matching the "don't guess, warn" policy.
    """
    warnings: list[str] = list(raw.get("warnings") or [])
    try:
        return VisionResult.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - normalize must not raise
        warnings.append(f"result normalization warning: {exc}")
        return VisionResult(
            summary=str(raw.get("summary", "")),
            answer=str(raw.get("answer", raw.get("answer") or raw.get("summary", ""))),
            warnings=warnings,
            details=raw,
        )