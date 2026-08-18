"""配置管理 — 基于 Pydantic Settings 的统一配置入口."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMProviderConfig(BaseModel):
    """单个 LLM Provider 的配置.

    Attributes:
        name: 标识名 — primary(主) / secondary(副) / fallback(兜底)
        provider: 厂商标识 — openai / anthropic / deepseek / ollama
        model: 模型名称
        api_key: API 密钥（ollama 可留空）
        base_url: 自定义 API 端点（留空使用厂商默认）
        temperature: 采样温度（-1 则使用全局默认值）
        max_tokens: 最大输出 token 数（-1 则使用全局默认值）
    """

    name: str = "primary"
    provider: str = "openai"
    model: str = "gpt-4o"
    api_key: str = ""
    base_url: str = ""
    temperature: float = -1
    max_tokens: int = -1
    supports_vision: bool = False


class Settings(BaseSettings):
    """全局配置，通过环境变量 / .env 文件注入."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Server ---
    host: str = "127.0.0.1"
    port: int = 8000
    debug: bool = True

    # --- LLM Providers ---
    # 按优先级排列：primary → secondary → fallback
    llm_providers: list[LLMProviderConfig] = Field(
        default_factory=lambda: [
            LLMProviderConfig(name="primary", provider="openai", model="gpt-4o"),
        ]
    )
    # 全局 LLM 参数（provider 级别的值为 -1 时使用这些默认值）
    llm_temperature: float = 0.7
    llm_max_tokens: int = 4096
    

    # --- Database ---
    sqlite_db_path: str = "./data/athena.db"
    chromadb_path: str = "./data/chromadb"

    # --- File Intelligence ---
    file_storage_path: str = "./data/files"
    file_max_upload_bytes: int = 512 * 1024 * 1024
    file_chunk_tokens: int = 800
    file_chunk_overlap_tokens: int = 80
    file_task_max_attempts: int = 3
    file_task_timeout: int = 900
    file_task_poll_interval: float = 0.25
    file_parse_concurrency: int = 2
    file_code_concurrency: int = 1
    file_summary_concurrency: int = 4
    file_embedding_concurrency: int = 2

    # --- Harness ---
    max_turns_per_run: int = 20
    retry_budget: int = 3
    tool_timeout: int = 60
    llm_stream_timeout: int = 120
    approval_timeout: int = 120

    # --- Memory ---
    summary_threshold: int = 10
    memory_sync_interval: int = 900
    memory_min_score: float = 0.7
    memory_ttl_days: int = 90
    memory_access_window_days: int = 7
    vector_weight: float = 0.75
    keyword_weight: float = 0.25
    rrf_k: int = 60
    retrieval_top_k: int = 5
    memory_max_tokens: int = 2000

    # --- Context Compression ---
    max_context_tokens: int = 128000
    compression_threshold: float = 0.8
    keep_recent_turns: int = 3
    max_summary_tokens: int = 2000
    summary_incremental: bool = True
    save_summary_to_memory: bool = True

    # --- Docker Sandbox ---
    sandbox_enabled: bool = False
    sandbox_image: str = "athena-sandbox:latest"
    sandbox_network_disabled: bool = True
    sandbox_memory_limit: str = "256m"
    sandbox_cpu_limit: float = 0.5
    sandbox_workspace_path: str = "/workspace"
    sandbox_check_interval: int = 300
    sandbox_allowed_paths: str = ""

    # --- Approval ---
    approval_batch_mode: str = "sequential"  # sequential / batch
    approval_keyboard_shortcuts: bool = True
    approval_sound_alert: bool = False

    @property
    def primary_llm(self) -> LLMProviderConfig:
        """获取主 provider 配置."""
        if not self.llm_providers:
            raise ValueError("llm_providers 不能为空")
        return self.llm_providers[0]

    @property
    def secondary_llm(self) -> LLMProviderConfig | None:
        """获取副 provider 配置（模型能力稍弱，用于次要任务）."""
        return self.llm_providers[1] if len(self.llm_providers) > 1 else None

    @property
    def fallback_llm_list(self) -> list[LLMProviderConfig]:
        """获取所有 fallback provider 配置（secondary 之后的）."""
        return self.llm_providers[2:] if len(self.llm_providers) > 2 else []

    @property
    def db_path(self) -> Path:
        return Path(self.sqlite_db_path)

    @property
    def chroma_path(self) -> Path:
        return Path(self.chromadb_path)

    @property
    def files_path(self) -> Path:
        return Path(self.file_storage_path)

    @property
    def allowed_paths_list(self) -> list[str]:
        if not self.sandbox_allowed_paths:
            return []
        return [p.strip() for p in self.sandbox_allowed_paths.split(",") if p.strip()]


@lru_cache
def get_settings() -> Settings:
    """获取全局配置单例."""
    return Settings()
