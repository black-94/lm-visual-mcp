"""Command-line interface: run the MCP server, doctor, --version.

All logging goes to stderr; stdout is reserved for the MCP stdio protocol.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Optional

from . import __version__
from .config import load_config

logger = logging.getLogger("lm_visual_mcp")


def _configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("lm_visual_mcp")
    root.handlers = [handler]
    root.setLevel(level.upper())
    # Redact secrets from all logs.
    _add_redaction_filter(root)


def _add_redaction_filter(logger_: logging.Logger) -> None:
    _secrets: list[str] = []

    class Redact(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()
            for secret in _secrets:
                if secret and secret in msg:
                    msg = msg.replace(secret, "[REDACTED]")
            record.msg = msg
            record.args = ()
            return True

    logger_.addFilter(Redact())


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
    sub = parser.add_subparsers(dest="command")
    doc = sub.add_parser("doctor", help="Inspect the environment / configuration")
    _add_common_args(doc, suppress_default=True)
    doc.add_argument("--probe", action="store_true",
                     help="Run a real AGY vision smoke test (requires Pillow + agy)")
    server = sub.add_parser("server", help="Run as the shared single-instance lm-vision-server")
    _add_common_args(server, suppress_default=True)
    proxy = sub.add_parser("proxy", help="Run the transparent lm-proxy (HTTP)")
    _add_common_args(proxy, suppress_default=True)
    proxy.add_argument("--host", help="Listen host (overrides config / env)")
    proxy.add_argument("--port", type=int, help="Listen port (overrides config / env)")
    for action, help_text in (
        ("start", "Ensure the lm-vision-server and lm-proxy singletons are running"),
        ("stop", "Stop the lm-vision-server and lm-proxy singletons"),
        ("restart", "Restart the lm-vision-server and lm-proxy singletons"),
    ):
        p = sub.add_parser(action, help=help_text)
        _add_common_args(p, suppress_default=True)
        p.add_argument("--service", choices=["server", "proxy"],
                       help="Target only this service (default: both)")
    return parser


def _serve(cfg, config_path: Optional[str]) -> int:
    """Client mode: probe the shared lm-vision-server, start it if absent, then proxy.

    Presents the normal stdio MCP server to Claude Code; every tool call is
    forwarded over loopback HTTP to the one global server. The server and proxy
    are probed and launched exactly once here; there is no runtime keep-alive.
    """
    from .server import build_server
    from .services import (
        ProxyVisionSession,
        probe_server,
        probe_proxy,
        start_server,
        start_proxy,
    )

    host, port = cfg.runtime.host, cfg.runtime.port
    if not probe_server(host, port):
        logger.info("no shared lm-vision-server on %s:%s; starting one", host, port)
        if not start_server(cfg, config_path, host, port):
            if not probe_server(host, port):
                logger.error("could not start shared lm-vision-server on %s:%s", host, port)
                return 1

    # Ensure the transparent lm-proxy is up too (singleton). It serves the
    # agent's text-model client; MCP vision tools still go through the server,
    # so a proxy startup failure is logged but does not block serving.
    phost, pport = cfg.proxy.host, cfg.proxy.port
    if not probe_proxy(phost, pport):
        logger.info("no lm-proxy on %s:%s; starting one", phost, pport)
        if not start_proxy(cfg, config_path):
            if not probe_proxy(phost, pport):
                logger.error("could not start lm-proxy on %s:%s", phost, pport)

    session = ProxyVisionSession(cfg, host, port)
    mcp = build_server(cfg, session=session)
    mcp.run(transport="stdio")
    return 0


def doctor(cfg, *, probe: bool = False) -> int:
    from .services import MediaService
    from .providers import build_registry
    from .router import ProviderRouter

    print("Vision MCP")
    print()
    print("Provider order:")
    print("  " + " -> ".join(cfg.providers.order))
    print()
    for name in cfg.providers.order:
        pc = cfg.providers.get(name)
        if pc is None:
            print(f"{name}: unknown")
            continue
        enabled = pc.enabled
        print(name)
        print(f"  enabled: {'yes' if enabled else 'no'}")
        if name == "gemini":
            import os
            key = pc.effective_api_key() or os.environ.get("GEMINI_API_KEY")
            print(f"  API key: {'configured' if key else 'not configured'}")
            print(f"  model: {pc.model or 'default'}")
            print(f"  effort: {pc.effort or 'default'}")
        else:
            exe = _which(pc.command)
            print(f"  executable: {exe or 'not found'}")
            print(f"  model: {pc.model or 'default'}")
            print(f"  effort: {pc.effort or 'default'}")
        if enabled and name == "agy" and probe:
            _probe_agy(cfg, pc)
        print()
    print("Runtime")
    print(f"  workdir: {cfg.runtime.workdir or 'temporary'}")
    print(f"  timeout: {cfg.runtime.timeout}")
    print(f"  max_concurrency: {cfg.runtime.max_concurrency}")
    print()
    print("Fallback")
    print(f"  enabled: {cfg.fallback.enabled}")
    print("  on: " + ", ".join(r.value for r in cfg.fallback.reasons()))
    return 0


def _lifecycle(cfg, args) -> int:
    """``start`` / ``stop`` / ``restart`` the server and proxy singletons."""
    from .services.lifecycle import SERVICES, service_targets, start_service, stop_service

    names = [args.service] if args.service else list(SERVICES)
    if args.command in ("stop", "restart"):
        for name in reversed(names):  # proxy first, then server
            _, port, pidfile = service_targets(cfg, name)
            _report(stop_service(name, port, pidfile))
    if args.command in ("start", "restart"):
        for name in names:
            _report(start_service(cfg, name, args.config))
    return 0


def _report(res: dict) -> None:
    line = f"{res['service']}: {res['status']}"
    if res.get("port"):
        line += f" (:{res['port']})"
    print(line)


def _apply_proxy_overrides(cfg, args) -> None:
    """Apply ``lm-visual-mcp proxy --host/--port`` over the loaded config."""
    if args.host:
        cfg.proxy.host = args.host
    if args.port:
        cfg.proxy.port = args.port


def _which(command: Optional[str]):
    if not command:
        return None
    import shutil
    from pathlib import Path

    if Path(command).is_file():
        return str(Path(command).resolve())
    return shutil.which(command)


def _probe_agy(cfg, pc) -> None:
    from .providers.agy import AgyProvider

    try:
        import PIL  # noqa: F401
    except ImportError:
        print("  agy vision probe: skipped (Pillow not installed)")
        return
    from .services.subprocess_runner import SubprocessRunner

    provider = AgyProvider(command=pc.command, model=pc.model, timeout=cfg.runtime.timeout,
                           runner=SubprocessRunner())

    # Build a real test image and ask AGY to read it.
    import tempfile
    from PIL import Image, ImageDraw, ImageFont
    from .models import ImageInput, VisionRequest

    with tempfile.TemporaryDirectory() as td:
        img = Image.new("RGB", (520, 100), "white")
        d = ImageDraw.Draw(img)
        try:
            f = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 40)
        except Exception:  # noqa: BLE001
            f = ImageFont.load_default()
        d.text((20, 20), "VISION_TEST_7391", fill="black", font=f)
        path = f"{td}/test.png"
        img.save(path)
        req = VisionRequest(
            system_prompt="You are a vision tester.",
            user_prompt="Read the exact text shown in the supplied image.",
            images=[ImageInput(source=path, local_path=path, mime_type="image/png")],
        )
        try:
            result = asyncio.run(provider.analyze(req))
            answer = result.result.get("answer", "")
            ok = answer and "7391" in answer
            print(f"  vision capability: {'available' if ok else 'unavailable'}")
            print(f"  agy answer: {answer[:80]!r}")
        except RuntimeError as exc:
            if "cannot be called" in str(exc) or "running event loop" in str(exc):
                print(f"  vision capability: skipped (cannot run probe inside existing event loop)")
            else:
                print(f"  vision capability: unsupported ({exc})")
        except Exception as exc:  # noqa: BLE001
            print(f"  vision capability: unsupported ({exc})")


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
        from .services import run_server

        return run_server(cfg)

    if args.command == "proxy":
        _apply_proxy_overrides(cfg, args)
        from .proxy import run_proxy

        return run_proxy(cfg)

    if args.command in ("start", "stop", "restart"):
        return _lifecycle(cfg, args)

    return _serve(cfg, args.config)


if __name__ == "__main__":
    raise SystemExit(main())