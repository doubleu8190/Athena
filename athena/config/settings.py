"""配置管理 — 基于 Pydantic Settings 的统一配置入口."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # --- LLM Provider ---
    llm_provider: str = "openai"  # openai/anthropic/deepseek/ollama
    llm_model: str = "gpt-4o"
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_temperature: float = 0.7
    llm_max_tokens: int = 4096

    # --- Database ---
    sqlite_db_path: str = "./data/athena.db"
    chromadb_path: str = "./data/chromadb"

    # --- Harness ---
    max_turns_per_run: int = 20
    retry_budget: int = 3
    tool_timeout: int = 60
    approval_timeout: int = 120

    # --- Memory ---
    summary_threshold: int = 10
    memory_sync_interval: int = 900
    memory_min_score: float = 0.7
    memory_ttl_days: int = 90
    vector_weight: float = 0.6
    keyword_weight: float = 0.4
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
    def db_path(self) -> Path:
        return Path(self.sqlite_db_path)

    @property
    def chroma_path(self) -> Path:
        return Path(self.chromadb_path)

    @property
    def allowed_paths_list(self) -> list[str]:
        if not self.sandbox_allowed_paths:
            return []
        return [p.strip() for p in self.sandbox_allowed_paths.split(",") if p.strip()]


@lru_cache
def get_settings() -> Settings:
    """获取全局配置单例."""
    return Settings()
