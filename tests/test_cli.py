"""CLI argument parsing tests (config path accepted before/after subcommand)."""

from __future__ import annotations

from lm_visual_mcp.cli import build_parser


def test_config_before_subcommand() -> None:
    args = build_parser().parse_args(["--config", "a.yaml", "doctor"])
    assert args.command == "doctor"
    assert args.config == "a.yaml"


def test_config_after_subcommand() -> None:
    args = build_parser().parse_args(["doctor", "--config", "a.yaml"])
    assert args.command == "doctor"
    assert args.config == "a.yaml"


def test_version_flag() -> None:
    args = build_parser().parse_args(["--version"])
    assert args.version is True
    assert args.command is None


def test_default_config_is_none() -> None:
    args = build_parser().parse_args(["doctor"])
    assert args.config is None
    assert args.log_level is None


def test_daemon_subcommand() -> None:
    args = build_parser().parse_args(["--config", "a.yaml", "daemon"])
    assert args.command == "daemon"
    assert args.config == "a.yaml"


def test_daemon_config_after_subcommand() -> None:
    args = build_parser().parse_args(["daemon", "--config", "a.yaml"])
    assert args.command == "daemon"
    assert args.config == "a.yaml"