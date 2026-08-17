"""Image hook + protocol adapters: rewrite carries the absolute image path."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from lm_visual_mcp.server.cache import VisionCache
from lm_visual_mcp.server.hooks import HookContext
from lm_visual_mcp.server.image_hook import ImageHook
from lm_visual_mcp.server.protocols import build_registry

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class FakeVision:
    def __init__(self) -> None:
        self.describe_calls: list = []
        self.router = type("R", (), {"providers": []})()

    async def describe(self, images, timeout=None):
        self.describe_calls.append(len(images))
        return [f"description-of-{i}" for i in range(len(images))], "fake"


def make_hook(tmp_path: Path) -> tuple[ImageHook, FakeVision]:
    from lm_visual_mcp.media import MediaService

    vision = FakeVision()
    media = MediaService(workdir=tmp_path / "media")
    hook = ImageHook(
        media=media,
        vision=vision,
        cache=VisionCache(directory=tmp_path / "desc"),  # isolated, not the real runtime dir
        adapters=build_registry(),
    )
    return hook, vision


def anthropic_body(n: int = 1, datas: list[bytes] | None = None) -> bytes:
    blobs = datas if datas is not None else [_PNG] * n
    image_blocks = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(blobs[i]).decode(),
            },
        }
        for i in range(n)
    ]
    return json.dumps(
        {
            "model": "claude-x",
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "look"}, *image_blocks],
                }
            ],
        }
    ).encode()


async def test_anthropic_image_rewritten_with_absolute_path(tmp_path):
    hook, vision = make_hook(tmp_path)
    c = HookContext(
        method="POST", url="http://up", headers={}, body=anthropic_body(),
        state={"protocol": "anthropic"},
    )
    result = await hook.process(c)
    assert result.action == "continue" and result.body is not None
    doc = json.loads(result.body)
    blocks = doc["messages"][0]["content"]
    assert blocks[1]["type"] == "text"
    text = blocks[1]["text"]
    # The absolute staged path must be recorded, and the file must exist there.
    assert text.startswith("[Image 1: ")
    path = text.splitlines()[0][len("[Image 1: "):-1]
    assert Path(path).is_absolute()
    assert Path(path).exists()
    assert "description-of-0" in text
    assert vision.describe_calls  # one batched describe call


async def test_multi_image_batched_single_describe(tmp_path):
    """A request with several images costs one batched describe call."""
    hook, vision = make_hook(tmp_path)
    body = anthropic_body(n=2)
    c = HookContext(method="POST", url="http://up", headers={}, body=body,
                    state={"protocol": "anthropic"})
    result = await hook.process(c)
    assert result.action == "continue" and result.body is not None
    # exactly one describe call, with *both* images in it (never one-per-image).
    assert vision.describe_calls == [2]
    doc = json.loads(result.body)
    blocks = doc["messages"][0]["content"]
    assert sum(1 for b in blocks if b["type"] == "text" and "[Image" in b["text"]) == 2


async def test_multi_image_cache_misses_only_described_once(tmp_path):
    """With one of two images already cached, only the missing one is described."""
    hook, vision = make_hook(tmp_path)
    # Image 0 = the cached PNG; image 1 differs by one byte (distinct key).
    other = _PNG[:-4] + b"\x00\x00\x00\x00"
    # Warm the cache with image 0 alone.
    first = await hook.process(HookContext(
        method="POST", url="http://up", headers={}, body=anthropic_body(n=1, datas=[_PNG]),
        state={"protocol": "anthropic"},
    ))
    assert first.action == "continue"
    before = len(vision.describe_calls)

    second = await hook.process(HookContext(
        method="POST", url="http://up", headers={}, body=anthropic_body(n=2, datas=[_PNG, other]),
        state={"protocol": "anthropic"},
    ))
    assert second.action == "continue"
    # Image 0 is served from cache; only image 1 is a miss, batched into one call.
    assert len(vision.describe_calls) == before + 1
    assert vision.describe_calls[-1] == 1  # a single image in that batched call


async def test_second_request_hits_cache(tmp_path):
    hook, vision = make_hook(tmp_path)
    for _ in range(2):
        c = HookContext(
            method="POST", url="http://up", headers={}, body=anthropic_body(),
            state={"protocol": "anthropic"},
        )
        result = await hook.process(c)
        assert result.action == "continue"
    assert len(vision.describe_calls) == 1  # second pass served from cache


async def test_no_image_passthrough(tmp_path):
    hook, vision = make_hook(tmp_path)
    body = json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}).encode()
    c = HookContext(method="POST", url="http://up", headers={}, body=body,
                    state={"protocol": "anthropic"})
    result = await hook.process(c)
    assert result.action == "continue" and result.body is None
    assert not vision.describe_calls


async def test_unknown_protocol_passthrough(tmp_path):
    hook, vision = make_hook(tmp_path)
    c = HookContext(method="POST", url="http://up", headers={}, body=anthropic_body(),
                    state={"protocol": "other"})
    result = await hook.process(c)
    assert result.action == "continue" and result.body is None


async def test_openai_chat_rewritten(tmp_path):
    hook, vision = make_hook(tmp_path)
    body = json.dumps(
        {
            "model": "gpt-x",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{base64.b64encode(_PNG).decode()}"
                            },
                        },
                    ],
                }
            ],
        }
    ).encode()
    c = HookContext(method="POST", url="http://up", headers={}, body=body,
                    state={"protocol": "openai/chat"})
    result = await hook.process(c)
    assert result.action == "continue" and result.body is not None
    doc = json.loads(result.body)
    part = doc["messages"][0]["content"][1]
    assert part["type"] == "text"
    assert "[Image 1: " in part["text"]
    assert Path(part["text"].splitlines()[0].split(": ", 1)[1].rstrip("]")).exists()


async def test_nested_tool_result_image_found(tmp_path):
    """Images nested in tool_result.content are extracted too."""
    hook, vision = make_hook(tmp_path)
    body = json.dumps(
        {
            "model": "claude-x",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": base64.b64encode(_PNG).decode(),
                                    },
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    ).encode()
    c = HookContext(method="POST", url="http://up", headers={}, body=body,
                    state={"protocol": "anthropic"})
    result = await hook.process(c)
    assert result.action == "continue" and result.body is not None
    doc = json.loads(result.body)
    block = doc["messages"][0]["content"][0]["content"][0]
    assert block["type"] == "text"
    assert "[Image 1: " in block["text"]
