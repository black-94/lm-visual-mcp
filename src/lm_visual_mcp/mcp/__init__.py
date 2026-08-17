"""MCP module: the stdio MCP entry presented to coding agents.

Deliberately thin - whether the shared server is started from here is decided
by the agent's MCP configuration (``--start-server`` / ``--no-start-server`` /
``LM_VISUAL_MCP_START_SERVER``), never by the YAML config file. All tool
execution lives in the shared server process.
"""

from __future__ import annotations

from .client import RemoteVision, connect
from .server import build_mcp

__all__ = ["RemoteVision", "connect", "build_mcp"]
