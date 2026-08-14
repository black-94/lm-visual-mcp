"""Service layer: workspace, media, subprocess and JSON helpers."""

from .workspace import WorkspaceManager, Workspace
from .media import MediaService, ResolvedMedia
from .subprocess_runner import SubprocessRunner, SubprocessInvocation
from .json_output import extract_json, JsonExtractionError
from .control import ToolServer, run_server, default_pidfile
from .proxy import (
    ProxyVisionSession,
    probe_server,
    probe_proxy,
    start_server,
    start_proxy,
)

__all__ = [
    "WorkspaceManager",
    "Workspace",
    "MediaService",
    "ResolvedMedia",
    "SubprocessRunner",
    "SubprocessInvocation",
    "extract_json",
    "JsonExtractionError",
    "ToolServer",
    "run_server",
    "default_pidfile",
    "ProxyVisionSession",
    "probe_server",
    "probe_proxy",
    "start_server",
    "start_proxy",
]