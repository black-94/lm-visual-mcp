"""Config system tests: defaults, YAML, env, CLI, validation, secrets."""

from __future__ import annotations

import pytest

from lm_visual_mcp.config import load_config
from lm_visual_mcp.errors import ConfigError


def test_defaults() -> None:
    cfg = load_config(env={}, config_path=None)
    assert cfg.providers.order == ["agy", "codex", "gemini", "opencode"]
    assert cfg.runtime.timeout == 120.0
    assert cfg.runtime.workdir is None
    assert cfg.fallback.enabled is True
    assert cfg.media.max_image_mb == 20.0
    assert cfg.media.max_video_mb == 8.0
    # Plan §5: all providers enabled by default (probe+fallback makes this safe).
    assert cfg.providers.agy.enabled is True


def test_yaml_loading(tmp_path, monkeypatch) -> None:
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text(
        """
version: 1
providers:
  order: [codex, gemini]
  codex:
    enabled: true
    command: /usr/bin/codex
    model: gpt-x
  gemini:
    enabled: true
    model: gemini-y
    api_key_env: FOO_KEY
runtime:
  timeout: 45
media:
  max_image_mb: 5
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("FOO_KEY", "secret-123")
    cfg = load_config(config_path=str(cfg_file), env=None)
    assert cfg.providers.order == ["codex", "gemini"]
    assert cfg.providers.codex.model == "gpt-x"
    assert cfg.runtime.timeout == 45.0
    assert cfg.media.max_image_mb == 5.0
    # api_key_env resolved at use time.
    assert cfg.providers.gemini.effective_api_key() == "secret-123"


def test_environment_override(monkeypatch, tmp_path) -> None:
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text(
        "runtime:\n  timeout: 10\nproviders:\n  codex:\n    enabled: true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LM_VISUAL_MCP_TIMEOUT", "99")
    monkeypatch.setenv("LM_VISUAL_MCP_CODEX_MODEL", "env-model")
    monkeypatch.setenv("LM_VISUAL_MCP_WORKDIR", "/tmp/wd")
    cfg = load_config(config_path=str(cfg_file), env=None)
    assert cfg.runtime.timeout == 99.0
    assert cfg.providers.codex.model == "env-model"
    assert cfg.runtime.workdir == "/tmp/wd"


def test_cli_override_log_level(tmp_path) -> None:
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("logging:\n  level: INFO\n", encoding="utf-8")
    cfg = load_config(config_path=str(cfg_file), env={}, log_level="DEBUG")
    assert cfg.logging.level == "DEBUG"


def test_lm_visual_mcp_config_env_path(monkeypatch, tmp_path) -> None:
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("version: 1\n", encoding="utf-8")
    monkeypatch.setenv("LM_VISUAL_MCP_CONFIG", str(cfg_file))
    cfg = load_config(env=None)
    assert cfg.version == 1


def test_invalid_provider_in_order(tmp_path) -> None:
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("providers:\n  order: [nope, codex]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown provider"):
        load_config(config_path=str(cfg_file), env={})


def test_duplicate_provider_in_order(tmp_path) -> None:
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("providers:\n  order: [codex, codex]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="duplicates"):
        load_config(config_path=str(cfg_file), env={})


def test_disabled_provider_ignored(tmp_path) -> None:
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text(
        "providers:\n  order: [gemini, codex]\n  gemini:\n    enabled: false\n  codex:\n    enabled: true\n",
        encoding="utf-8",
    )
    cfg = load_config(config_path=str(cfg_file), env={})
    assert cfg.providers.gemini.enabled is False
    assert cfg.providers.codex.enabled is True


def test_api_secret_redacted(tmp_path) -> None:
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text(
        "providers:\n  gemini:\n    enabled: true\n    api_key: super-secret-key-value\n",
        encoding="utf-8",
    )
    cfg = load_config(config_path=str(cfg_file), env={})
    # SecretStr hides the value.
    assert "super-secret-key-value" not in str(cfg.providers.gemini.api_key)
    assert cfg.providers.gemini.effective_api_key() == "super-secret-key-value"


def test_gemini_api_key_three_level_resolution(tmp_path, monkeypatch) -> None:
    """Plan §8: LM_VISUAL_MCP_GEMINI_API_KEY > api_key_env > GEMINI_API_KEY."""
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text(
        "providers:\n  gemini:\n    enabled: true\n    api_key_env: CUSTOM_KEY\n",
        encoding="utf-8",
    )

    # Level 3: bare GEMINI_API_KEY fallback when nothing else is set.
    monkeypatch.setenv("GEMINI_API_KEY", "lvl3")
    monkeypatch.delenv("CUSTOM_KEY", raising=False)
    monkeypatch.delenv("LM_VISUAL_MCP_GEMINI_API_KEY", raising=False)
    assert load_config(config_path=str(cfg_file), env=None).providers.gemini.effective_api_key() == "lvl3"

    # Level 2: api_key_env wins over GEMINI_API_KEY.
    monkeypatch.setenv("CUSTOM_KEY", "lvl2")
    assert load_config(config_path=str(cfg_file), env=None).providers.gemini.effective_api_key() == "lvl2"

    # Level 1: the prefixed env override wins over everything.
    monkeypatch.setenv("LM_VISUAL_MCP_GEMINI_API_KEY", "lvl1")
    assert load_config(config_path=str(cfg_file), env=None).providers.gemini.effective_api_key() == "lvl1"


def test_effort_env_override(monkeypatch, tmp_path) -> None:
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("providers:\n  codex:\n    enabled: true\n", encoding="utf-8")
    monkeypatch.setenv("LM_VISUAL_MCP_CODEX_EFFORT", "high")
    cfg = load_config(config_path=str(cfg_file), env=None)
    assert cfg.providers.codex.effort == "high"


def test_missing_config_file_raises(tmp_path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(config_path=str(tmp_path / "none.yaml"), env={})