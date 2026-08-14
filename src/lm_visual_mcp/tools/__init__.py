"""Vision tools executor.

:class:`VisionSession` is the provider-neutral bridge between MCP tool handlers
and the router. It resolves media sources, stages them into a per-task
workspace, builds the specialized :class:`VisionRequest`, routes it, and wraps
the result in the standard MCP response envelope.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from ..config import AppConfig
from ..errors import AllProvidersFailedError, MediaError, VisionError
from ..models import ImageInput, VideoInput, VisionRequest
from ..prompts import get_system_prompt
from ..router import ProviderRouter, RoutedResponse
from ..schema import VISION_RESULT_SCHEMA
from ..services import MediaService, Workspace, WorkspaceManager

logger = logging.getLogger("lm_visual_mcp.tools")


class VisionSession:
    def __init__(self, cfg: AppConfig, router: Optional[ProviderRouter] = None) -> None:
        self.cfg = cfg
        self.workspaces = WorkspaceManager(
            base=Path(cfg.runtime.workdir) if cfg.runtime.workdir else None
        )
        self.router = router or ProviderRouter(cfg)
        # Serializes request execution across every caller (all MCP sessions in
        # the shared lm-vision-server funnel through this one session). Requests
        # beyond runtime.max_concurrency queue here; set it to 1 for strict serial.
        self._sem = asyncio.Semaphore(cfg.runtime.max_concurrency)

    def _make_media_service(self, workdir: Optional[Path] = None) -> MediaService:
        """Create a per-request MediaService to avoid shared-state races."""
        return MediaService(
            max_image_mb=self.cfg.media.max_image_mb,
            max_video_mb=self.cfg.media.max_video_mb,
            download_timeout=self.cfg.media.download_timeout,
            max_download_mb=self.cfg.media.max_download_mb,
            workdir=workdir,
        )

    # -- image tool ---------------------------------------------------------
    async def analyze_images(
        self,
        *,
        tool: str,
        image_sources: list[str],
        user_prompt: str,
        output_type: Optional[str] = None,
    ) -> dict:
        system_prompt = get_system_prompt(tool, output_type)
        return await self._run(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            image_sources=image_sources,
            output_type=output_type,
        )

    async def analyze_video(
        self,
        *,
        tool: str,
        video_source: str,
        user_prompt: str,
    ) -> dict:
        system_prompt = get_system_prompt(tool)
        return await self._run(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            video_sources=[video_source],
        )

    # -- core ----------------------------------------------------------------
    async def _run(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        image_sources: Optional[list[str]] = None,
        video_sources: Optional[list[str]] = None,
        output_type: Optional[str] = None,
    ) -> dict:
        async with self._sem:
            return await self._run_locked(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                image_sources=image_sources or [],
                video_sources=video_sources or [],
                output_type=output_type,
            )

    async def _run_locked(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        image_sources: list[str],
        video_sources: list[str],
        output_type: Optional[str] = None,
    ) -> dict:
        workspace = self.workspaces.create()
        try:
            request = self._build_request(
                workspace=workspace,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                image_sources=image_sources,
                video_sources=video_sources,
            )
            routed = await self.router.route(request)
            return self._envelope(routed)
        except AllProvidersFailedError as exc:
            return self._error_envelope(exc)
        except (MediaError, VisionError) as exc:
            return self._error_envelope(exc)
        finally:
            workspace.cleanup()

    def _build_request(
        self,
        *,
        workspace: Workspace,
        system_prompt: str,
        user_prompt: str,
        image_sources: list[str],
        video_sources: list[str],
    ) -> VisionRequest:
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
        videos: list[VideoInput] = []
        for source in video_sources:
            resolved = media.resolve_video(source)
            staged = workspace.stage_media(resolved.local_path)
            videos.append(
                VideoInput(source=source, local_path=str(staged), mime_type=resolved.mime_type)
            )
        return VisionRequest(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            images=images,
            videos=videos,
            output_schema=VISION_RESULT_SCHEMA,
            workdir=workspace.root,
            timeout=self.cfg.runtime.timeout,
        )

    # -- envelope -------------------------------------------------------------
    @staticmethod
    def _envelope(routed: RoutedResponse) -> dict:
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