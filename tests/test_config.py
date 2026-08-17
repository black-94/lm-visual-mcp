"""Configuration loading tests."""

from __future__ import annotations

import pytest

from lm_visual_mcp.config import AppConfig, ConfigError, load_config


def test_defaults(tmp_path, monkeypatch):
    # No config file anywhere: isolate from the developer machine's own config.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("lm_visual_mcp.config.DEFAULT_CONFIG_CANDIDATES", ())
    cfg = load_config(env={})
    assert cfg.version == 2
    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.port == 8787
    assert cfg.server.image_hook.enabled is True
    assert cfg.server.classifier_hook.enabled is True
    assert [e.name for e in cfg.vision.providers] == ["agy", "codex", "gemini", "opencode"]
    assert cfg.vision.providers[0].type == "agy"
    assert cfg.vision.max_concurrency == 2


def test_file_overrides(tmp_path):
    f = tmp_path / "cfg.yaml"
    f.write_text(
        """
server:
  port: 9999
  image_hook:
    enabled: false
vision:
  providers:
    - name: g1
      type: gemini
      api_key_env: MY_KEY
      rate_limit:
        rpm: 5
        concurrency: 2
    - name: agy2
      type: agy
      command: /usr/local/bin/agy
""",
        encoding="utf-8",
    )
    cfg = load_config(config_path=str(f), env={})
    assert cfg.server.port == 9999
    assert cfg.server.image_hook.enabled is False
    assert [e.name for e in cfg.vision.providers] == ["g1", "agy2"]
    assert cfg.vision.providers[0].rate_limit.rpm == 5
    assert cfg.vision.providers[1].command == "/usr/local/bin/agy"


def test_env_overrides(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("lm_visual_mcp.config.DEFAULT_CONFIG_CANDIDATES", ())
    cfg = load_config(
        env={
            "LM_VISUAL_MCP_SERVER_PORT": "7777",
            "LM_VISUAL_MCP_IMAGE_HOOK": "false",
            "LM_VISUAL_MCP_TIMEOUT": "60",
        }
    )
    assert cfg.server.port == 7777
    assert cfg.server.image_hook.enabled is False
    assert cfg.vision.timeout == 60.0


def test_unknown_provider_type_rejected(tmp_path):
    f = tmp_path / "cfg.yaml"
    f.write_text(
        "vision:\n  providers:\n    - name: x\n      type: nope\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(config_path=str(f), env={})


def test_duplicate_names_rejected(tmp_path):
    f = tmp_path / "cfg.yaml"
    f.write_text(
        "vision:\n  providers:\n    - name: x\n      type: agy\n    - name: x\n      type: codex\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(config_path=str(f), env={})


def test_no_mcp_section_in_schema():
    """The MCP start decision lives in CLI args / env, never in the file."""
    cfg = AppConfig()
    assert not any(
        field.startswith("mcp") for field in type(cfg).model_fields
    )
