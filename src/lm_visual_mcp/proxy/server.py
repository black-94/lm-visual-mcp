"""Vision Proxy HTTP server.

Transparent forwarder + image preprocessor. No image -> byte-level passthrough
(only hop-by-hop headers stripped, API keys and bodies untouched). Image present
-> describe once (per-image SHA-256 cache) and rewrite the image parts into
text. Responses (including SSE) are piped back untouched except for the known
Claude Code Auto classifier stage-one compatibility normalization.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from .. import __version__
from ..config import AppConfig
from ..providers import build_registry
from ..router import ProviderRouter
from ..services.media import MediaService
from .cache import VisionCache
from .classifier import (
    disable_auto_classifier_thinking,
    is_auto_classifier_request,
    is_auto_classifier_stage1_request,
    normalize_auto_classifier_response,
)
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
        # Persistent workdir for proxy temp files; cleaned up on shutdown.
        self._media_workdir = Path("~/.cache/lm-visual-mcp/proxy-media").expanduser()
        self._media_workdir.mkdir(parents=True, exist_ok=True)
        self.media = MediaService(
            max_image_mb=cfg.media.max_image_mb,
            max_video_mb=cfg.media.max_video_mb,
            download_timeout=cfg.media.download_timeout,
            max_download_mb=cfg.media.max_download_mb,
            workdir=self._media_workdir,
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
        # Clean up proxy temp media files.
        import shutil
        shutil.rmtree(self._media_workdir, ignore_errors=True)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        try:
            return await self._handle(request)
        except ProxyError as exc:
            return web.Response(status=exc.status, text=exc.message)
        except Exception as exc:  # noqa: BLE001 - never leak internals to client
            logger.exception("proxy request failed")
            return web.Response(status=500, text="proxy error")

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        proto, target, suffix = self._parse_target(request.path)
        adapter = self._adapters.get(proto)
        body = await request.read()
        classifier_request = proto == "anthropic" and is_auto_classifier_request(body)
        classifier_stage1 = classifier_request and is_auto_classifier_stage1_request(body)
        if classifier_request and self.cfg.proxy.classifier.disable_thinking:
            body, _ = disable_auto_classifier_thinking(body)
        if adapter is None or not adapter.has_image(body):
            return await self._forward(
                request,
                target,
                suffix,
                body,
                classifier_stage1,
            )

        try:
            extracted = adapter.extract(body, self.media)
        except Exception as exc:  # noqa: BLE001 - fall back to transparent
            logger.warning("proxy parse failed for %s; forwarding raw: %s", proto, exc)
            return await self._forward(
                request,
                target,
                suffix,
                body,
                classifier_stage1,
            )
        if not extracted.slots:
            return await self._forward(
                request,
                target,
                suffix,
                body,
                classifier_stage1,
            )

        descs = await self._describe_cached(extracted.slots)
        for slot, desc in zip(extracted.slots, descs):
            slot.apply(desc)
        return await self._forward(
            request,
            target,
            suffix,
            serialize(extracted.doc),
            classifier_stage1,
        )

    def _parse_target(self, path: str) -> tuple[str, str, str]:
        """Return ``(protocol_path, target_url, suffix_path)`` from ``/proxy/<proto>/<b64>...``.

        The base64 segment encodes the *base* upstream URL. SDKs (e.g. the
        Anthropic SDK used by Claude Code) append the endpoint path onto the
        configured ``base_url``, so the request arrives with a trailing suffix
        (e.g. ``/v1/messages``). We match the protocol path as a prefix, scan
        the remaining segments for the one that base64url-decodes to a full
        http(s) URL, and return the later segments as ``suffix_path`` so the
        forwarder can rebase them onto the decoded target — the upstream
        gateway expects the full path (base + endpoint).
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
        target = None
        for i in range(consumed, len(parts)):
            try:
                decoded = base64.urlsafe_b64decode(_pad(parts[i])).decode("utf-8")
            except Exception:  # noqa: BLE001 - not this segment
                continue
            if decoded.startswith(("http://", "https://")):
                target = decoded
                consumed = i + 1
                break
        if target is None:
            raise ProxyError(400, "missing base64url ref")
        suffix = "/" + "/".join(quote(s, safe="") for s in parts[consumed:]) if parts[consumed:] else ""
        return proto, target, suffix

    # -- describe (per-image cache, batched vision call) --------------------
    async def _describe_cached(self, slots: list[ImageSlot]) -> list[str]:
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
            results = await describe(
                self.router, [slots[i].image for i in missed], self.cfg.runtime.timeout
            )
            for k, i in enumerate(missed):
                txt = results[k] if k < len(results) else ""
                descs[i] = txt
                await self.cache.aput(self.cache.key_of_file(slots[i].image.local_path), txt)
        return descs

    # -- forwarding ---------------------------------------------------------
    async def _get_session(self):
        if self._session is None:
            import aiohttp

            self._session = aiohttp.ClientSession(
                # Byte-level transparency: forward raw upstream bytes (and their
                # Content-Encoding) untouched instead of having aiohttp decode
                # them. The classifier path below decompresses explicitly when
                # it needs to parse JSON.
                auto_decompress=False,
                # Don't inject aiohttp's own Accept-Encoding / Accept / User-Agent
                # into the forward request; only the caller's headers go out.
                skip_auto_headers=("*",),
            )
        return self._session

    async def _forward(
        self,
        request: web.Request,
        target: str,
        suffix: str,
        body: bytes,
        normalize_classifier_stage1: bool,
    ) -> web.StreamResponse:
        session = await self._get_session()
        url = target if not suffix else target.rstrip("/") + suffix
        out_headers = _passthrough_headers(request.headers)
        out_headers["Host"] = _host_of(url)
        out_headers["Content-Length"] = str(len(body))
        async with session.request(
            request.method, url, headers=out_headers, data=body, allow_redirects=False
        ) as resp:
            resp_headers = _passthrough_headers(resp.headers)
            resp_headers.pop("Content-Length", None)  # streamed; aiohttp frames it
            if (
                normalize_classifier_stage1
                and resp.status == 200
                and "json" in (resp.headers.get("Content-Type") or "").lower()
            ):
                upstream_body = await _read_decompressed(resp)
                rewritten_body = normalize_auto_classifier_response(upstream_body)[0]
                # The payload is now decoded (and possibly rewritten), so
                # encodings and validators tied to the upstream bytes no longer
                # describe what we're about to send.
                resp_headers = _decompressed_body_headers(resp_headers)
                return web.Response(
                    status=resp.status,
                    headers=resp_headers,
                    body=rewritten_body,
                )
            stream = web.StreamResponse(status=resp.status, headers=resp_headers)
            await stream.prepare(request)
            # auto_decompress=False: resp.content yields the raw upstream bytes,
            # so a gzip body is forwarded gzip and the client decompresses it.
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


async def _read_decompressed(resp) -> bytes:
    """Read the upstream body, decoding per its ``Content-Encoding``.

    The proxy session runs with ``auto_decompress=False`` so `resp.content`
    yields raw bytes. Callers that must parse the body (classifier
    normalization) decompress explicitly here; on unknown/absent encodings the
    raw bytes are returned untouched.
    """
    body = await resp.content.read()
    enc = (resp.headers.get("Content-Encoding") or "").lower()
    if enc == "gzip":
        import gzip

        return gzip.decompress(body)
    if enc == "deflate":
        import zlib

        try:
            return zlib.decompress(body)
        except zlib.error:
            return zlib.decompress(body, -zlib.MAX_WBITS)  # raw deflate
    if enc == "br":
        try:
            import brotli
        except ImportError:
            return body
        return brotli.decompress(body)
    return body


def _decompressed_body_headers(headers: dict) -> dict:
    """Drop headers that no longer describe the body being forwarded.

    Used where the proxy has decoded/rewritten the upstream payload (classifier
    normalization), so the upstream ``Content-Encoding`` and byte-level
    validators no longer describe the plaintext bytes we send.
    """
    out = dict(headers)
    for key in ("Content-Encoding", "Content-MD5", "ETag"):
        out.pop(key, None)
    return out


def _host_of(target: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(target).netloc


def _pad(b64: str) -> str:
    return b64 + "=" * (-len(b64) % 4)
