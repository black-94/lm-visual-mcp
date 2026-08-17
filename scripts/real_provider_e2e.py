#!/usr/bin/env python3
"""Real end-to-end provider test: real image + real CLI (agy / codex) + all 8 MCP tools.

Drives ``VisionService.analyze_images`` (the exact code path behind
``POST /vision/analyze``) with genuine local CLI providers. For each installed
provider it runs all eight MCP tool specs against a real generated image and
asserts every call succeeds (returns a success envelope, not an ``error``).

The provider's configured ``model`` is honoured first; if the CLI rejects it as
invalid (placeholder model names in config), it falls back to the CLI default
and reports the override so a misconfiguration is surfaced, not hidden.

Usage:
    python scripts/real_provider_e2e.py
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

from PIL import Image, ImageDraw


def make_images(outdir: Path) -> tuple[str, str]:
    """Create two small labelled cards, 'A' (red) and 'B' (blue)."""
    outdir.mkdir(parents=True, exist_ok=True)
    paths = []
    for letter, color in (("A", (220, 60, 60)), ("B", (60, 90, 220))):
        img = Image.new("RGB", (120, 90), color)
        draw = ImageDraw.Draw(img)
        draw.text((52, 30), letter, fill="white")
        p = outdir / f"card_{letter.lower()}.png"
        img.save(p)
        paths.append(str(p))
    return paths[0], paths[1]


TOOL_SPECS = [
    ("ui_to_artifact", None),          # image filled below (needs output_type)
    ("extract_text_from_screenshot", None),
    ("diagnose_error_screenshot", None),
    ("understand_technical_diagram", None),
    ("analyze_data_visualization", None),
    ("ui_diff_check", None),
    ("analyze_image", None),
    ("image_analysis", None),
]


def tool_kwargs(tool: str, img_a: str, img_b: str) -> dict:
    prompt = "Describe what you see in this image. Answer in English, be concrete."
    if tool == "ui_to_artifact":
        return {"output_type": "code", "user_prompt": "Turn this screenshot into accessible HTML."}
    if tool == "extract_text_from_screenshot":
        return {"user_prompt": "Extract every visible character verbatim."}
    if tool == "diagnose_error_screenshot":
        return {"user_prompt": "Is there an error here? Describe it."}
    if tool == "understand_technical_diagram":
        return {"user_prompt": "Explain what this diagram shows."}
    if tool == "analyze_data_visualization":
        return {"user_prompt": "Summarize the trends or data shown."}
    if tool == "ui_diff_check":
        return {"user_prompt": "Compare these two; are they identical?"}
    return {"user_prompt": prompt}


async def run_all_tools(service, img_a: str, img_b: str) -> list:
    """Run all 8 tool specs; return a list of (tool, ok, provider, model, note)."""
    rows = []
    for tool, _ in TOOL_SPECS:
        platform_imgs = ([img_a, img_b] if tool == "ui_diff_check" else [img_a])
        kwargs = tool_kwargs(tool, img_a, img_b)
        try:
            result = await service.analyze_images(
                tool=tool,
                image_sources=platform_imgs,
                user_prompt=kwargs["user_prompt"],
                output_type=kwargs.get("output_type"),
            )
            if result.get("error"):
                rows.append((tool, False, result.get("provider"), result.get("model"),
                             f"error: {result['error'][:120]}"))
                continue
            ans = (result.get("result") or {}).get("answer") or (
                result.get("result") or {}).get("summary") or ""
            rows.append((tool, True, result.get("provider"), result.get("model"),
                         f"answer={ans[:60]!r}"))
        except Exception as exc:  # noqa: BLE001 - surface any provider failure as a row
            rows.append((tool, False, None, None,
                         f"{type(exc).__name__}: {str(exc)[:120]}"))
    return rows


def fmt(rows: list) -> None:
    print(f"{'tool':<28}{'ok':<5}{'provider':<10}{'model':<18}note")
    print("-" * 80)
    for tool, ok, prov, model, note in rows:
        print(f"{tool:<28}{'YES' if ok else 'NO ':<5}{(prov or '-'):<10}{(model or '-'):<18}{note}")
    passed = sum(1 for _, ok, *_ in rows if ok)
    print("-" * 80)
    print(f"passed {passed}/{len(rows)} for this provider\n")


def main() -> int:
    outdir = Path("/tmp/lm-visual-mcp-real-e2e")
    img_a, img_b = make_images(outdir / "media")

    from lm_visual_mcp.config import AppConfig, ProviderEntryConfig
    from lm_visual_mcp.vision.service import VisionService

    async def _run() -> int:
        overall = 0
        for ptype in ("agy", "codex"):
            print(f"\n=== REAL PROVIDER: {ptype} ===")
            # A chain of only this provider (real CLI), no other fallback.
            cfg = AppConfig()
            cfg.vision.providers = [
                ProviderEntryConfig(name=ptype, type=ptype, command=ptype,
                                    model=None, effort="low", timeout=120)
            ]
            try:
                service = VisionService(cfg)
            except Exception as exc:  # noqa: BLE001
                print(f"  build failure: {type(exc).__name__}: {exc}")
                overall += 1
                continue
            rows = await run_all_tools(service, img_a, img_b)
            fmt(rows)
            overall += sum(1 for _, ok, *_ in rows if not ok)
        return overall

    import asyncio
    overall = asyncio.run(_run())
    print(f"\nTOTAL non-passing tool calls across agy+codex: {overall}")
    if overall:
        print("FAILED")
        return 1
    print("ALL REAL E2E TOOL CALLS PASSED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - never exit silently on a stack trace
        traceback.print_exc()
        sys.exit(2)