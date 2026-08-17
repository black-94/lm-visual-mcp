"""VisionService: the single owner of the provider chain.

Resolves media sources, stages them into a per-task workspace, builds the
specialized :class:`ImageRequest`, routes it through the fallback chain and
wraps the result in the standard envelope. Runs inside the shared server
process only - the MCP layer never instantiates it, so concurrency and rate
limits are always centrally enforced.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from ..config import AppConfig
from ..errors import AllProvidersFailedError, MediaError, VisionError
from ..media import MediaService, Workspace, WorkspaceManager
from .prompts import get_system_prompt
from .router import RoutedResult, VisionRouter
from .schema import VISION_RESULT_SCHEMA
from .types import ImageInput, ImageRequest

logger = logging.getLogger("lm_visual_mcp.vision.service")

# Lightweight per-image output schema used by the proxy's describe pass.
DESCRIBE_SCHEMA: dict = {
    "type": "object",
    "properties": {"images": {"type": "array", "items": {"type": "string"}}},
    "required": ["images"],
}

_DESCRIBE_USER = (
    'Describe each attached image: visible objects, text, layout, colors and '
    'spatial relationships. Return a JSON object with a single key "images": '
    'an array of strings, one per image, in the same order as the images. '
    'Do not answer questions or infer intent.'
)


class VisionService:
    def __init__(self, cfg: AppConfig, router: Optional[VisionRouter] = None) -> None:
        self.cfg = cfg
        self.workspaces = WorkspaceManager(
            base=Path(cfg.vision.workdir) if cfg.vision.workdir else None
        )
        if router is None:
            from .providers import build_chain

            router = VisionRouter(
                build_chain(cfg.vision),
                fallback_enabled=cfg.vision.fallback.enabled,
                fallback_on=cfg.vision.fallback.reasons(),
            )
        self.router = router
        # Serializes request execution across every caller (all MCP sessions
        # funnel through the one shared server). Requests beyond
        # vision.max_concurrency queue here; set it to 1 for strict serial.
        #
        # NOT reentrant: never call analyze_images()/describe() from inside a
        # block already holding this semaphore (e.g. a hook running within
        # another call's `async with self._sem`) - once max_concurrency slots
        # are taken the nested acquire waits forever, deadlocking the caller.
        self._sem = asyncio.Semaphore(cfg.vision.max_concurrency)

    def _make_media_service(self, workdir: Optional[Path] = None) -> MediaService:
        """Create a per-request MediaService to avoid shared-state races."""
        m = self.cfg.media
        return MediaService(
            max_image_mb=m.max_image_mb,
            download_timeout=m.download_timeout,
            max_download_mb=m.max_download_mb,
            workdir=workdir,
        )

    # -- image tool ----------------------------------------------------------
    async def analyze_images(
        self,
        *,
        tool: str,
        image_sources: list[str],
        user_prompt: str,
        output_type: Optional[str] = None,
    ) -> dict:
        system_prompt = get_system_prompt(tool, output_type)
        async with self._sem:
            workspace = self.workspaces.create()
            try:
                request = self._build_request(
                    workspace=workspace,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    image_sources=image_sources,
                )
                routed = await self.router.analyze(request)
                return self._envelope(routed)
            except AllProvidersFailedError as exc:
                return self._error_envelope(exc)
            except (MediaError, VisionError) as exc:
                return self._error_envelope(exc)
            finally:
                workspace.cleanup()

    # -- describe (server image hook) ----------------------------------------
    async def describe(self, images: list[ImageInput], timeout: Optional[float] = None) -> tuple[list[str], str]:
        """Run one generic description pass over already-local images.

        Returns ``(descriptions, provider_chain)``. The list is aligned with
        ``images`` (padded/truncated to match); the provider chain is a short
        observability string (e.g. ``"codex"`` or ``"codex+2fb"``).
        """
        if not images:
            return [], ""
        request = ImageRequest(
            system_prompt=get_system_prompt("describe_image"),
            user_prompt=_DESCRIBE_USER,
            images=images,
            output_schema=DESCRIBE_SCHEMA,
            timeout=timeout or self.cfg.vision.timeout,
        )
        async with self._sem:
            routed = await self.router.analyze(request)
        provider_chain = routed.provider
        if routed.fallbacks:
            provider_chain = f"{provider_chain}+{len(routed.fallbacks)}fb"
        arr = (routed.result.get("details") or {}).get("images")
        if not isinstance(arr, list):
            arr = [routed.result.get("answer") or routed.result.get("summary") or ""]
        descs = [_coerce(arr[k]) if k < len(arr) else "" for k in range(len(images))]
        return descs, provider_chain

    # -- core ----------------------------------------------------------------
    def _build_request(
        self,
        *,
        workspace: Workspace,
        system_prompt: str,
        user_prompt: str,
        image_sources: list[str],
    ) -> ImageRequest:
        # Per-request MediaService avoids shared mutable state across concurrent
        # requests (each gets its own workdir for remote downloads).
        media = self._make_media_service(workdir=workspace.root)
        images: list[ImageInput] = []
        for source in image_sources:
            resolved = media.resolve_image(source)
            staged = workspace.stage_media(resolved.local_path)
            images.append(
                ImageInput(source=source, local_path=str(staged), mime_type=resolved.mime_type)
            )
        return ImageRequest(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            images=images,
            output_schema=VISION_RESULT_SCHEMA,
            workdir=workspace.root,
            timeout=self.cfg.vision.timeout,
        )

    # -- envelope -------------------------------------------------------------
    @staticmethod
    def _envelope(routed: RoutedResult) -> dict:
        return {
            "provider": routed.provider,
            "model": routed.model,
            "result": routed.result,
            "meta": {
                "duration_ms": routed.duration_ms,
                "fallbacks": [
                    {"provider": f.provider, "reason": f.reason, "message": f.message}
                    for f in routed.fallbacks
                ],
                "usage": routed.usage,
            },
        }

    @staticmethod
    def _error_envelope(exc: Exception) -> dict:
        message = str(exc) or exc.__class__.__name__
        return {
            "provider": None,
            "model": None,
            "result": {
                "summary": "",
                "answer": "",
                "observations": [],
                "texts": [],
                "elements": [],
                "warnings": [message],
            },
            "meta": {
                "duration_ms": 0,
                "fallbacks": [],
                "usage": {
                    "input_tokens": None,
                    "output_tokens": None,
                    "thinking_tokens": None,
                    "cache_read_tokens": None,
                    "total_tokens": None,
                },
            },
            "error": str(exc),
        }


def _coerce(value) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)
