"""CLI argument tests (no servers spawned)."""

from __future__ import annotations

from lm_visual_mcp.cli import build_parser, main


def test_version(capsys):
    assert main(["--version"]) == 0
    assert "lm-visual-mcp" in capsys.readouterr().out


def test_start_server_flags():
    parser = build_parser()
    assert parser.parse_args([]).start_server is None  # default: env decides (true)
    assert parser.parse_args(["--start-server"]).start_server is True
    assert parser.parse_args(["--no-start-server"]).start_server is False
    args = parser.parse_args(["--no-start-server", "doctor"])
    assert args.command == "doctor" and args.start_server is False


def test_start_server_env_resolution(monkeypatch):
    monkeypatch.setenv("LM_VISUAL_MCP_START_SERVER", "0")
    parser = build_parser()
    assert parser.parse_args([]).start_server is None  # resolution happens in main()


def test_doctor_runs_without_server(capsys):
    assert main(["--no-start-server", "doctor"]) == 0
    out = capsys.readouterr().out
    assert "Provider chain" in out
    assert "rate_limit" in out or "model" in out
