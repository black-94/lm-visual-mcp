"""Configuration loading tests (top-level server / hooks / providers / vision)."""

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
    # top-level hooks, no server.image_hook/classifier_hook
    assert cfg.hooks.image.enabled is True
    assert cfg.hooks.classifier.enabled is True
    assert not hasattr(cfg.server, "image_hook")
    assert not hasattr(cfg.server, "classifier_hook")
    # built-in default chain: only gemini
    assert [e.name for e in cfg.providers] == ["gemini"]
    assert cfg.vision.image_chain == ["gemini"]
    assert cfg.vision.classifier_chain == ["gemini"]
    assert cfg.vision.max_concurrency == 2


def test_default_provider_is_gemini():
    cfg = AppConfig()
    assert [e.name for e in cfg.providers] == ["gemini"]
    assert cfg.providers[0].type == "gemini"
    assert cfg.providers[0].api_key_env == "GEMINI_API_KEY"


def test_file_overrides_new_shape(tmp_path):
    f = tmp_path / "cfg.yaml"
    f.write_text(
        """
server:
  port: 9999
hooks:
  image:
    enabled: false
  classifier:
    models: ["glm-5"]
providers:
  - name: g1
    type: gemini
    api_key_env: MY_KEY
    disable_thinking: true
    rate_limit:
      rpm: 5
      concurrency: 2
  - name: agy2
    type: agy
    command: /usr/local/bin/agy
vision:
  image_chain: [agy2, g1]
  classifier_chain: [g1]
""",
        encoding="utf-8",
    )
    cfg = load_config(config_path=str(f), env={})
    assert cfg.server.port == 9999
    assert cfg.hooks.image.enabled is False
    assert cfg.hooks.classifier.models == ["glm-5"]
    assert [e.name for e in cfg.providers] == ["g1", "agy2"]
    assert cfg.providers[0].rate_limit.rpm == 5
    assert cfg.providers[0].disable_thinking is True
    assert cfg.providers[1].command == "/usr/local/bin/agy"
    assert cfg.vision.image_chain == ["agy2", "g1"]
    assert cfg.vision.classifier_chain == ["g1"]


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
    assert cfg.hooks.image.enabled is False
    assert cfg.vision.timeout == 60.0


def test_unknown_provider_type_rejected(tmp_path):
    f = tmp_path / "cfg.yaml"
    f.write_text("providers:\n  - name: x\n    type: nope\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(config_path=str(f), env={})


def test_duplicate_names_rejected(tmp_path):
    f = tmp_path / "cfg.yaml"
    f.write_text(
        "providers:\n  - name: x\n    type: agy\n  - name: x\n    type: codex\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(config_path=str(f), env={})


def test_chain_undefined_provider_rejected(tmp_path):
    f = tmp_path / "cfg.yaml"
    f.write_text(
        "vision:\n  image_chain: [missing]\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        load_config(config_path=str(f), env={})


def test_chain_disabled_provider_rejected(tmp_path):
    f = tmp_path / "cfg.yaml"
    f.write_text(
        "providers:\n  - name: g\n    type: gemini\n    enabled: false\n"
        "vision:\n  classifier_chain: [g]\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(config_path=str(f), env={})


def test_no_mcp_section_in_schema():
    """The MCP start decision lives in CLI args / env, never in the file."""
    cfg = AppConfig()
    assert not any(field.startswith("mcp") for field in type(cfg).model_fields)