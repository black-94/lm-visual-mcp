"""MCP server exposing the image vision tools.

Tool schemas carry only business parameters (image_source, prompt, ...). Server
concerns (provider, model, api_key, timeout, fallback) never appear in tool
schemas - they are policy owned by the shared server's configuration. Every
call is forwarded to the server's vision service (see :mod:`.client`); this
version supports image analysis only.
"""

from __future__ import annotations

import logging
from typing import Optional

from mcp.server.fastmcp import FastMCP

from .client import RemoteVision

logger = logging.getLogger("lm_visual_mcp.mcp.server")


def build_mcp(client: RemoteVision) -> FastMCP:
    mcp = FastMCP("Vision MCP")

    @mcp.tool()
    async def ui_to_artifact(image_source: str, output_type: str, prompt: str) -> dict:
        """Convert a UI screenshot into an artifact (code, prompt, spec or description)."""
        return await client.analyze_images(
            tool="ui_to_artifact",
            image_sources=[image_source],
            user_prompt=prompt,
            output_type=output_type,
        )

    @mcp.tool()
    async def extract_text_from_screenshot(
        image_source: str, prompt: str, programming_language: Optional[str] = None
    ) -> dict:
        """Extract visible text (OCR), source code, terminal/config content verbatim."""
        user = prompt + (f"\nProgramming language: {programming_language}" if programming_language else "")
        return await client.analyze_images(
            tool="extract_text_from_screenshot",
            image_sources=[image_source],
            user_prompt=user,
        )

    @mcp.tool()
    async def diagnose_error_screenshot(
        image_source: str, prompt: str, context: Optional[str] = None
    ) -> dict:
        """Diagnose an error/stack trace shown in a screenshot."""
        user = prompt + (f"\nContext: {context}" if context else "")
        return await client.analyze_images(
            tool="diagnose_error_screenshot",
            image_sources=[image_source],
            user_prompt=user,
        )

    @mcp.tool()
    async def understand_technical_diagram(
        image_source: str, prompt: str, diagram_type: Optional[str] = None
    ) -> dict:
        """Understand an architecture/flowchart/UML/ER/system diagram."""
        user = prompt + (f"\nDiagram type: {diagram_type}" if diagram_type else "")
        return await client.analyze_images(
            tool="understand_technical_diagram",
            image_sources=[image_source],
            user_prompt=user,
        )

    @mcp.tool()
    async def analyze_data_visualization(
        image_source: str, prompt: str, analysis_focus: Optional[str] = None
    ) -> dict:
        """Analyze a chart/plot (trends, anomalies, comparisons, distribution)."""
        user = prompt + (f"\nAnalysis focus: {analysis_focus}" if analysis_focus else "")
        return await client.analyze_images(
            tool="analyze_data_visualization",
            image_sources=[image_source],
            user_prompt=user,
        )

    @mcp.tool()
    async def ui_diff_check(
        expected_image_source: str, actual_image_source: str, prompt: str
    ) -> dict:
        """Compare EXPECTED (first) vs ACTUAL (second) UI for visual regression."""
        return await client.analyze_images(
            tool="ui_diff_check",
            image_sources=[expected_image_source, actual_image_source],
            user_prompt=prompt,
        )

    @mcp.tool()
    async def analyze_image(image_source: str, prompt: str) -> dict:
        """General visual analysis of an image."""
        return await client.analyze_images(
            tool="analyze_image", image_sources=[image_source], user_prompt=prompt
        )

    # -- alias (same implementation) -----------------------------------------
    @mcp.tool()
    async def image_analysis(image_source: str, prompt: str) -> dict:
        """Alias for analyze_image."""
        return await client.analyze_images(
            tool="image_analysis", image_sources=[image_source], user_prompt=prompt
        )

    return mcp
