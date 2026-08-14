"""Proxy unit + integration tests."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lm_visual_mcp.config import AppConfig
from lm_visual_mcp.models import ImageInput
from lm_visual_mcp.proxy.cache import VisionCache
from lm_visual_mcp.proxy.detect import build_registry
from lm_visual_mcp.proxy.types import serialize
from lm_visual_mcp.router import RoutedResponse
from lm_visual_mcp.schema import VisionResult, normalize_result
from lm_visual_mcp.services.media import MediaService

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
DATA_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()


class FakeRouter:
    """Returns fixed per-image descriptions without calling a real provider."""

    def __init__(self, images_list):
        self._results = images_list
        self.calls = 0

    async def route(self, request):
        self.calls += 1
        return RoutedResponse(
            provider="fake", model="m",
            result={"details": {"images": self._results}},
            usage={},
        )


def _media(tmp_path) -> MediaService:
    m = MediaService()
    m.workdir = Path(tmp_path)
    return m


def _openai_body(*refs):
    content = [{"type": "text", "text": "look"}]
    for r in refs:
        content.append({"type": "image_url", "image_url": {"url": r}})
    return json.dumps({"messages": [{"role": "user", "content": content}]}).encode()


def _b64(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


# -- registry ---------------------------------------------------------------
def test_registry_paths():
    assert set(build_registry()) == {"openai/chat", "openai/responses", "anthropic"}


# -- adapters ---------------------------------------------------------------
def test_openai_chat_rewrite(tmp_path):
    from lm_visual_mcp.proxy.openai_chat import OpenAIChatAdapter

    ad = OpenAIChatAdapter()
    body = _openai_body(DATA_URL)
    assert ad.has_image(body)
    ex = ad.extract(body, _media(tmp_path))
    assert len(ex.slots) == 1
    assert ex.slots[0].image.local_path  # resolved to a temp file
    ex.slots[0].apply("a red car")
    parts = json.loads(serialize(ex.doc))["messages"][0]["content"]
    assert parts[1] == {"type": "text", "text": "[Image 1]\na red car"}


def test_openai_chat_transparent_no_image(tmp_path):
    from lm_visual_mcp.proxy.openai_chat import OpenAIChatAdapter

    ad = OpenAIChatAdapter()
    body = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()
    assert not ad.has_image(body)


def test_openai_responses_rewrite(tmp_path):
    from lm_visual_mcp.proxy.openai_responses import OpenAIResponsesAdapter

    ad = OpenAIResponsesAdapter()
    body = json.dumps({"input": [{
        "role": "user",
        "content": [{"type": "input_image", "image_url": DATA_URL}],
    }]}).encode()
    assert ad.has_image(body)
    ex = ad.extract(body, _media(tmp_path))
    assert len(ex.slots) == 1
    ex.slots[0].apply("a chart")
    item = json.loads(serialize(ex.doc))["input"][0]["content"][0]
    assert item == {"type": "input_text", "text": "[Image 1]\na chart"}


def test_anthropic_rewrite(tmp_path):
    from lm_visual_mcp.proxy.anthropic import AnthropicAdapter

    ad = AnthropicAdapter()
    body = json.dumps({"messages": [{
        "role": "user",
        "content": [{
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png",
                       "data": base64.b64encode(PNG).decode()},
        }],
    }]}).encode()
    assert ad.has_image(body)
    ex = ad.extract(body, _media(tmp_path))
    assert len(ex.slots) == 1
    ex.slots[0].apply("a screenshot")
    block = json.loads(serialize(ex.doc))["messages"][0]["content"][0]
    assert block == {"type": "text", "text": "[Image 1]\na screenshot"}


def test_multi_image_same_request(tmp_path):
    from lm_visual_mcp.proxy.openai_chat import OpenAIChatAdapter

    ad = OpenAIChatAdapter()
    body = _openai_body(DATA_URL, DATA_URL)
    ex = ad.extract(body, _media(tmp_path))
    assert len(ex.slots) == 2
    ex.slots[0].apply("first")
    ex.slots[1].apply("second")
    parts = json.loads(serialize(ex.doc))["messages"][0]["content"]
    assert [p["text"] for p in parts[1:]] == ["[Image 1]\nfirst", "[Image 2]\nsecond"]


# -- cache ------------------------------------------------------------------
def test_cache_per_image():
    c = VisionCache()
    k = c.key_of_bytes(PNG)
    assert c.get(k) is None
    c.put(k, "a png")
    assert c.get(k) == "a png"
    assert c.key_of_bytes(PNG) == c.key_of_bytes(PNG)


# -- normalize_result preserves unknown keys --------------------------------
def test_normalize_preserves_unknown_keys():
    r = normalize_result({"images": ["a", "b"], "summary": "s"})
    assert r.details["images"] == ["a", "b"]
    assert r.summary == "s"


# -- Claude Code Auto classifier compatibility -----------------------------
def _classifier_body() -> bytes:
    return json.dumps({
        "model": "gateway-model-alias",
        "max_tokens": 2112,
        "stop_sequences": ["</block>"],
        "system": [
            {"type": "text", "text": "billing metadata"},
            {"type": "text", "text": (
                "You are a security monitor for autonomous AI coding agents.\n"
                "Return <block>yes</block> or <block>no</block>."
            ), "cache_control": {"type": "ephemeral"}},
        ],
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "<transcript>...action...</transcript>"},
        ]}],
    }).encode()


def test_detects_classifier_by_contract_not_model_or_token_count():
    from lm_visual_mcp.proxy.classifier import (
        is_auto_classifier_request,
        is_auto_classifier_stage1_request,
    )

    assert is_auto_classifier_request(_classifier_body()) is True
    assert is_auto_classifier_stage1_request(_classifier_body()) is True
    ordinary = json.dumps({
        "model": "claude-sonnet-5",
        "max_tokens": 64,
        "system": "ordinary prompt",
        "messages": [],
    }).encode()
    assert is_auto_classifier_request(ordinary) is False


def test_classifier_family_detection_does_not_require_stage_one_stop_sequence():
    from lm_visual_mcp.proxy.classifier import (
        is_auto_classifier_request,
        is_auto_classifier_stage1_request,
    )

    request = json.loads(_classifier_body())
    request["stop_sequences"] = ["</severity>"]
    body = json.dumps(request).encode()
    assert is_auto_classifier_request(body) is True
    assert is_auto_classifier_stage1_request(body) is False


def test_classifier_response_restores_anthropic_stop_sequence():
    from lm_visual_mcp.proxy.classifier import normalize_auto_classifier_response

    upstream = json.dumps({
        "id": "msg_gateway",
        "type": "message",
        "role": "assistant",
        "model": "actual-gateway-model",
        "content": [
            {"type": "thinking", "thinking": "analysis that must not lead content"},
            {"type": "text", "text": "<block>no</block>"},
        ],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 4},
    }).encode()
    body, changed = normalize_auto_classifier_response(upstream)
    assert changed is True
    result = json.loads(body)
    assert result["content"] == [{"type": "text", "text": "<block>no"}]
    assert result["stop_reason"] == "stop_sequence"
    assert result["stop_sequence"] == "</block>"
    assert result["usage"] == {"input_tokens": 10, "output_tokens": 4}


def test_classifier_yes_response_is_binary_stage_one_verdict():
    from lm_visual_mcp.proxy.classifier import normalize_auto_classifier_response

    upstream = json.dumps({
        "type": "message",
        "content": [{
            "type": "text",
            "text": (
                "analysis before verdict <block>yes</block>"
                "<category>Data Exfiltration</category>"
                "<reason>[Data Exfiltration] sends credentials</reason>"
            ),
        }],
        "stop_reason": "end_turn",
    }).encode()
    body, changed = normalize_auto_classifier_response(upstream)
    assert changed is True
    result = json.loads(body)
    # Stage one requested </block> as a stop sequence, so the normalized wire
    # result contains only the preliminary binary verdict. Claude Code decides
    # whether a second classifier stage is needed.
    assert result["content"] == [{"type": "text", "text": "<block>yes"}]
    assert result["stop_reason"] == "stop_sequence"


def test_classifier_request_disables_thinking():
    from lm_visual_mcp.proxy.classifier import disable_auto_classifier_thinking

    body, changed = disable_auto_classifier_thinking(_classifier_body())
    assert changed is True
    assert json.loads(body)["thinking"] == {"type": "disabled"}
    body_again, changed_again = disable_auto_classifier_thinking(body)
    assert changed_again is False
    assert body_again == body


def test_classifier_response_without_verdict_is_not_rewritten():
    from lm_visual_mcp.proxy.classifier import normalize_auto_classifier_response

    upstream = json.dumps({
        "type": "message",
        "content": [{"type": "text", "text": "I cannot decide"}],
    }).encode()
    body, changed = normalize_auto_classifier_response(upstream)
    assert changed is False
    assert body == upstream


def test_classifier_response_with_conflicting_verdicts_is_not_rewritten():
    from lm_visual_mcp.proxy.classifier import normalize_auto_classifier_response

    upstream = json.dumps({
        "type": "message",
        "content": [{"type": "text", "text": "<block>yes</block> <block>no</block>"}],
    }).encode()
    body, changed = normalize_auto_classifier_response(upstream)
    assert changed is False
    assert body == upstream


def test_classifier_response_accepts_already_truncated_verdict():
    from lm_visual_mcp.proxy.classifier import normalize_auto_classifier_response

    upstream = json.dumps({
        "type": "message",
        "content": [{"type": "text", "text": "<block>no"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
    }).encode()
    body, changed = normalize_auto_classifier_response(upstream)
    assert changed is True
    result = json.loads(body)
    assert result["content"] == [{"type": "text", "text": "<block>no"}]
    assert result["stop_reason"] == "stop_sequence"
    assert result["stop_sequence"] == "</block>"


# -- server integration -----------------------------------------------------
@pytest.mark.asyncio
async def test_transparent_path_does_not_describe(unused_tcp_port):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    seen = {}

    async def origin_h(request):
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = await request.read()
        return web.Response(text="pong")

    origin = web.Application()
    origin.router.add_post("/v1/chat/completions", origin_h)
    origin_server = TestServer(origin, port=unused_tcp_port)
    await origin_server.start_server()

    target = f"http://127.0.0.1:{origin_server.port}/v1/chat/completions"
    fake = FakeRouter(["should-not-run"])
    proxy = _app(fake)
    client = TestClient(TestServer(proxy.build()))
    await client.start_server()
    try:
        resp = await client.post(
            f"/proxy/openai/chat/{_b64(target)}",
            data=json.dumps({"messages": [{"role": "user", "content": "hi"}]}),
            headers={"Authorization": "Bearer sk-x"},
        )
        assert resp.status == 200
        await resp.read()
        assert fake.calls == 0  # no image -> describe never called
        assert seen["auth"] == "Bearer sk-x"
        assert json.loads(seen["body"]) == {"messages": [{"role": "user", "content": "hi"}]}
    finally:
        await client.close()
        await origin_server.close()


@pytest.mark.asyncio
async def test_image_path_describes_and_rewrites(unused_tcp_port):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    captured = {}

    async def origin_h(request):
        captured["body"] = await request.read()
        return web.Response(text="ok")

    origin = web.Application()
    origin.router.add_post("/v1/messages", origin_h)
    origin_server = TestServer(origin, port=unused_tcp_port)
    await origin_server.start_server()

    target = f"http://127.0.0.1:{origin_server.port}/v1/messages"
    fake = FakeRouter(["a debug screenshot"])
    proxy = _app(fake)
    client = TestClient(TestServer(proxy.build()))
    await client.start_server()
    try:
        body = json.dumps({"messages": [{
            "role": "user",
            "content": [{
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png",
                           "data": base64.b64encode(PNG).decode()},
            }],
        }]}).encode()
        resp = await client.post(f"/proxy/anthropic/{_b64(target)}", data=body)
        assert resp.status == 200
        await resp.read()
        assert fake.calls == 1
        sent = json.loads(captured["body"])
        assert sent["messages"][0]["content"] == [
            {"type": "text", "text": "[Image 1]\na debug screenshot"}
        ]
    finally:
        await client.close()
        await origin_server.close()


# -- target parsing (SDK-appended suffix tolerance) ---------------------------
def test_parse_target_tolerates_sdk_suffix():
    from lm_visual_mcp.proxy.server import VisionProxyApp

    app = VisionProxyApp(AppConfig())
    # Base URL encoded; Claude Code / Anthropic SDK appends /v1/messages —
    # the suffix is returned so the forwarder rebases it onto the target.
    proto, got, suffix = app._parse_target(
        f"/proxy/anthropic/{_b64('https://api.anthropic.com')}/v1/messages"
    )
    assert (proto, got, suffix) == ("anthropic", "https://api.anthropic.com", "/v1/messages")
    # openai/chat protocol path + SDK-appended /v1/chat/completions
    proto, got, suffix = app._parse_target(
        f"/proxy/openai/chat/{_b64('https://api.deepseek.com')}/v1/chat/completions"
    )
    assert (proto, got, suffix) == (
        "openai/chat", "https://api.deepseek.com", "/v1/chat/completions"
    )
    # raw curl without a suffix still works (empty suffix)
    proto, got, suffix = app._parse_target(
        f"/proxy/anthropic/{_b64('https://api.anthropic.com/v1/messages')}"
    )
    assert (proto, got, suffix) == ("anthropic", "https://api.anthropic.com/v1/messages", "")


def test_parse_target_rejects_bad_path():
    from lm_visual_mcp.proxy.server import ProxyError, VisionProxyApp

    app = VisionProxyApp(AppConfig())
    with pytest.raises(ProxyError):
        app._parse_target("/proxy/bogus/aGVsbG8=")
    with pytest.raises(ProxyError):
        app._parse_target("/proxy/anthropic/not-a-base64-ref")


@pytest.mark.asyncio
async def test_end_to_end_with_sdk_suffix(unused_tcp_port):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    captured = {}

    async def origin_h(request):
        captured["body"] = await request.read()
        return web.Response(text="ok")

    origin = web.Application()
    origin.router.add_post("/v1/messages", origin_h)
    origin_server = TestServer(origin, port=unused_tcp_port)
    await origin_server.start_server()

    # Base URL encoded; the SDK-appended /v1/messages suffix is rebased onto it,
    # so the origin must receive the request at /v1/messages.
    target = f"http://127.0.0.1:{origin_server.port}"
    fake = FakeRouter(["a debug screenshot"])
    proxy = _app(fake)
    client = TestClient(TestServer(proxy.build()))
    await client.start_server()
    try:
        body = json.dumps({"messages": [{
            "role": "user",
            "content": [{
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png",
                           "data": base64.b64encode(PNG).decode()},
            }],
        }]}).encode()
        # Transparent (no image) but the SDK appended /v1/messages after b64.
        noimg = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()
        resp = await client.post(f"/proxy/anthropic/{_b64(target)}/v1/messages", data=noimg)
        assert resp.status == 200
        await resp.read()
        assert json.loads(captured["body"]) == {"messages": [{"role": "user", "content": "hi"}]}
    finally:
        await client.close()
        await origin_server.close()


@pytest.mark.asyncio
async def test_classifier_response_is_normalized_end_to_end(unused_tcp_port):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    captured = {}

    async def origin_h(request):
        captured["body"] = await request.read()
        return web.json_response({
            "id": "msg_gateway",
            "type": "message",
            "role": "assistant",
            "model": "gateway-backend-model",
            "content": [
                {"type": "thinking", "thinking": "benign"},
                {"type": "text", "text": "<block>no</block>"},
            ],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })

    origin = web.Application()
    origin.router.add_post("/v1/messages", origin_h)
    origin_server = TestServer(origin, port=unused_tcp_port)
    await origin_server.start_server()

    target = f"http://127.0.0.1:{origin_server.port}"
    proxy = _app(FakeRouter([]))
    client = TestClient(TestServer(proxy.build()))
    await client.start_server()
    try:
        request_body = _classifier_body()
        resp = await client.post(
            f"/proxy/anthropic/{_b64(target)}/v1/messages",
            data=request_body,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 200
        result = await resp.json()
        upstream_request = json.loads(captured["body"])
        assert upstream_request["thinking"] == {"type": "disabled"}
        # All classifier semantics other than the configured thinking override
        # remain intact.
        original_request = json.loads(request_body)
        upstream_request.pop("thinking")
        assert upstream_request == original_request
        assert result["content"] == [{"type": "text", "text": "<block>no"}]
        assert result["stop_reason"] == "stop_sequence"
        assert result["stop_sequence"] == "</block>"
    finally:
        await client.close()
        await origin_server.close()


@pytest.mark.asyncio
async def test_classifier_thinking_rewrite_can_be_disabled_without_disabling_response_normalization(
    unused_tcp_port,
):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    captured = {}

    async def origin_h(request):
        captured["body"] = await request.read()
        return web.json_response({
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "gateway reasoning"},
                {"type": "text", "text": "<block>no</block>"},
            ],
            "stop_reason": "end_turn",
        })

    origin = web.Application()
    origin.router.add_post("/v1/messages", origin_h)
    origin_server = TestServer(origin, port=unused_tcp_port)
    await origin_server.start_server()

    cfg = AppConfig()
    cfg.proxy.classifier.disable_thinking = False
    target = f"http://127.0.0.1:{origin_server.port}"
    from lm_visual_mcp.proxy.server import VisionProxyApp

    proxy = VisionProxyApp(cfg, router=FakeRouter([]))
    client = TestClient(TestServer(proxy.build()))
    await client.start_server()
    try:
        request_body = _classifier_body()
        resp = await client.post(
            f"/proxy/anthropic/{_b64(target)}/v1/messages",
            data=request_body,
            headers={"Content-Type": "application/json"},
        )
        result = await resp.json()
        assert captured["body"] == request_body  # request rewrite is disabled
        assert result["content"] == [{"type": "text", "text": "<block>no"}]
        assert result["stop_reason"] == "stop_sequence"
        assert result["stop_sequence"] == "</block>"
    finally:
        await client.close()
        await origin_server.close()


# -- classifier response encoding branches (gzip/deflate transparency) -------
class _FakeStream:
    def __init__(self, body):
        self._body = body

    async def read(self):
        return self._body


class _FakeResp:
    """Minimal stand-in for aiohttp.ClientResponse in _read_decompressed."""

    def __init__(self, body, encoding=None):
        self.content = _FakeStream(body)
        self.headers = {} if encoding is None else {"Content-Encoding": encoding}


def _run(coro):
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)


def test_read_decompressed_passthrough_when_no_encoding():
    from lm_visual_mcp.proxy.server import _read_decompressed

    raw = b'{"plain": true}'
    assert _run(_read_decompressed(_FakeResp(raw))) == raw


def test_read_decompressed_unknown_encoding_returns_raw():
    from lm_visual_mcp.proxy.server import _read_decompressed

    raw = b"\x00\xff not gzip"
    assert _run(_read_decompressed(_FakeResp(raw, "zstd"))) == raw


def test_read_decompressed_gzip():
    import gzip

    from lm_visual_mcp.proxy.server import _read_decompressed

    plain = b'{"type":"message"}'
    assert _run(_read_decompressed(_FakeResp(gzip.compress(plain), "gzip"))) == plain


def test_read_decompressed_zlib_deflate():
    import zlib

    from lm_visual_mcp.proxy.server import _read_decompressed

    plain = b'{"type":"message"}'
    assert _run(_read_decompressed(_FakeResp(zlib.compress(plain), "deflate"))) == plain


def test_read_decompressed_raw_deflate():
    import zlib

    from lm_visual_mcp.proxy.server import _read_decompressed

    # Some servers emit raw deflate (no zlib header); fall back to -MAX_WBITS.
    plain = b'{"type":"message"}'
    raw = zlib.compress(plain, 6, -zlib.MAX_WBITS)
    assert _run(_read_decompressed(_FakeResp(raw, "deflate"))) == plain


def test_read_decompressed_brotli():
    try:
        import brotli
    except ImportError:
        pytest.skip("brotli not installed")
    from lm_visual_mcp.proxy.server import _read_decompressed

    plain = b'{"type":"message"}'
    assert _run(_read_decompressed(_FakeResp(brotli.compress(plain), "br"))) == plain


def test_read_decompressed_brotli_unavailable_returns_raw():
    try:
        import brotli  # noqa: F401
    except ImportError:
        pass
    else:
        pytest.skip("brotli installed")
    from lm_visual_mcp.proxy.server import _read_decompressed

    raw = b"\x21\x1b br bytes"
    assert _run(_read_decompressed(_FakeResp(raw, "br"))) == raw


def _classifier_origin(encoding, payload, raw_body=None):
    """Build an aiohttp handler returning a classifier response, optionally encoded."""
    from aiohttp import web

    async def origin_h(request):
        body = raw_body if raw_body is not None else json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if encoding == "gzip":
            import gzip

            body = gzip.compress(body)
            headers["Content-Encoding"] = "gzip"
        elif encoding == "deflate":
            import zlib

            body = zlib.compress(body)
            headers["Content-Encoding"] = "deflate"
        return web.Response(status=200, body=body, headers=headers)

    return origin_h


async def _classifier_client(origin_h, unused_tcp_port):
    """Start origin + proxy, return (TestClient, target_url, origin_server)."""
    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    origin = web.Application()
    origin.router.add_post("/v1/messages", origin_h)
    origin_server = TestServer(origin, port=unused_tcp_port)
    await origin_server.start_server()
    client = TestClient(TestServer(_app(FakeRouter([])).build()))
    await client.start_server()
    target = f"http://127.0.0.1:{origin_server.port}"
    return client, target, origin_server


async def _classifier_raw_response(client, target, request_body=_classifier_body()):
    """POST through the proxy with a non-decompressing client; return (resp, raw_bytes)."""
    import aiohttp

    url = f"http://127.0.0.1:{client.server.port}/proxy/anthropic/{_b64(target)}/v1/messages"
    async with aiohttp.ClientSession(auto_decompress=False) as s:
        async with s.post(
            url, data=request_body, headers={"Content-Type": "application/json"}
        ) as r:
            return r, await r.content.read()


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["gzip", "deflate", None])
async def test_classifier_encoded_upstream_is_decompressed_and_normalized(
    unused_tcp_port, encoding
):
    payload = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "<block>yes</block>"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
    }
    client, target, origin_server = await _classifier_client(
        _classifier_origin(encoding, payload), unused_tcp_port
    )
    try:
        resp, body_raw = await _classifier_raw_response(client, target)
        assert resp.status == 200
        assert resp.headers.get("Content-Encoding") is None
        assert body_raw[:2] != b"\x1f\x8b"  # plaintext, not compressed
        result = json.loads(body_raw)
        assert result["content"] == [{"type": "text", "text": "<block>yes"}]
        assert result["stop_reason"] == "stop_sequence"
        assert result["stop_sequence"] == "</block>"
    finally:
        await client.close()
        await origin_server.close()


@pytest.mark.asyncio
async def test_classifier_gzip_no_verdict_decompressed_but_not_normalized(unused_tcp_port):
    payload = {
        "type": "message",
        "content": [{"type": "text", "text": "I cannot decide"}],
        "stop_reason": "end_turn",
    }
    client, target, origin_server = await _classifier_client(
        _classifier_origin("gzip", payload), unused_tcp_port
    )
    try:
        resp, body_raw = await _classifier_raw_response(client, target)
        assert resp.headers.get("Content-Encoding") is None
        result = json.loads(body_raw)
        # No unambiguous verdict -> body passes through, but still decompressed
        # and the stale Content-Encoding header is gone.
        assert result["content"] == [{"type": "text", "text": "I cannot decide"}]
        assert result["stop_reason"] == "end_turn"
    finally:
        await client.close()
        await origin_server.close()


@pytest.mark.asyncio
async def test_classifier_gzip_conflicting_verdicts_not_rewritten(unused_tcp_port):
    payload = {
        "type": "message",
        "content": [
            {"type": "text", "text": "<block>yes</block> <block>no</block>"}
        ],
        "stop_reason": "end_turn",
    }
    client, target, origin_server = await _classifier_client(
        _classifier_origin("gzip", payload), unused_tcp_port
    )
    try:
        resp, body_raw = await _classifier_raw_response(client, target)
        assert resp.headers.get("Content-Encoding") is None
        result = json.loads(body_raw)
        assert result["content"][0]["text"] == "<block>yes</block> <block>no</block>"
        assert result["stop_reason"] == "end_turn"
    finally:
        await client.close()
        await origin_server.close()


@pytest.mark.asyncio
async def test_classifier_gzip_malformed_json_decompressed_not_rewritten(unused_tcp_port):
    # Non-JSON body: normalize_auto_classifier_response leaves it alone, but the
    # proxy must still decompress and drop the stale Content-Encoding header so
    # the client receives plaintext with matching headers.
    client, target, origin_server = await _classifier_client(
        _classifier_origin("gzip", None, raw_body=b"<html>not json</html>"),
        unused_tcp_port,
    )
    try:
        resp, body_raw = await _classifier_raw_response(client, target)
        assert resp.headers.get("Content-Encoding") is None
        assert body_raw == b"<html>not json</html>"
    finally:
        await client.close()
        await origin_server.close()


@pytest.mark.asyncio
async def test_classifier_non_stage1_is_transparent_passthrough(unused_tcp_port):
    # A classifier-family request with a non-binary stop sequence is NOT stage
    # one, so it takes the streaming path: raw bytes + Content-Encoding preserved.
    payload = {"type": "message", "content": [{"type": "text", "text": "anything"}]}
    client, target, origin_server = await _classifier_client(
        _classifier_origin("gzip", payload), unused_tcp_port
    )
    try:
        request_body = json.loads(_classifier_body())
        request_body["stop_sequences"] = ["</severity>"]
        resp, body_raw = await _classifier_raw_response(
            client, target, request_body=json.dumps(request_body).encode()
        )
        assert resp.headers.get("Content-Encoding") == "gzip"
        assert body_raw[:2] == b"\x1f\x8b"  # raw gzip forwarded untouched
    finally:
        await client.close()
        await origin_server.close()


@pytest.mark.asyncio
async def test_streaming_gzip_passthrough_preserves_encoding(unused_tcp_port):
    # Non-classifier request: byte-level transparency keeps gzip end to end.
    import gzip

    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    async def origin_h(request):
        import gzip

        return web.Response(
            body=gzip.compress(b"hello world"),
            headers={"Content-Encoding": "gzip", "Content-Type": "text/plain"},
        )

    origin = web.Application()
    origin.router.add_post("/v1/messages", origin_h)
    origin_server = TestServer(origin, port=unused_tcp_port)
    await origin_server.start_server()
    client = TestClient(TestServer(_app(FakeRouter([])).build()))
    await client.start_server()
    try:
        target = f"http://127.0.0.1:{origin_server.port}"
        url = f"http://127.0.0.1:{client.server.port}/proxy/anthropic/{_b64(target)}/v1/messages"
        import aiohttp

        async with aiohttp.ClientSession(auto_decompress=False) as s:
            async with s.post(url, data=b'{"messages":[]}') as r:
                assert r.headers.get("Content-Encoding") == "gzip"
                raw = await r.content.read()
                assert raw == gzip.compress(b"hello world")
    finally:
        await client.close()
        await origin_server.close()


# -- health probe / singleton -------------------------------------------------
@pytest.mark.asyncio
async def test_health_endpoint(unused_tcp_port):
    from aiohttp.test_utils import TestClient, TestServer

    proxy = _app(FakeRouter([]))
    client = TestClient(TestServer(proxy.build()))
    await client.start_server()
    try:
        resp = await client.get("/health")
        data = await resp.json()
        assert resp.status == 200
        assert data["ok"] is True
        assert data["version"]
    finally:
        await client.close()


def test_probe_proxy_negative():
    from lm_visual_mcp.services import probe_proxy

    # Nothing listens here -> not healthy.
    assert probe_proxy("127.0.0.1", 1, timeout=0.1) is False


def test_run_proxy_exits_quietly_when_port_taken(unused_tcp_port):
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", unused_tcp_port))
    s.listen(1)
    try:
        env = dict(
            os.environ,
            LM_VISUAL_MCP_PROXY_HOST="127.0.0.1",
            LM_VISUAL_MCP_PROXY_PORT=str(unused_tcp_port),
        )
        proc = subprocess.Popen(
            [sys.executable, "-m", "lm_visual_mcp", "proxy"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            rc = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail("proxy did not exit on port conflict")
        assert rc == 0  # singleton: loser exits quietly, winner keeps serving
    finally:
        s.close()


def test_start_proxy_spawns(monkeypatch, unused_tcp_port):
    from lm_visual_mcp import services
    from lm_visual_mcp.config import AppConfig

    called: dict = {}

    def fake_popen(cmd, **kwargs):
        called["cmd"] = cmd
        return object()

    monkeypatch.setattr(services.proxy.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(services.proxy, "probe_proxy", lambda *a, **k: True)

    cfg = AppConfig()
    cfg.proxy.port = unused_tcp_port
    assert services.start_proxy(cfg, None) is True
    assert sys.executable in called["cmd"]
    assert "-m" in called["cmd"]
    assert "proxy" in called["cmd"]


@pytest.mark.asyncio
async def test_probe_proxy_positive(unused_tcp_port):
    import asyncio

    from aiohttp.test_utils import TestClient, TestServer
    from lm_visual_mcp.services import probe_proxy

    proxy = _app(FakeRouter([]))
    client = TestClient(TestServer(proxy.build()))
    await client.start_server()
    try:
        # Run the blocking probe off-loop so the server socket keeps being served.
        ok = await asyncio.to_thread(probe_proxy, "127.0.0.1", client.server.port, 0.5)
        assert ok is True
    finally:
        await client.close()


# -- helpers ----------------------------------------------------------------
def _app(fake):
    from lm_visual_mcp.proxy.server import VisionProxyApp

    return VisionProxyApp(AppConfig(), router=fake)
