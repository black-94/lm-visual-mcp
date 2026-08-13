"""Vision tool names, kept import-light so the daemon's ``/health`` endpoint can
report a tool count without pulling in the whole server stack (providers,
aiohttp, Pillow) on cold start.

IMPORTANT: This list MUST match the ``@mcp.tool()`` registrations in
``server.py``. ``build_server`` validates them at startup.
"""

from __future__ import annotations

_TOOL_NAMES: tuple[str, ...] = (
    "ui_to_artifact",
    "extract_text_from_screenshot",
    "diagnose_error_screenshot",
    "understand_technical_diagram",
    "analyze_data_visualization",
    "ui_diff_check",
    "analyze_image",
    "analyze_video",
    "image_analysis",
    "video_analysis",
)