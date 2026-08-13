"""The describe step: reuse the existing perception prompt + provider router.

The proxy only performs a generic first-pass description (sensor). Deeper
digging is left to the text model, which can call the existing ``lm-visual-mcp``
MCP server for task-aware tools when it needs more detail.
"""

from __future__ import annotations

from ..models import ImageInput, VisionRequest
from ..prompts.image_analysis import SYSTEM_PROMPT
from ..router import ProviderRouter

# Lightweight per-image output schema. All providers can enforce it
# (Gemini response_schema / AGY --json-schema / Codex --output-schema).
DESCRIBE_SCHEMA: dict = {
    "type": "object",
    "properties": {"images": {"type": "array", "items": {"type": "string"}}},
    "required": ["images"],
}

_DESCRIBE_USER = (
    'Describe each attached image: visible objects, text, layout, colors and '
    'spatial relationships. Return a JSON object with a single key "images": '
    'an array of strings, one per image, in the same order as the images. '
    'Do not answer questions or infer intent.'
)


async def describe(router: ProviderRouter, images: list[ImageInput], timeout: float) -> list[str]:
    """Run one vision request over ``images`` and return per-image descriptions.

    The returned list is aligned with ``images`` (padded/truncated to match).
    """
    if not images:
        return []
    request = VisionRequest(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=_DESCRIBE_USER,
        images=images,
        output_schema=DESCRIBE_SCHEMA,
        timeout=timeout,
    )
    routed = await router.route(request)
    arr = (routed.result.get("details") or {}).get("images")
    if not isinstance(arr, list):
        arr = [routed.result.get("answer") or routed.result.get("summary") or ""]
    # Align to the number of images, never guessing content.
    return [_coerce(arr[k]) if k < len(arr) else "" for k in range(len(images))]


def _coerce(value) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)