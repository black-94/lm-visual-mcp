"""Unified configuration (schema version 2).

Priority (highest first):

    CLI argument  >  Environment variable  >  Config file  >  Built-in default

The configuration has no ``mcp:`` section by design: the only MCP-level
decision - whether the MCP process should start the shared server - is passed
by the agent's MCP config as a CLI argument (``--start-server`` /
``--no-start-server``) or env var, not by this file. All listen addresses live
under ``server:``.

API keys are referenced by environment-variable *name* (``api_key_env``),
never embedded as plain text - though plain ``SecretStr`` keys are accepted
for compatibility and are strictly redacted everywhere.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import pydantic as pd
import yaml

from .errors import ConfigError
from .vision.types import ProviderFailureReason

CONFIG_VERSION = 2

# Default config file search paths (first existing wins): an in-cwd config
# outranks the user-level one. The legacy `config.yaml` name is no longer read.
DEFAULT_CONFIG_CANDIDATES = (
    Path("lm-visual-mcp.yaml"),
    Path("~/.config/lm-visual-mcp/lm-visual-mcp.yaml").expanduser(),
)

# Env var prefix for all overrides.
PREFIX = "LM_VISUAL_MCP_"


class RateLimitConfig(pd.BaseModel):
    """Per-provider rate limit. Either or both may be set; None disables."""

    rpm: Optional[int] = None
    concurrency: Optional[int] = None


class ProviderEntryConfig(pd.BaseModel):
    """One entry in the provider chain (list order = fallback order)."""

    name: str
    # Registry key into the provider registry - decouples config from classes.
    type: str
    enabled: bool = True
    command: Optional[str] = None  # CLI-based providers
    model: Optional[str] = None
    effort: Optional[str] = None
    # API-based providers.
    api_key_env: Optional[str] = None
    api_key: Optional[pd.SecretStr] = None
    base_url: Optional[str] = None
    timeout: Optional[float] = None
    # Seconds to keep an "unsupported" verdict cached (agy only).
    vision_cache_ttl: float = 300.0
    sandbox: str = "read-only"  # codex only
    rate_limit: RateLimitConfig = pd.Field(default_factory=RateLimitConfig)

    def effective_api_key(self, default_env: Optional[str] = None) -> Optional[str]:
        """Resolve the API key, never leaking it.

        Order: explicit config key > api_key_env env-var > type default env-var.
        """
        if self.api_key is not None:
            return self.api_key.get_secret_value()
        env_name = self.api_key_env or default_env
        if env_name:
            val = os.environ.get(env_name)
            if val:
                return val
        return None


class FallbackConfig(pd.BaseModel):
    enabled: bool = True
    on: list[ProviderFailureReason] = pd.Field(
        default_factory=lambda: sorted(ProviderFailureReason.fallback_eligible_defaults(), key=lambda r: str(r))
    )

    def reasons(self) -> set[ProviderFailureReason]:
        return set(self.on)


class VisionConfig(pd.BaseModel):
    timeout: float = 120.0
    max_concurrency: int = 2
    fallback: FallbackConfig = pd.Field(default_factory=FallbackConfig)
    providers: list[ProviderEntryConfig] = pd.Field(
        default_factory=lambda: [
            ProviderEntryConfig(name="agy", type="agy", command="agy"),
            ProviderEntryConfig(name="codex", type="codex", command="codex"),
            ProviderEntryConfig(
                name="gemini", type="gemini", api_key_env="GEMINI_API_KEY"
            ),
            ProviderEntryConfig(
                name="opencode", type="opencode", api_key_env="OPENCODE_API_KEY"
            ),
        ]
    )


class ImageHookConfig(pd.BaseModel):
    """Image request hook: rewrite image parts into text descriptions."""

    enabled: bool = True


class ClassifierHookConfig(pd.BaseModel):
    """Claude Code Auto-mode classifier compatibility hook."""

    enabled: bool = True
    # Classifier output is a tiny XML verdict. Thinking blocks waste tokens and
    # break some Claude Code parsers when returned before the text block.
    disable_thinking: bool = True


class ServerConfig(pd.BaseModel):
    """The shared singleton server (vision endpoint + request hooks)."""

    host: str = "127.0.0.1"
    port: int = 8787
    image_hook: ImageHookConfig = pd.Field(default_factory=ImageHookConfig)
    classifier_hook: ClassifierHookConfig = pd.Field(default_factory=ClassifierHookConfig)


class MediaConfig(pd.BaseModel):
    max_image_mb: float = 20.0
    download_timeout: float = 30.0
    max_download_mb: float = 32.0


class LoggingConfig(pd.BaseModel):
    level: str = "INFO"


class AppConfig(pd.BaseModel):
    version: int = CONFIG_VERSION
    server: ServerConfig = pd.Field(default_factory=ServerConfig)
    vision: VisionConfig = pd.Field(default_factory=VisionConfig)
    media: MediaConfig = pd.Field(default_factory=MediaConfig)
    logging: LoggingConfig = pd.Field(default_factory=LoggingConfig)

    def validate_all(self) -> None:
        from .vision.providers import PROVIDER_TYPES

        names = [e.name for e in self.vision.providers]
        if len(set(names)) != len(names):
            raise ConfigError("vision.providers contains duplicate names")
        for entry in self.vision.providers:
            if entry.type not in PROVIDER_TYPES:
                raise ConfigError(
                    f"unknown provider type {entry.type!r} (provider {entry.name!r})"
                )


class ConfigLoader:
    """Builds an :class:`AppConfig` merging CLI, env, file and defaults."""

    def __init__(
        self,
        *,
        config_path: Optional[str] = None,
        env: Optional[dict] = None,
        log_level: Optional[str] = None,
    ) -> None:
        self.config_path = config_path
        self.env = env if env is not None else os.environ
        self.log_level = log_level

    def _read_file(self, path: Path) -> dict:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read config file {path}: {exc}") from exc
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ConfigError(f"config file {path} must contain a mapping at top level")
        return data

    def resolve_config_path(self) -> Optional[Path]:
        if self.config_path:
            return Path(self.config_path).expanduser()
        env_path = self.env.get(f"{PREFIX}CONFIG")
        if env_path:
            return Path(env_path).expanduser()
        for cand in DEFAULT_CONFIG_CANDIDATES:
            if cand.exists():
                return cand
        return None

    def load(self) -> AppConfig:
        """Load, validate and merge configuration."""

        # 1. Built-in defaults.
        data: dict = {}

        # 2. Config file.
        path = self.resolve_config_path()
        if path is not None:
            if not path.exists():
                raise ConfigError(f"config file not found: {path}")
            data = dict(self._read_file(path))

        # 3. Environment overrides.
        self._apply_env(data)

        # 4. CLI overrides.
        if self.log_level is not None:
            data.setdefault("logging", {})["level"] = self.log_level

        try:
            cfg = AppConfig.model_validate(data)
        except pd.ValidationError as exc:
            raise ConfigError(f"invalid configuration: {exc}") from exc
        cfg.validate_all()
        return cfg

    # -- env overrides -------------------------------------------------------
    def _apply_env(self, data: dict) -> None:
        env = self.env
        e = lambda key: env.get(f"{PREFIX}{key}")  # noqa: E731

        server = data.setdefault("server", {})
        if e("SERVER_HOST"):
            server["host"] = e("SERVER_HOST")
        if e("SERVER_PORT"):
            server["port"] = int(e("SERVER_PORT"))
        if e("IMAGE_HOOK"):
            server.setdefault("image_hook", {})["enabled"] = _parse_bool(e("IMAGE_HOOK"))
        if e("CLASSIFIER_HOOK"):
            server.setdefault("classifier_hook", {})["enabled"] = _parse_bool(e("CLASSIFIER_HOOK"))

        vision = data.setdefault("vision", {})
        if e("TIMEOUT"):
            vision["timeout"] = float(e("TIMEOUT"))
        if e("MAX_CONCURRENCY"):
            vision["max_concurrency"] = int(e("MAX_CONCURRENCY"))

        if e("LOG_LEVEL"):
            data.setdefault("logging", {})["level"] = e("LOG_LEVEL")

        media = data.setdefault("media", {})
        if e("MAX_IMAGE_MB"):
            media["max_image_mb"] = float(e("MAX_IMAGE_MB"))


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"invalid boolean environment value: {value!r}")


def load_config(
    *,
    config_path: Optional[str] = None,
    env: Optional[dict] = None,
    log_level: Optional[str] = None,
) -> AppConfig:
    return ConfigLoader(config_path=config_path, env=env, log_level=log_level).load()
