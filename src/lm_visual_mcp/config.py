"""Server configuration.

Priority (highest first):

    CLI argument  >  Environment variable  >  Config file  >  Built-in default

API keys are referenced by environment-variable *name* (``api_key_env``), never
embedded as plain text — though plain ``SecretStr`` keys are accepted for
compatibility and are strictly redacted everywhere.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import pydantic as pd
import yaml

from .errors import ConfigError
from .models import DEFAULT_PROVIDER_ORDER, ProviderFailureReason

CONFIG_VERSION = 1

# Default config file search paths (first existing wins).
DEFAULT_CONFIG_CANDIDATES = (
    Path("lm-visual-mcp.yaml"),
    Path("~/.config/lm-visual-mcp/config.yaml").expanduser(),
    Path("~/.config/lm-visual-mcp/lm-visual-mcp.yaml").expanduser(),
)

# Env var prefix for all overrides.
PREFIX = "LM_VISUAL_MCP_"


class ProviderConfig(pd.BaseModel):
    enabled: bool = True
    command: Optional[str] = None
    model: Optional[str] = None
    effort: Optional[str] = None  # Reasoning effort: low | medium | high | xhigh (provider-dependent)
    # Only meaningful for API-based providers (gemini).
    api_key_env: Optional[str] = None
    # Plain-text compatibility key. Redacted. Prefer api_key_env.
    api_key: pd.SecretStr | None = pd.Field(default=None)
    # Seconds to keep a "vision unsupported" verdict cached before AGY is retried
    # with a real call. AGY-only; ignored by the other providers.
    vision_cache_ttl: float = 300.0

    def effective_api_key(self) -> Optional[str]:
        """Resolve the API key, never leaking it.

        Order: explicit config key > api_key_env env-var.
        """
        if self.api_key is not None:
            return self.api_key.get_secret_value()
        if self.api_key_env:
            val = os.environ.get(self.api_key_env)
            if val:
                return val
        return None


class ProvidersConfig(pd.BaseModel):
    order: list[str] = pd.Field(default_factory=lambda: list(DEFAULT_PROVIDER_ORDER))
    agy: ProviderConfig = pd.Field(default_factory=lambda: ProviderConfig(command="agy"))
    codex: ProviderConfig = pd.Field(default_factory=lambda: ProviderConfig(command="codex"))
    gemini: ProviderConfig = pd.Field(default_factory=lambda: ProviderConfig(api_key_env="GEMINI_API_KEY"))
    opencode: ProviderConfig = pd.Field(default_factory=lambda: ProviderConfig(command="opencode"))

    def get(self, name: str) -> Optional[ProviderConfig]:
        return getattr(self, name, None)

    def names(self) -> list[str]:
        return [k for k in self.model_dump().keys() if k != "order"]


class RuntimeConfig(pd.BaseModel):
    workdir: Optional[str] = None
    timeout: float = 120.0
    max_concurrency: int = 2
    # Singleton (global single-instance) transport settings. The shared
    # lm-vision-server binds this host/port; every Claude Code session proxies
    # to it. Requests beyond ``max_concurrency`` queue inside the server.
    host: str = "127.0.0.1"
    port: int = 6506


class FallbackConfig(pd.BaseModel):
    enabled: bool = True
    on: list[ProviderFailureReason] = pd.Field(
        default_factory=lambda: sorted(ProviderFailureReason.fallback_eligible_defaults(), key=lambda r: str(r))
    )

    def reasons(self) -> set[ProviderFailureReason]:
        return set(self.on)


class MediaConfig(pd.BaseModel):
    max_image_mb: float = 20.0
    max_video_mb: float = 8.0
    download_timeout: float = 30.0
    max_download_mb: float = 32.0


class LoggingConfig(pd.BaseModel):
    level: str = "INFO"


class ClassifierProxyConfig(pd.BaseModel):
    """Claude Code Auto-mode classifier compatibility settings."""

    # Classifier output is a tiny XML verdict. Thinking blocks waste tokens and
    # break some Claude Code parsers when returned before the text block.
    disable_thinking: bool = True


class ProxyConfig(pd.BaseModel):
    """Transparent vision-proxy HTTP listener configuration."""

    host: str = "127.0.0.1"
    port: int = 8787
    classifier: ClassifierProxyConfig = pd.Field(default_factory=ClassifierProxyConfig)


class AppConfig(pd.BaseModel):
    version: int = CONFIG_VERSION
    providers: ProvidersConfig = pd.Field(default_factory=ProvidersConfig)
    runtime: RuntimeConfig = pd.Field(default_factory=RuntimeConfig)
    fallback: FallbackConfig = pd.Field(default_factory=FallbackConfig)
    media: MediaConfig = pd.Field(default_factory=MediaConfig)
    logging: LoggingConfig = pd.Field(default_factory=LoggingConfig)
    proxy: ProxyConfig = pd.Field(default_factory=ProxyConfig)

    def validate_all(self) -> None:
        known = set(self.providers.names())
        for name in self.providers.order:
            if name not in known:
                raise ConfigError(f"unknown provider in order: {name!r}")
        if len(set(self.providers.order)) != len(self.providers.order):
            raise ConfigError("providers.order contains duplicates")


def _coerce_reason(value) -> ProviderFailureReason:
    if isinstance(value, ProviderFailureReason):
        return value
    return ProviderFailureReason(str(value))


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

    # -- env overrides -----------------------------------------------------
    def _apply_env(self, data: dict) -> None:
        env = self.env
        e = lambda key: env.get(f"{PREFIX}{key}")  # noqa: E731

        if e("WORKDIR"):
            data.setdefault("runtime", {})["workdir"] = e("WORKDIR")
        if e("TIMEOUT"):
            data.setdefault("runtime", {})["timeout"] = float(e("TIMEOUT"))
        if e("MAX_CONCURRENCY"):
            data.setdefault("runtime", {})["max_concurrency"] = int(e("MAX_CONCURRENCY"))
        if e("HOST"):
            data.setdefault("runtime", {})["host"] = e("HOST")
        if e("PORT"):
            data.setdefault("runtime", {})["port"] = int(e("PORT"))

        for name in ("agy", "codex", "opencode"):
            cmd = e(f"{name.upper()}_COMMAND")
            model = e(f"{name.upper()}_MODEL")
            effort = e(f"{name.upper()}_EFFORT")
            if not (cmd or model or effort):
                continue
            provider = data.setdefault("providers", {}).setdefault(name, {})
            if cmd:
                provider["command"] = cmd
            if model:
                provider["model"] = model
            if effort:
                provider["effort"] = effort

        # Only materialize the gemini section when an actual override exists;
        # otherwise pydantic's default_factory (api_key_env="GEMINI_API_KEY")
        # would be skipped, dropping the default key resolution.
        gem = {
            k: v
            for k, v in (
                ("model", e("GEMINI_MODEL")),
                ("api_key", e("GEMINI_API_KEY")),
                ("effort", e("GEMINI_EFFORT")),
            )
            if v
        }
        if gem:
            data.setdefault("providers", {}).setdefault("gemini", {}).update(gem)

        if e("LOG_LEVEL"):
            data.setdefault("logging", {})["level"] = e("LOG_LEVEL")

        proxy = data.setdefault("proxy", {})
        if e("PROXY_HOST"):
            proxy["host"] = e("PROXY_HOST")
        if e("PROXY_PORT"):
            proxy["port"] = int(e("PROXY_PORT"))
        if e("PROXY_CLASSIFIER_DISABLE_THINKING"):
            proxy.setdefault("classifier", {})["disable_thinking"] = _parse_bool(
                e("PROXY_CLASSIFIER_DISABLE_THINKING")
            )


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
