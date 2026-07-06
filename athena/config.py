"""Configuration loading from YAML files and environment variables."""

from __future__ import annotations

import json
import os
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from athena.core.secrets import get_secret
from athena.logging_config import get_logger

logger = get_logger(__name__)

# ── Default paths (overridable via env vars) ──────────────────────────

DEFAULT_DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DEFAULT_SYSTEM_CONFIG_PATH = Path(
    os.environ.get("SYSTEM_CONFIG_PATH", DEFAULT_DATA_DIR / "athena.yaml")
)
DEFAULT_USER_CONFIG_PATH = Path(
    os.environ.get("USER_CONFIG_PATH", DEFAULT_DATA_DIR / "user.yaml")
)
DEFAULT_LLM_CONFIG_PATH = Path(
    os.environ.get("LLM_CONFIG_PATH", DEFAULT_DATA_DIR / "llm.yaml")
)
DEFAULT_MCP_SERVERS_CONFIG = Path(
    os.environ.get("MCP_SERVERS_CONFIG", DEFAULT_DATA_DIR / "mcp_servers.json")
)
# ── Configuration dataclasses ──────────────────────────────────────────


@dataclass
class SystemConfig:
    """System-global configuration from athena.yaml."""

    max_fallback_depth: int = 1
    global_max_concurrent_tasks: int = 8
    session_idle_timeout_minutes: int = 30
    session_expire_hours: int = 24

    # Harness defaults
    circuit_breaker_threshold: int = 3
    cooling_off_defaults: dict[str, int] = field(default_factory=lambda: {
        "low": 0,
        "medium": 15,
        "high": 30,
        "critical": 60,
    })

    # MCP defaults
    mcp_heartbeat_interval_seconds: int = 30
    mcp_reconnect_backoff_max_seconds: int = 60
    mcp_tools_list_refresh_on_reconnect: bool = True


@dataclass
class LLMProviderConfig:
    """Configuration for a single LLM provider."""

    api_key_env: str = ""
    model: str = ""
    max_tokens: int = 4096
    base_url: str | None = None
    temperature: float = 0.7
    format: str = "openai"  # "openai" or "anthropic"   


@dataclass
class LLMConfig:
    """LLM configuration from llm.yaml.

    Two models only:
    - ``default_provider``: strong model for main agent tasks
    - ``default_summarize_provider``: fast/cheap model for summarization
    """

    default_provider: str = "openai"
    default_summarize_provider: str = ""
    providers: dict[str, LLMProviderConfig] = field(default_factory=dict)


@dataclass
class UserChannelConfig:
    """Per-channel user identity."""

    user_id: str = ""


@dataclass
class UserConfig:
    """User configuration from user.yaml."""

    telegram: UserChannelConfig = field(default_factory=UserChannelConfig)
    web: UserChannelConfig = field(default_factory=UserChannelConfig)
    wechat: UserChannelConfig = field(default_factory=UserChannelConfig)

    def is_channel_enabled(self, channel: str) -> bool:
        """Check if a channel has a configured user_id."""
        cfg = getattr(self, channel, None)
        return cfg is not None and bool(cfg.user_id)


@dataclass
class Config:
    """Root configuration aggregating all config sources."""

    system: SystemConfig = field(default_factory=SystemConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    user: UserConfig = field(default_factory=UserConfig)
    mcp_servers_seed: list[dict[str, Any]] = field(default_factory=list)

    # Paths
    sqlite_db_path: str = field(
        default_factory=lambda: os.environ.get("SQLITE_DB_PATH", "/data/athena.db")
    )
    chroma_persist_dir: str = field(
        default_factory=lambda: os.environ.get("CHROMA_PERSIST_DIR", "/data/chroma")
    )
    redis_url: str = field(
        default_factory=lambda: os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    )
    celery_broker_url: str = field(
        default_factory=lambda: os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/1")
    )
    celery_result_backend: str = field(
        default_factory=lambda: os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/2")
    )
    admin_api_key: str = field(
        default_factory=lambda: get_secret("ADMIN_API_KEY", "")
    )
    device_psk: str = field(
        default_factory=lambda: get_secret("DEVICE_PSK", "")
    )
    log_level: str = field(
        default_factory=lambda: os.environ.get("LOG_LEVEL", "INFO")
    )
    prometheus_enabled: bool = field(
        default_factory=lambda: os.environ.get("PROMETHEUS_ENABLED", "true").lower() == "true"
    )
    cors_origins: list[str] = field(
        default_factory=lambda: [
            o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",")
        ]
    )

    @classmethod
    def load(cls) -> Config:
        """Load all configuration from YAML files and environment variables.

        Returns a Config instance. Missing optional config files are silently
        skipped with defaults applied.
        """
        config = cls()

        # Load system config (optional)
        config.system = cls._load_system_config()

        # Load LLM config (required)
        config.llm = cls._load_llm_config()

        # Load user config (required)
        config.user = cls._load_user_config()

        # Load MCP servers seed (optional)
        config.mcp_servers_seed = cls._load_mcp_servers_seed()

        return config

    @classmethod
    def _load_yaml(cls, path: Path) -> dict[str, Any]:
        """Load a YAML file, returning empty dict if not found."""
        if not path.exists():
            return {}
        with open(path, "r") as f:
            return yaml.safe_load(f) or {}

    @classmethod
    def _load_system_config(cls) -> SystemConfig:
        if not DEFAULT_SYSTEM_CONFIG_PATH.exists():
            logger.warning(f"Warning: {DEFAULT_SYSTEM_CONFIG_PATH} does not exist")
            
        data = cls._load_yaml(DEFAULT_SYSTEM_CONFIG_PATH)
        system_data = data.get("system", {})
        harness_data = data.get("harness", {})
        mcp_data = data.get("mcp", {})

        return SystemConfig(
            max_fallback_depth=system_data.get("max_fallback_depth", 1),
            global_max_concurrent_tasks=system_data.get("global_max_concurrent_tasks", 8),
            session_idle_timeout_minutes=system_data.get("session_idle_timeout_minutes", 30),
            session_expire_hours=system_data.get("session_expire_hours", 24),
            circuit_breaker_threshold=harness_data.get("circuit_breaker_threshold", 3),
            cooling_off_defaults=harness_data.get("cooling_off_defaults", {
                "low": 0, "medium": 15, "high": 30, "critical": 60,
            }),
            mcp_heartbeat_interval_seconds=mcp_data.get("heartbeat_interval_seconds", 30),
            mcp_reconnect_backoff_max_seconds=mcp_data.get("reconnect_backoff_max_seconds", 60),
            mcp_tools_list_refresh_on_reconnect=mcp_data.get(
                "tools_list_refresh_on_reconnect", True
            ),
        )

    @classmethod
    def _load_llm_config(cls) -> LLMConfig:
        if not DEFAULT_LLM_CONFIG_PATH.exists():
            logger.warning(f"Warning: {DEFAULT_LLM_CONFIG_PATH} does not exist")
        data = cls._load_yaml(DEFAULT_LLM_CONFIG_PATH)
        providers_raw = data.get("providers", {})
        providers = {}
        for name, pdata in providers_raw.items():
            providers[name] = LLMProviderConfig(
                api_key_env=pdata.get("api_key_env", ""),
                model=pdata.get("model", ""),
                max_tokens=pdata.get("max_tokens", 4096),
                base_url=pdata.get("base_url"),
                temperature=pdata.get("temperature", 0.7),
                format=pdata.get("format", "openai"),
            )
        return LLMConfig(
            default_provider=data.get("default_provider", ""),
            default_summarize_provider=data.get("default_summarize_provider", ""),
            providers=providers,
        )

    @classmethod
    def _load_user_config(cls) -> UserConfig:
        if not DEFAULT_USER_CONFIG_PATH.exists():
            logger.warning(f"Warning: {DEFAULT_USER_CONFIG_PATH} does not exist")
        data = cls._load_yaml(DEFAULT_USER_CONFIG_PATH)
        user_data = data.get("user", {})
        return UserConfig(
            telegram=UserChannelConfig(
                user_id=user_data.get("telegram", {}).get("user_id", "")
            ),
            web=UserChannelConfig(
                user_id=user_data.get("web", {}).get("user_id", "")
            ),
            wechat=UserChannelConfig(
                user_id=user_data.get("wechat", {}).get("user_id", "")
            ),
        )

    @classmethod
    def _load_mcp_servers_seed(cls) -> list[dict[str, Any]]:
        """Load built-in MCP server definitions from Claude Desktop format JSON.

        Parses the ``mcpServers`` dict and returns a list of server entries
        compatible with the internal seed format.  Transport is inferred from
        the presence of a ``url`` key (sse/http) and defaults to stdio.
        All servers loaded from this file are marked ``source: builtin``.
        """
        if not DEFAULT_MCP_SERVERS_CONFIG.exists():
            logger.warning(f"Warning: {DEFAULT_MCP_SERVERS_CONFIG} does not exist")
            return []

        with open(DEFAULT_MCP_SERVERS_CONFIG, "r") as f:
            data = json.load(f) or {}

        servers = data.get("mcpServers", {})
        if not isinstance(servers, dict):
            logger.warning("mcp_servers_config_invalid_format")
            return []

        result: list[dict[str, Any]] = []
        for server_id, config in servers.items():
            if not isinstance(config, dict):
                continue

            # Infer transport from config shape
            if "url" in config:
                transport = "sse" if "sse" in str(config["url"]).lower() else "http"
            else:
                transport = "stdio"

            result.append({
                "server_id": server_id,
                "name": server_id,
                "transport": transport,
                "connection_config": config,
                "source": "builtin",
            })

        return result


# Module-level singleton (initialized at app startup)
_config: Config | None = None


def get_config() -> Config:
    """Get the current configuration singleton."""
    global _config
    if _config is None:
        _config = Config.load()
    return _config


def set_config(config: Config) -> None:
    """Set the configuration singleton (for testing)."""
    global _config
    _config = config
