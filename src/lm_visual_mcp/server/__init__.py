"""Server module: the shared singleton process.

Owns the vision endpoint (``POST /vision/analyze``) and the transparent hook
proxy (``/proxy/<proto>/<base64url>...``). Which hooks are active is pure
configuration: ``server.image_hook.enabled`` and
``server.classifier_hook.enabled``.
"""

from __future__ import annotations

from .app import VisionServerApp, run_server
from .hooks import Hook, HookContext, HookPipeline, HookResponse, HookResult
from .lifecycle import ensure_server, probe_server, start_server, stop_server

__all__ = [
    "VisionServerApp",
    "run_server",
    "Hook",
    "HookContext",
    "HookPipeline",
    "HookResponse",
    "HookResult",
    "ensure_server",
    "probe_server",
    "start_server",
    "stop_server",
]
