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
    daemon = sub.add_parser("daemon", help="Run as the shared single-instance daemon")
    _add_common_args(daemon, suppress_default=True)
    return parser


def _serve(cfg, config_path: Optional[str]) -> int:
    """Client mode: probe the shared daemon, start it if absent, then proxy.

    Presents the normal stdio MCP server to Claude Code; every tool call is
    forwarded over loopback HTTP to the one global daemon.
    """
    from .server import build_server
    from .services import ProxyVisionSession, probe_primary, start_primary

    host, port = cfg.runtime.host, cfg.runtime.port
    if not probe_primary(host, port):
        logger.info("no shared daemon on %s:%s; starting one", host, port)
        if not start_primary(cfg, config_path, host, port):
            if not probe_primary(host, port):
                logger.error("could not start shared daemon on %s:%s", host, port)
                return 1

    session = ProxyVisionSession(cfg, host, port, config_path=config_path)
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
            key = pc.effective_api_key()
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

    if args.command == "daemon":
        from .services import run_daemon

        return run_daemon(cfg)

    return _serve(cfg, args.config)


if __name__ == "__main__":
    raise SystemExit(main())