"""Vision tool names, kept import-light so health probes and CLI startup can
report a tool count without pulling in the whole server stack.

IMPORTANT: This list MUST match the ``@mcp.tool()`` registrations in
``mcp/server.py``.
"""

from __future__ import annotations

TOOL_NAMES: tuple[str, ...] = (
    "ui_to_artifact",
    "extract_text_from_screenshot",
    "diagnose_error_screenshot",
    "understand_technical_diagram",
    "analyze_data_visualization",
    "ui_diff_check",
    "analyze_image",
    "image_analysis",
)
