"""The shared singleton server.

One process, two capabilities:

- ``POST /vision/analyze``: the vision endpoint. MCP tool calls are forwarded
  here, so the provider chain, its rate limits and the concurrency gate are
  owned by exactly one process.
- ``ALL /proxy/<protocol-path>/<base64url>[/suffix]``: the transparent hook
  proxy for text-model clients. No hook intercepts -> byte-level passthrough
  (only hop-by-hop headers stripped, API keys and bodies untouched). Hooks may
  rewrite the request (image blocks -> text descriptions) or short-circuit it
  with a response of their own.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from .. import __version__
from ..config import AppConfig
from ..media import MediaService, WorkspaceManager
from ..vision.service import VisionService
from .cache import VisionCache
from ..paths import RUNTIME_DIR
from .classifier_hook import ClassifierHook
from .hooks import HookContext, HookPipeline, HookResponse
from .image_hook import ImageHook
from .protocols import build_registry as build_adapter_registry
from .protocols.types import serialize  # re-exported for tests

logger = logging.getLogger("lm_visual_mcp.server")

# Hop-by-hop headers (RFC 7230 §6.1) + ones we recompute. Everything else - most
# importantly Authorization / x-api-key - passes through untouched.
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

# aiohttp's Application default is 1MB; a single vision request can carry
# several base64 images well past that. Raise the ceiling so uploads are not
# dropped by the framework (media size is bounded separately by the config).
_MAX_BODY_BYTES = 100 * 1024 * 1024

# Disk log rotation: keep the tail of the server log, not unbounded growth.
_LOG_MAX_BYTES = 5 * 1024 * 1024
_LOG_BACKUP_COUNT = 3


class ProxyError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class VisionServerApp:
    def __init__(
        self,
        cfg: AppConfig,
        *,
        vision: VisionService | None = None,
        adapters: dict | None = None,
        session=None,
    ) -> None:
        self.cfg = cfg
        self.vision = vision or VisionService(cfg)
        # Per-task workspaces: each image-bearing proxy request gets its own
        # RUNTIME_DIR/<uuid> workspace so media lands in a single predictable
        # directory. Workspaces are retained (GC reclaims them) so the absolute
        # paths written into rewritten prompts stay valid for later use.
        self._workspaces = WorkspaceManager()
        # Fallback MediaService for hook code paths that didn't get a
        # per-request service via ctx.state (e.g. tests constructing the hook
        # directly). No global media cache anymore - media is per-request.
        self.media = MediaService(
            max_image_mb=cfg.media.max_image_mb,
            download_timeout=cfg.media.download_timeout,
            max_download_mb=cfg.media.max_download_mb,
        )
        self.cache = VisionCache()
        self._adapters = adapters or build_adapter_registry()
        self._session = session
        self.pipeline = self._build_pipeline()

    def _build_pipeline(self) -> HookPipeline:
        hooks = []
        scfg = self.cfg.server
        if scfg.image_hook.enabled:
            hooks.append(
                ImageHook(
                    media=self.media,
                    vision=self.vision,
                    cache=self.cache,
                    adapters=self._adapters,
                    timeout=self.cfg.vision.timeout,
                )
            )
        if scfg.classifier_hook.enabled:
            hooks.append(ClassifierHook(disable_thinking=scfg.classifier_hook.disable_thinking))
        return HookPipeline(hooks)

    def _make_request_media(self, workdir: Path) -> MediaService:
        """Build a MediaService bound to one request's workspace input dir."""
        m = self.cfg.media
        return MediaService(
            max_image_mb=m.max_image_mb,
            download_timeout=m.download_timeout,
            max_download_mb=m.max_download_mb,
            workdir=workdir,
        )

    # -- aiohttp -----------------------------------------------------------
    def build(self) -> web.Application:
        app = web.Application(client_max_size=_MAX_BODY_BYTES)
        app.router.add_get("/health", self.health)
        app.router.add_post("/vision/analyze", self.vision_analyze)
        app.router.add_route("*", "/{tail:.*}", self.handle_proxy)
        app.on_cleanup.append(self._close)
        return app

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "version": __version__,
                "hooks": [h.name for h in self.pipeline.hooks],
                "providers": [p.name for p in self.vision.router.providers],
                "pid": os.getpid(),
            }
        )

    async def vision_analyze(self, request: web.Request) -> web.Response:
        """The MCP-facing vision endpoint: one JSON in, envelope JSON out."""
        try:
            payload = json.loads(await request.read() or b"{}")
        except ValueError:
            return web.json_response({"error": "invalid JSON body"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"error": "expected JSON object"}, status=400)
        tool = payload.get("tool") or "analyze_image"
        image_sources = payload.get("image_sources") or []
        user_prompt = payload.get("user_prompt", "")
        output_type = payload.get("output_type")
        logger.info(
            "vision_analyze tool=%s images=%d user_prompt=%r",
            tool, len(image_sources), (user_prompt or "")[:80],
        )
        if not isinstance(image_sources, list) or not all(isinstance(s, str) for s in image_sources):
            return web.json_response({"error": "image_sources must be a list of strings"}, status=400)
        result = await self.vision.analyze_images(
            tool=tool,
            image_sources=image_sources,
            user_prompt=user_prompt,
            output_type=output_type,
        )
        return web.json_response(result)

    async def _close(self, app) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
        # NOTE: the media cache dir is intentionally NOT removed - staged
        # images are referenced by absolute path from rewritten prompts.

    # -- hook proxy ----------------------------------------------------------
    async def handle_proxy(self, request: web.Request) -> web.StreamResponse:
        try:
            return await self._handle_proxy(request)
        except ProxyError as exc:
            return web.Response(status=exc.status, text=exc.message)
        except Exception as exc:  # noqa: BLE001 - never leak internals to client
            logger.exception("proxy request failed")
            return web.Response(status=500, text="proxy error")

    async def _handle_proxy(self, request: web.Request) -> web.StreamResponse:
        proto, target, suffix = self._parse_target(request.path)
        body = await request.read()
        model = _model_of(body)

        state = {"protocol": proto}
        # Give image-bearing requests their own retained workspace so any media
        # they carry lands in a single predictable RUNTIME_DIR/<uuid> directory
        # (the absolute paths written into rewritten prompts stay valid).
        adapter = self._adapters.get(proto)
        if adapter is not None:
            try:
                if adapter.has_image(body):
                    ws = self._workspaces.create()
                    state["workspace"] = ws
                    state["media_service"] = self._make_request_media(ws.input_dir)
            except Exception:  # noqa: BLE001 - never break the proxy for media setup
                logger.exception("per-request media setup failed; forwarding raw")

        ctx = HookContext(
            method=request.method,
            url=target if not suffix else target.rstrip("/") + suffix,
            headers=dict(request.headers),
            body=body,
            state=state,
        )
        intercepted = await self.pipeline.run(ctx)
        if intercepted is not None:
            logger.info("HOOK %s %s -> intercepted by hook (%d)", request.method, ctx.url, intercepted.status)
            return web.Response(
                status=intercepted.status,
                headers=_drop_hop(intercepted.headers),
                body=intercepted.body,
            )

        direction = "REWRITTEN" if ctx.body is not body else "RAW"
        _entry_log(proto, model, len(body), direction, ctx.url)
        return await self._forward(request, ctx)

    def _parse_target(self, path: str) -> tuple[str, str, str]:
        """Return ``(protocol_path, target_url, suffix_path)`` from ``/proxy/<proto>/<b64>...``.

        The base64 segment encodes the *base* upstream URL. SDKs (e.g. the
        Anthropic SDK used by Claude Code) append the endpoint path onto the
        configured ``base_url``, so the request arrives with a trailing suffix
        (e.g. ``/v1/messages``). We match the protocol path as a prefix, scan
        the remaining segments for the one that base64url-decodes to a full
        http(s) URL, and return the later segments as ``suffix_path`` so the
        forwarder can rebase them onto the decoded target - the upstream
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

    async def _forward(self, request: web.Request, ctx: HookContext) -> web.StreamResponse:
        session = await self._get_session()
        body = ctx.body
        out_headers = _passthrough_headers(ctx.headers)
        out_headers["Host"] = _host_of(ctx.url)
        out_headers["Content-Length"] = str(len(body))
        async with session.request(
            request.method, ctx.url, headers=out_headers, data=body, allow_redirects=False
        ) as resp:
            resp_headers = _passthrough_headers(resp.headers)
            resp_headers.pop("Content-Length", None)  # streamed; aiohttp frames it
            logger.info("UPSTREAM %s %s -> %d", request.method, ctx.url, resp.status)
            if resp.status >= 400:
                raw = await resp.content.read()
                logger.info(
                    "UPSTREAM ERROR %d url=%s body=%s",
                    resp.status,
                    ctx.url,
                    _truncate_text(raw, 1200),
                )
                return web.Response(status=resp.status, headers=resp_headers, body=raw)
            if ctx.state.get("read_response_body") and resp.status == 200 and "json" in (
                resp.headers.get("Content-Type") or ""
            ).lower():
                upstream_body = await _read_decompressed(resp)
                status, headers, rewritten = await self.pipeline.run_response(
                    ctx, resp.status, dict(resp_headers), upstream_body
                )
                # The payload is now decoded (and possibly rewritten), so
                # encodings and validators tied to the upstream bytes no longer
                # describe what we're about to send.
                return web.Response(status=status, headers=headers, body=rewritten)
            stream = web.StreamResponse(status=resp.status, headers=resp_headers)
            await stream.prepare(request)
            # auto_decompress=False: resp.content yields the raw upstream bytes,
            # so a gzip body is forwarded gzip and the client decompresses it.
            async for chunk in resp.content.iter_any():
                await stream.write(chunk)
            return stream


def run_server(cfg: AppConfig) -> int:
    """Run the server until interrupted.

    Binds the socket before serving so a port conflict (another live server) is
    detected and exits quietly with rc 0 - the existing server keeps serving and
    concurrent MCP launches all connect to the winner (singleton).
    """
    _setup_server_logging()
    # VisionServerApp -> VisionService creates asyncio primitives (a Semaphore)
    # here, before the serving loop below exists. That is safe only because
    # asyncio locks/semaphores bind to their loop lazily (Python >= 3.10, and
    # this package requires >= 3.11); on 3.9 they would grab get_event_loop()
    # at construction and later fail with "attached to a different loop".
    runner = web.AppRunner(VisionServerApp(cfg).build())
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, cfg.server.host, cfg.server.port)
        loop.run_until_complete(site.start())
    except OSError:
        loop.run_until_complete(runner.cleanup())
        logger.info("server port %s:%s already in use; exiting", cfg.server.host, cfg.server.port)
        return 0
    from .lifecycle import server_pidfile, write_pidfile
    from ..paths import gc_runtime

    write_pidfile(server_pidfile())
    # Reclaim stale retained workspaces / description entries on boot. Best-effort;
    # never fail startup over cleanup.
    try:
        removed = gc_runtime()
        if removed["workspaces"] or removed["descriptions"]:
            logger.info("gc_runtime reclaimed %s", removed)
    except OSError:
        logger.warning("gc_runtime failed; continuing", exc_info=True)
    logger.info(
        "vision server listening on %s:%s (hooks and providers: see /health)",
        cfg.server.host,
        cfg.server.port,
    )
    try:
        loop.run_forever()
    finally:
        loop.close()
    return 0


# -- header helpers -----------------------------------------------------------


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


def _drop_hop(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP}


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


def _host_of(target: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(target).netloc


def _pad(b64: str) -> str:
    return b64 + "=" * (-len(b64) % 4)


def _model_of(body: bytes) -> str:
    """Best-effort extraction of the request ``model`` for the entry log."""
    try:
        doc = json.loads(body)
    except Exception:  # noqa: BLE001 - non-JSON body
        return "?"
    model = doc.get("model") if isinstance(doc, dict) else None
    return str(model) if model else "?"


def _truncate_text(raw: bytes, limit: int) -> str:
    """Decode upstream error bytes to text, truncated to ``limit`` chars."""
    text = raw.decode("utf-8", errors="replace")
    if len(text) > limit:
        text = text[:limit] + f"...({len(raw)} bytes)"
    return text


def _entry_log(proto: str, model: str, length: int, direction: str, url: str) -> None:
    logger.info("REQ proto=%s model=%s len=%d -> %s %s", proto, model, length, direction, url)


def _setup_server_logging() -> None:
    """Send the server's ``lm_visual_mcp`` logs to a rotating disk file.

    The server is detached (stderr usually DEVNULL), so a console handler is
    useless here. Best-effort: if the cache dir cannot be created, fall back to
    whatever handlers exist rather than crashing.
    """
    try:
        log_dir = Path("~/.cache/lm-visual-mcp").expanduser()
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            str(log_dir / "server.log"),
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root = logging.getLogger("lm_visual_mcp")
        root.handlers = [handler]
        root.setLevel(logging.INFO)
    except OSError:
        logger.warning("server disk logging unavailable; continuing without it")
