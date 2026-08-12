"""Workspace management tests."""

from __future__ import annotations

from pathlib import Path

from lm_visual_mcp.services import WorkspaceManager


def test_temporary_workspace_created_and_cleaned() -> None:
    mgr = WorkspaceManager(base=None)
    ws = mgr.create()
    assert ws.root.name.startswith("lm-visual-mcp-")
    assert ws.input_dir.is_dir()
    assert ws.output_dir.is_dir()
    ws.cleanup()
    assert not ws.root.exists()


def test_base_workdir_uses_subdir(tmp_path) -> None:
    mgr = WorkspaceManager(base=tmp_path)
    ws = mgr.create()
    assert ws.root.parent.name == ".lm-visual-mcp"
    assert ws.root.is_relative_to(tmp_path)
    ws.cleanup()
    assert not ws.root.exists()


def test_stage_media_copies_user_file(tmp_path) -> None:
    src = tmp_path / "user.png"
    src.write_bytes(b"\x89PNGfake")
    mgr = WorkspaceManager(base=None)
    ws = mgr.create()
    staged = ws.stage_media(src)
    assert staged.exists()
    assert staged.read_bytes() == b"\x89PNGfake"
    # Original untouched.
    assert src.exists()
    ws.cleanup()


def test_cleanup_never_deletes_user_files(tmp_path) -> None:
    user_file = tmp_path / "keep.txt"
    user_file.write_text("keep me", encoding="utf-8")
    mgr = WorkspaceManager(base=tmp_path)
    ws = mgr.create()
    ws.cleanup()
    assert (tmp_path / "keep.txt").read_text() == "keep me"