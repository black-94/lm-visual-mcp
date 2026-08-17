"""lm_visual_mcp - Vision MCP Server.

Give text-only LLMs / coding agents visual capabilities over the Model
Context Protocol (MCP). Three modules:

- ``vision``: image-recognition capability (provider chain + per-provider rate
  limiting + fallback).
- ``server``: the shared singleton server (vision endpoint + request hooks).
- ``mcp``: the thin stdio MCP client presented to coding agents.
"""

__version__ = "0.2.0"

__all__ = ["__version__"]
