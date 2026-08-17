"""Image request hook.

Detects image-bearing requests via the protocol adapters, describes each image
once (per-image SHA-256 cache, batched vision call through the router) and
rewrites the image parts into text. Deeper digging is left to the text model,
which can call the MCP vision tools - and reuse the absolute image paths this
hook records in every rewritten block.
"""

from __future__ import annotations

import logging

from ..media import MediaService
from ..vision.service import VisionService
from .cache import VisionCache
from .hooks import Hook, HookContext, HookResult
from .protocols import ProtocolAdapter
from .protocols.types import serialize

logger = logging.getLogger("lm_visual_mcp.server.image_hook")


class ImageHook(Hook):
    name = "image"

    def __init__(
        self,
        *,
        media: MediaService,
        vision: VisionService,
        cache: VisionCache | None = None,
        adapters: dict[str, ProtocolAdapter] | None = None,
        timeout: float | None = None,
    ) -> None:
        self.media = media
        self.vision = vision
        # NB: not `cache or VisionCache()` - an empty cache is falsy via
        # __len__ == 0, which would silently discard the caller's cache.
        self.cache = cache if cache is not None else VisionCache()
        self.adapters = adapters or {}
        self.timeout = timeout

    async def process(self, ctx: HookContext) -> HookResult:
        adapter = self.adapters.get(ctx.state.get("protocol", ""))
        if adapter is None or not adapter.has_image(ctx.body):
            return HookResult.passthrough()

        # Media is per-request: the server wiring injects a MediaService bound
        # to this request's workspace input dir. Fall back to the shared one
        # only when a per-request service wasn't provisioned (e.g. tests).
        media = ctx.state.get("media_service") or self.media
        try:
            extracted = adapter.extract(ctx.body, media)
        except Exception as exc:  # noqa: BLE001 - fall back to transparent passthrough
            logger.warning("image hook parse failed; forwarding raw: %s", exc)
            return HookResult.passthrough()
        if not extracted.slots:
            return HookResult.passthrough()

        descs = await self._describe_cached(extracted.slots)
        for slot, desc in zip(extracted.slots, descs):
            slot.apply(desc)
        logger.info(
            "image hook rewrote %d image(s) for %s", len(extracted.slots), ctx.url
        )
        return HookResult.rewrite(serialize(extracted.doc))

    async def process_response(self, ctx, status, headers, body):
        return None  # responses pass through untouched

    # -- describe (per-image cache, batched vision call) --------------------
    async def _describe_cached(self, slots) -> list[str]:
        n = len(slots)
        descs: list[str] = [""] * n
        missed: list[int] = []
        for i, slot in enumerate(slots):
            key = self.cache.key_of_file(slot.image.local_path)
            hit = await self.cache.aget(key)
            if hit is not None:
                descs[i] = hit
            else:
                missed.append(i)
        if missed:
            images = [slots[i].image for i in missed]
            results, provider_chain = await self.vision.describe(images, timeout=self.timeout)
            logger.info("DESCRIBE missed=%d providers=%s", len(missed), provider_chain)
            for k, i in enumerate(missed):
                txt = results[k] if k < len(results) else ""
                descs[i] = txt
                await self.cache.aput(
                    self.cache.key_of_file(slots[i].image.local_path),
                    txt,
                    provider=provider_chain,
                )
        return descs
