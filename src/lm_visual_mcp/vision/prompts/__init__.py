"""Specialized image-analysis prompt layer.

Each tool gets its own system prompt; providers never see the tool name - they
only receive the final :class:`~lm_visual_mcp.vision.types.ImageRequest`.
Every prompt ends with the shared structured-output rules so the model produces
the unified JSON schema.
"""

from __future__ import annotations

from typing import Optional

from . import (  # noqa: F401
    data_visualization,
    error_diagnosis,
    image_analysis,
    technical_diagram,
    text_extraction,
    ui_description,
    ui_diff,
    ui_to_code,
    ui_to_prompt,
    ui_to_spec,
)
from .shared import OUTPUT_RULES

__all__ = ["get_system_prompt", "OUTPUT_RULES"]


_UI_ARTIFACT = {
    "code": ui_to_code.SYSTEM_PROMPT,
    "prompt": ui_to_prompt.SYSTEM_PROMPT,
    "spec": ui_to_spec.SYSTEM_PROMPT,
    "description": ui_description.SYSTEM_PROMPT,
}

_TOOL_PROMPTS = {
    "ui_to_artifact": image_analysis.SYSTEM_PROMPT,  # overridden by output_type
    "extract_text_from_screenshot": text_extraction.SYSTEM_PROMPT,
    "diagnose_error_screenshot": error_diagnosis.SYSTEM_PROMPT,
    "understand_technical_diagram": technical_diagram.SYSTEM_PROMPT,
    "analyze_data_visualization": data_visualization.SYSTEM_PROMPT,
    "ui_diff_check": ui_diff.SYSTEM_PROMPT,
    "analyze_image": image_analysis.SYSTEM_PROMPT,
    "image_analysis": image_analysis.SYSTEM_PROMPT,
    "describe_image": image_analysis.SYSTEM_PROMPT,
}


def get_system_prompt(tool: str, output_type: Optional[str] = None) -> str:
    """Return the specialized system prompt for ``tool``."""
    if tool == "ui_to_artifact" and output_type in _UI_ARTIFACT:
        base = _UI_ARTIFACT[output_type]
    else:
        base = _TOOL_PROMPTS.get(tool, image_analysis.SYSTEM_PROMPT)
    return base + "\n\n" + OUTPUT_RULES
