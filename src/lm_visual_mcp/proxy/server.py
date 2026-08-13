"""Vision Proxy HTTP server.

Transparent forwarder + image preprocessor. No image -> byte-level passthrough
(only hop-by-hop headers stripped, API keys and bodies untouched). Image present
-> describe once (per-image SHA-256 cache) and rewrite the image parts into
text. The response (including SSE) is always piped back untouched.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os

from aiohttp import web

from .. import __version__
from ..config import AppConfig
from ..providers import build_registry
from ..router import ProviderRouter
from ..services.media import MediaService
from .cache import VisionCache
from .describe import describe
from .detect import build_registry as build_adapter_registry
from .types import ImageSlot, serialize

logger = logging.getLogger("lm_visual_mcp.proxy")

# Hop-by-hop headers (RFC 7230 §6.1) + ones we recompute. Everything else — most
# importantly Authorization / x-api-key — passes through untouched.
_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "host",
}


class ProxyError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class VisionProxyApp:
    def __init__(
        self,
        cfg: AppConfig,
        *,
        router: ProviderRouter | None = None,
        adapters: dict | None = None,
        session=None,
    ) -> None:
        self.cfg = cfg
        self.router = router or ProviderRouter(cfg, build_registry(cfg))
        self.media = MediaService(
            max_image_mb=cfg.media.max_image_mb,
            max_video_mb=cfg.media.max_video_mb,
            download_timeout=cfg.media.download_timeout,
            max_download_mb=cfg.media.max_download_mb,
        )
        self.cache = VisionCache()
        self._adapters = adapters or build_adapter_registry()
        self._session = session

    # -- aiohttp -----------------------------------------------------------
    def build(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/health", self.health)
        app.router.add_route("*", "/{tail:.*}", self.handle)
        app.on_cleanup.append(self._close)
        return app

    async def health(self, request: web.Request) -> web.Response:
        """Singleton probe endpoint (mirrors the daemon's ``/health``)."""
        return web.json_response({"ok": True, "version": __version__, "pid": os.getpid()})

    async def _close(self, app) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def handle(self, request: web.Request) -> web.StreamResponse:
        try:
            return await self._handle(request)
        except ProxyError as exc:
            return web.Response(status=exc.status, text=exc.message)
        except Exception as exc:  # noqa: BLE001 - never leak internals to client
            logger.exception("proxy request failed")
            return web.Response(status=500, text="proxy error")

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        proto, target = self._parse_target(request.path)
        adapter = self._adapters.get(proto)

        body = await request.read()
        if not adapter.has_image(body):
            return await self._forward(request, target, body)

        try:
            extracted = adapter.extract(body, self.media)
        except Exception as exc:  # noqa: BLE001 - fall back to transparent
            logger.warning("proxy parse failed for %s; forwarding raw: %s", proto, exc)
            return await self._forward(request, target, body)
        if not extracted.slots:
            return await self._forward(request, target, body)

        descs = await self._describe_cached(extracted.slots)
        for slot, desc in zip(extracted.slots, descs):
            slot.apply(desc)
        return await self._forward(request, target, serialize(extracted.doc))

    def _parse_target(self, path: str) -> tuple[str, str]:
        """Return ``(protocol_path, target_url)`` from ``/proxy/<proto>/<b64>...``.

        Tolerant of a trailing suffix: SDKs (e.g. the Anthropic SDK used by
        Claude Code) append the endpoint path onto the configured ``base_url``,
        so the URL arrives as ``/proxy/<proto>/<b64>/v1/messages``. We match the
        protocol path as a prefix, then scan the remaining segments for the one
        that base64url-decodes to a full http(s) URL; any later segments are the
        appended suffix and ignored (the decoded target already carries its own
        path).
        """
        parts = path.strip("/").split("/")
        if len(parts) < 3 or parts[0] != "proxy":
            raise ProxyError(404, "expected path /proxy/<protocol-path>/<base64url>[/suffix]")
        # Longest protocol path first so "openai/chat" wins over "openai".
        proto = None
        consumed = 1  # parts[0] == "proxy"
        for candidate in sorted(self._adapters, key=len, reverse=True):
            segs = candidate.split("/")
            if parts[consumed:consumed + len(segs)] == segs:
                proto = candidate
                consumed += len(segs)
                break
        if proto is None:
            raise ProxyError(404, "unknown protocol path")
        for seg in parts[consumed:]:
            try:
                target = base64.urlsafe_b64decode(_pad(seg)).decode("utf-8")
            except Exception:  # noqa: BLE001 - not this segment
                continue
            if target.startswith(("http://", "https://")):
                return proto, target
        raise ProxyError(400, "missing base64url ref")

    # -- describe (per-image cache, batched vision call) --------------------
    async def _describe_cached(self, slots: list[ImageSlot]) -> list[str]:
        n = len(slots)
        descs: list[str] = [""] * n
        missed: list[int] = []
        for i, slot in enumerate(slots):
            key = self.cache.key_of_file(slot.image.local_path)
            hit = self.cache.get(key)
            if hit is not None:
                descs[i] = hit
            else:
                missed.append(i)
        if missed:
            results = await describe(
                self.router, [slots[i].image for i in missed], self.cfg.runtime.timeout
            )
            for k, i in enumerate(missed):
                txt = results[k] if k < len(results) else ""
                descs[i] = txt
                self.cache.put(self.cache.key_of_file(slots[i].image.local_path), txt)
        return descs

    # -- forwarding ---------------------------------------------------------
    async def _get_session(self):
        if self._session is None:
            import aiohttp

            self._session = aiohttp.ClientSession()
        return self._session

    async def _forward(self, request: web.Request, target: str, body: bytes) -> web.StreamResponse:
        session = await self._get_session()
        out_headers = _passthrough_headers(request.headers)
        out_headers["Host"] = _host_of(target)
        out_headers["Content-Length"] = str(len(body))
        async with session.request(request.method, target, headers=out_headers, data=body) as resp:
            resp_headers = _passthrough_headers(resp.headers)
            resp_headers.pop("Content-Length", None)  # streamed; aiohttp frames it
            stream = web.StreamResponse(status=resp.status, headers=resp_headers)
            await stream.prepare(request)
            async for chunk in resp.content.iter_any():
                await stream.write(chunk)
            return stream


def run_proxy(cfg: AppConfig) -> int:
    """Run the proxy until interrupted.

    Binds the socket before serving so a port conflict (another live proxy) is
    detected and exits quietly with rc 0 — the existing proxy keeps serving and
    concurrent MCP launches all connect to the winner (singleton).
    """
    runner = web.AppRunner(VisionProxyApp(cfg).build())
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, cfg.proxy.host, cfg.proxy.port)
        loop.run_until_complete(site.start())
    except OSError:
        loop.run_until_complete(runner.cleanup())
        logger.info("proxy port %s:%s already in use; exiting", cfg.proxy.host, cfg.proxy.port)
        return 0
    # Only the process that won the port bind writes the pidfile.
    from ..services.control import proxy_pidfile, write_pidfile

    write_pidfile(proxy_pidfile())
    logger.info("vision proxy listening on %s:%s", cfg.proxy.host, cfg.proxy.port)
    try:
        loop.run_forever()
    finally:
        loop.close()
    return 0


def _passthrough_headers(headers) -> dict:
    conn_tokens = {
        t.strip().lower()
        for k, v in headers.items()
        if k.lower() == "connection"
        for t in v.split(",")
        if t.strip()
    }
    out: dict = {}
    for key, value in headers.items():
        lk = key.lower()
        if lk in _HOP or lk in conn_tokens:
            continue
        out[key] = value
    return out


def _host_of(target: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(target).netloc


def _pad(b64: str) -> str:
    return b64 + "=" * (-len(b64) % 4)