"""Real end-to-end MCP smoke test over stdio (no mocking).

Usage:
    python tests/real_smoke.py [config] [image] [--console]

Launches the server (via `python -m lm_visual_mcp` by default, or the `lm-visual-mcp`
console script with `--console`) and performs tools/list + a real tools/call
using the configured providers. Generates a test image if none is supplied.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

DEFAULT_CONFIG = "/tmp/vm.yaml"


def _make_image(path: Path) -> Path:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (520, 100), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 40)
    except Exception:  # noqa: BLE001
        font = ImageFont.load_default()
    d.text((20, 20), "VISION_TEST_7391", fill="black", font=font)
    img.save(path)
    return path


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("config", nargs="?", default=DEFAULT_CONFIG)
    ap.add_argument("image", nargs="?", default="")
    ap.add_argument("--console", action="store_true", help="use the lm-visual-mcp console script")
    args = ap.parse_args()

    tmp: Path | None = None
    if args.image:
        image = Path(args.image)
        if not image.exists():
            print(f"image not found: {args.image}; generating a test image", file=sys.stderr)
            tmp = Path(tempfile.mkdtemp()) / "vt.png"
            image = _make_image(tmp)
    else:
        tmp = Path(tempfile.mkdtemp()) / "vt.png"
        image = _make_image(tmp)

    command = "lm-visual-mcp" if args.console else sys.executable
    cmd_args = [] if args.console else ["-m", "lm_visual_mcp"]
    params = StdioServerParameters(command=command, args=[*cmd_args, "--config", args.config])

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print("tools/list ->", len(names), "tools")
            for n in sorted(names):
                print("  -", n)

            assert "analyze_image" in names, "analyze_image missing"
            assert "image_analysis" in names, "alias missing"
            assert "ui_diff_check" in names, "ui_diff_check missing"

            result = await session.call_tool(
                "analyze_image",
                {"image_source": str(image),
                 "prompt": "Read the exact visible text in the image."},
            )
            payload = json.loads(result.content[0].text)
            print("\ntools/call analyze_image ->")
            print("  provider:", payload["provider"], "| model:", payload["model"])
            print("  answer:", payload["result"].get("answer", "")[:80])
            print("  fallbacks:", [(f["provider"], f["reason"]) for f in payload["meta"]["fallbacks"]])
            if payload.get("error"):
                print("  error:", payload["error"])
    if tmp:
        tmp.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))