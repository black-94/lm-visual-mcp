"""Command-line interface.

All logging goes to stderr; stdout is reserved for the MCP stdio protocol.

Default (no subcommand): the MCP stdio server. Whether it should also start
the shared vision server is passed here - from the agent's MCP config - via
``--start-server`` (default) / ``--no-start-server`` or
``LM_VISUAL_MCP_START_SERVER=0|1``; the YAML config file has no say in it.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Optional

from . import __version__
from .config import load_config

logger = logging.getLogger("lm_visual_mcp")

_START_SERVER_ENV = "LM_VISUAL_MCP_START_SERVER"


def _configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("lm_visual_mcp")
    root.handlers = [handler]
    root.setLevel(level.upper())


def _add_common_args(parser, *, suppress_default: bool) -> None:
    """Add --config / --log-level to a parser.

    ``suppress_default`` is used for subparsers so a value given on the main
    parser (before the subcommand) is not clobbered by the subparser's default.
    """
    parser.add_argument("--config", default=argparse.SUPPRESS if suppress_default else None,
                        help="Path to the config file")
    parser.add_argument("--log-level", default=argparse.SUPPRESS if suppress_default else None,
                        choices=["ERROR", "WARNING", "INFO", "DEBUG"],
                        help="Log level (stderr)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lm-visual-mcp", description="Vision MCP Server")
    _add_common_args(parser, suppress_default=False)
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    server_group = parser.add_mutually_exclusive_group()
    server_group.add_argument(
        "--start-server", dest="start_server", action="store_true",
        help="Start the shared vision server if absent (default)",
    )
    server_group.add_argument(
        "--no-start-server", dest="start_server", action="store_false",
        help="Never start the shared server; only use an already-running one",
    )
    parser.set_defaults(start_server=None)
    sub = parser.add_subparsers(dest="command")
    doc = sub.add_parser("doctor", help="Inspect the environment / configuration")
    _add_common_args(doc, suppress_default=True)
    doc.add_argument("--probe", action="store_true",
                     help="Run a real AGY vision smoke test (requires Pillow + agy)")
    srv = sub.add_parser("server", help="Run the shared vision server (foreground)")
    _add_common_args(srv, suppress_default=True)
    for action, help_text in (
        ("start", "Ensure the shared vision server is running"),
        ("stop", "Stop the shared vision server"),
        ("restart", "Restart the shared vision server"),
    ):
        p = sub.add_parser(action, help=help_text)
        _add_common_args(p, suppress_default=True)
    return parser


def _serve(cfg, config_path: Optional[str], start_server: bool) -> int:
    """Default command: MCP stdio server backed by the shared vision server."""
    from .mcp import build_mcp, connect

    client = connect(cfg, config_path, start_server=start_server)
    if client is None:
        # Still serve MCP: every tool call answers with an actionable error
        # envelope instead of failing the whole session.
        from .mcp.client import RemoteVision

        client = RemoteVision(cfg.server.host, cfg.server.port)
    mcp = build_mcp(client)
    mcp.run(transport="stdio")
    return 0


def doctor(cfg, *, probe: bool = False) -> int:
    from .server.lifecycle import probe_server
    from .vision.providers import PROVIDER_TYPES
    from .vision.router import VisionRouter
    from .vision.service import VisionService

    print("Vision MCP")
    print()
    host, port = cfg.server.host, cfg.server.port
    print(f"Server {host}:{port}: {'healthy' if probe_server(host, port) else 'not running'}")
    print(f"  image_hook: {'enabled' if cfg.server.image_hook.enabled else 'disabled'}")
    print(f"  classifier_hook: {'enabled' if cfg.server.classifier_hook.enabled else 'disabled'}")
    print()
    print("Provider chain (fallback order):")
    print("  " + " -> ".join(e.name for e in cfg.vision.providers if e.enabled))
    print()
    for entry in cfg.vision.providers:
        print(f"{entry.name} (type={entry.type})")
        print(f"  enabled: {'yes' if entry.enabled else 'no'}")
        rl = entry.rate_limit
        if rl.rpm is not None or rl.concurrency is not None:
            print(f"  rate_limit: rpm={rl.rpm} concurrency={rl.concurrency}")
        if entry.type == "gemini":
            key = entry.effective_api_key("GEMINI_API_KEY")
            print(f"  API key: {'configured' if key else 'not configured'}")
        elif entry.type == "opencode":
            key = entry.effective_api_key("OPENCODE_API_KEY")
            print(f"  API key: {'configured' if key else 'not configured'}")
            print(f"  base_url: {entry.base_url or 'default'}")
        else:
            exe = _which(entry.command or entry.type)
            print(f"  executable: {exe or 'not found'}")
        print(f"  model: {entry.model or 'default'}")
        print(f"  effort: {entry.effort or 'default'}")
        print()
    print("Runtime")
    print(f"  workdir: {cfg.vision.workdir or 'temporary'}")
    print(f"  timeout: {cfg.vision.timeout}")
    print(f"  max_concurrency: {cfg.vision.max_concurrency}")
    print()
    print("Fallback")
    print(f"  enabled: {cfg.vision.fallback.enabled}")
    print("  on: " + ", ".join(r.value for r in cfg.vision.fallback.reasons()))
    if probe:
        _probe_chain(cfg)
    return 0


def _probe_chain(cfg) -> None:
    """Probe each configured provider's availability (no vision call)."""
    import asyncio

    from .vision.providers import build_chain
    from .vision.router import VisionRouter

    router = VisionRouter(build_chain(cfg.vision))

    async def run():
        return await router.status()

    for status in asyncio.run(run()):
        state = "available" if status.available else f"unavailable ({status.message})"
        print(f"  {status.name}: {state}")


def _which(command: Optional[str]):
    if not command:
        return None
    import shutil
    from pathlib import Path

    if Path(command).is_file():
        return str(Path(command).resolve())
    return shutil.which(command)


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"lm-visual-mcp {__version__}")
        return 0

    try:
        cfg = load_config(config_path=args.config, log_level=args.log_level)
    except Exception as exc:  # noqa: BLE001
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    _configure_logging(args.log_level or cfg.logging.level)

    if args.command == "doctor":
        return doctor(cfg, probe=args.probe)

    if args.command == "server":
        from .server import run_server

        return run_server(cfg)

    if args.command in ("start", "stop", "restart"):
        from .server.lifecycle import probe_server, start_server, stop_server

        if args.command in ("stop", "restart"):
            _report(stop_server(cfg))
        if args.command in ("start", "restart"):
            if probe_server(cfg.server.host, cfg.server.port):
                _report({"service": "server", "status": "already-running", "port": cfg.server.port})
            else:
                ok = start_server(cfg, args.config)
                _report(
                    {"service": "server", "status": "started" if ok else "failed", "port": cfg.server.port}
                )
                return 0 if ok else 1
        return 0

    # Default: MCP stdio server.
    start_server = args.start_server
    if start_server is None:
        env_val = os.environ.get(_START_SERVER_ENV)
        if env_val is None:
            start_server = True
        else:
            start_server = env_val.strip().lower() not in {"0", "false", "no", "off"}
    return _serve(cfg, args.config, start_server)


def _report(res: dict) -> None:
    line = f"{res['service']}: {res['status']}"
    if res.get("port"):
        line += f" (:{res['port']})"
    print(line)


if __name__ == "__main__":
    raise SystemExit(main())
