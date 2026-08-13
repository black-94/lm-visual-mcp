"""Vision Proxy: transparent HTTP forwarder + image preprocessor.

Reuses the existing ``lm_visual_mcp`` vision provider stack (router, providers,
media, config, prompts). Only three protocols are supported: OpenAI Chat
Completions, OpenAI Responses, and Anthropic Messages. No image -> bytes-level
transparent passthrough; image present -> describe once (per-image SHA-256
cache) and rewrite the image parts into text.
"""

from __future__ import annotations

from .server import VisionProxyApp, run_proxy

__all__ = ["VisionProxyApp", "run_proxy"]