"""Unified configuration (schema version 2).

Priority (highest first):

    CLI argument  >  Environment variable  >  Config file  >  Built-in default

The configuration has no ``mcp:`` section by design: the only MCP-level
decision - whether the MCP process should start the shared server - is passed
by the agent's MCP config as a CLI argument (``--start-server`` /
``--no-start-server``) or env var, not by this file.

Top-level nodes:

- ``server``   - the shared singleton service (host/port only). Per-hook config
  lives under the top-level ``hooks`` node.
- ``hooks``    - ``image`` / ``classifier`` hook switches + model allowlists.
- ``providers``- the single source of truth for provider instances. The dual
  chains reference these instances by ``name``.
- ``vision``   - image timeout/concurrency/fallback + the two execution chains
  (``image_chain`` and ``classifier_chain``).
- ``media``, ``logging``.

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
from .providers.types import ProviderFailureReason

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
    """One provider instance (single source of truth, defined at top level).

    ``mode`` selects the vendor dialect for the multi-mode providers
    (opencode ``go``/``zen``; volcengine ``agent``/``coding``/``api``) and is
    ignored by providers that have a single mode. ``disable_thinking`` is only
    read by the API providers that implement classifier handling
    (gemini/opencode/volcengine) - CLI providers (agy/codex) ignore it.
    """

    name: str
    # Registry key into the provider registry - decouples config from classes.
    type: str
    enabled: bool = True
    command: Optional[str] = None  # CLI-based providers
    model: Optional[str] = None
    effort: Optional[str] = None
    mode: Optional[str] = None  # multi-mode vendors (opencode/volcengine)
    # API-based providers.
    api_key_env: Optional[str] = None
    api_key: Optional[pd.SecretStr] = None
    base_url: Optional[str] = None
    timeout: Optional[float] = None
    # Provider-level classifier knob: disable classifier thinking? Only honored
    # by API providers that implement classifier capability.
    disable_thinking: Optional[bool] = None
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


def _default_fallback_reasons() -> list:
    """Lazy default reasons: imports types on first use to avoid a config cycle."""
    from .providers.types import ProviderFailureReason

    return sorted(
        ProviderFailureReason.fallback_eligible_defaults(), key=lambda r: str(r)
    )


class FallbackConfig(pd.BaseModel):
    enabled: bool = True
    on: list["ProviderFailureReason"] = pd.Field(default_factory=_default_fallback_reasons)

    def reasons(self) -> set["ProviderFailureReason"]:
        return set(self.on)


class VisionConfig(pd.BaseModel):
    timeout: float = 120.0
    max_concurrency: int = 2
    fallback: FallbackConfig = pd.Field(default_factory=FallbackConfig)
    # Ordered name lists referencing the top-level provider instances.
    image_chain: list[str] = pd.Field(default_factory=lambda: ["gemini"])
    classifier_chain: list[str] = pd.Field(default_factory=lambda: ["gemini"])


class ImageHookConfig(pd.BaseModel):
    """Image request hook: rewrite image parts into text descriptions."""

    enabled: bool = True
    # Model allowlist: empty = apply to all models; non-empty = only listed models.
    models: list[str] = pd.Field(default_factory=list)


class ClassifierHookConfig(pd.BaseModel):
    """Claude Code Auto-mode classifier compatibility hook.

    The hook only detects a classifier request and delegates rewriting to the
    provider chain. It performs no built-in thinking-disable or normalization -
    providers decide. There is intentionally no ``disable_thinking`` here; it
    moved to the provider level (``ProviderEntryConfig.disable_thinking``).
    """

    enabled: bool = True
    models: list[str] = pd.Field(default_factory=list)


class HooksConfig(pd.BaseModel):
    image: ImageHookConfig = pd.Field(default_factory=ImageHookConfig)
    classifier: ClassifierHookConfig = pd.Field(default_factory=ClassifierHookConfig)


class ServerConfig(pd.BaseModel):
    """The shared singleton server (vision endpoint + request hooks)."""

    host: str = "127.0.0.1"
    port: int = 8787


class MediaConfig(pd.BaseModel):
    max_image_mb: float = 20.0
    download_timeout: float = 30.0
    max_download_mb: float = 32.0


class LoggingConfig(pd.BaseModel):
    level: str = "INFO"


def _default_providers() -> list[ProviderEntryConfig]:
    """The built-in default provider chain: only gemini (both chains reference it)."""
    return [ProviderEntryConfig(name="gemini", type="gemini", api_key_env="GEMINI_API_KEY")]


class AppConfig(pd.BaseModel):
    version: int = CONFIG_VERSION
    server: ServerConfig = pd.Field(default_factory=ServerConfig)
    hooks: HooksConfig = pd.Field(default_factory=HooksConfig)
    providers: list[ProviderEntryConfig] = pd.Field(default_factory=_default_providers)
    vision: VisionConfig = pd.Field(default_factory=VisionConfig)
    media: MediaConfig = pd.Field(default_factory=MediaConfig)
    logging: LoggingConfig = pd.Field(default_factory=LoggingConfig)

    def validate_all(self) -> None:
        from .providers import PROVIDER_TYPES

        names = [e.name for e in self.providers]
        if len(set(names)) != len(names):
            raise ConfigError("providers contains duplicate names")
        for entry in self.providers:
            if entry.type not in PROVIDER_TYPES:
                raise ConfigError(
                    f"unknown provider type {entry.type!r} (provider {entry.name!r})"
                )
        self._validate_chain("image_chain", self.vision.image_chain)
        self._validate_chain("classifier_chain", self.vision.classifier_chain)

    def _validate_chain(self, key: str, chain: list[str]) -> None:
        enabled_names = {e.name for e in self.providers if e.enabled}
        all_names = {e.name for e in self.providers}
        for name in chain:
            if name not in all_names:
                raise ConfigError(
                    f"vision.{key} references undefined provider {name!r}"
                )
            if name not in enabled_names:
                raise ConfigError(
                    f"vision.{key} references provider {name!r} which is disabled"
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
        data = dict(_defaults())

        # 2. Config file.
        path = self.resolve_config_path()
        if path is not None:
            if not path.exists():
                raise ConfigError(f"config file not found: {path}")
            file_data = dict(self._read_file(path))
            data = _deep_merge(data, file_data)

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

        hooks = data.setdefault("hooks", {})
        if e("IMAGE_HOOK"):
            hooks.setdefault("image", {})["enabled"] = _parse_bool(e("IMAGE_HOOK"))
        if e("CLASSIFIER_HOOK"):
            hooks.setdefault("classifier", {})["enabled"] = _parse_bool(e("CLASSIFIER_HOOK"))

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


def _defaults() -> dict:
    """Built-in default config.

    ``AppConfig`` already defaults ``providers`` to the single gemini provider
    and ``vision`` chains to ``["gemini"]``, so no explicit defaults are needed
    here; the empty base lets file/env overrides merge cleanly.
    """
    return {}


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge ``override`` into ``base`` recursively (lists replace, dicts merge)."""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


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