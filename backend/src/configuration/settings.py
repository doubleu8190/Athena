"""Typed process configuration owned by the target application."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMProviderConfig(BaseModel):
    name: str = "primary"
    provider: str = "openai"
    model: str = "gpt-4o"
    api_key: str = ""
    base_url: str = ""
    temperature: float = -1
    max_tokens: int = -1
    supports_vision: bool = False


class LLMRetrySettings(BaseModel):
    max_attempts: int = Field(default=3, ge=1)
    min_delay_ms: int = Field(default=2000, ge=0)
    max_delay_ms: int = Field(default=30000, ge=0)
    jitter: float = Field(default=0.1, ge=0, le=1)
    timeout_ms: int = Field(default=60000, ge=1)


class Settings(BaseSettings):
    """Configuration needed by bootstrap and infrastructure adapters.

    The names intentionally remain compatible with the deployed environment;
    changing them during the package migration would be an unrelated API break.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 8000
    debug: bool = True
    langchain_tracing_v2: bool = False
    langchain_project: str = "athena"
    langchain_endpoint: str = "https://api.smith.langchain.com"
    langchain_api_key: str = ""
    auth_enabled: bool = False
    auth_username: str = "athena"
    auth_password: str = "change-me"
    auth_session_secret: str = ""
    auth_session_ttl_hours: int = Field(default=24, ge=1)

    database_backend: Literal["postgres"] = "postgres"
    postgres_create_schema: bool = True
    workers_enabled: bool = False

    llm_providers: list[LLMProviderConfig] = Field(
        default_factory=lambda: [LLMProviderConfig()]
    )
    llm_temperature: float = 0.7
    llm_max_tokens: int = 4096
    llm_retry: LLMRetrySettings = Field(default_factory=LLMRetrySettings)
    llm_secondary_retry: LLMRetrySettings = Field(
        default_factory=lambda: LLMRetrySettings(
            max_attempts=2, min_delay_ms=1000, max_delay_ms=15000, timeout_ms=30000
        )
    )

    postgres_user: str = "doubleu"
    postgres_password: str = ""
    postgres_db: str = "athena"
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_pool_size: int = Field(default=10, ge=1)
    postgres_max_overflow: int = Field(default=10, ge=0)
    embedding_provider: str = "sentence_transformers"
    embedding_model: str = "BAAI/bge-m3"
    embedding_device: str = "cpu"
    embedding_version: str = "v2"
    embedding_dimension: int = Field(default=1024, ge=1)
    embedding_api_key: str = ""
    embedding_base_url: str = ""

    neo4j_uri: str = "bolt://127.0.0.1:7687"
    neo4j_username: str = "neo4j"
    neo4j_password: str = ""
    neo4j_database: str = "neo4j"
    neo4j_max_connection_pool_size: int = Field(default=20, ge=1, le=200)
    neo4j_connection_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    neo4j_query_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    graph_max_hops: int = Field(default=2, ge=1, le=2)
    graph_entity_limit: int = Field(default=8, ge=1, le=50)
    graph_path_limit: int = Field(default=12, ge=1, le=100)
    graph_min_confidence: float = Field(default=0.65, ge=0, le=1)
    graph_extraction_concurrency: int = Field(default=2, ge=1, le=16)
    graph_timeout_seconds: float = Field(default=5.0, gt=0, le=60)

    file_storage_path: str = "./data/files"
    file_max_upload_bytes: int = 512 * 1024 * 1024
    file_chunk_tokens: int = 800
    file_chunk_overlap_tokens: int = 80
    file_parse_concurrency: int = 2
    file_embedding_concurrency: int = 2
    pdf_ocr_mixed_pages: bool = True
    pdf_ocr_min_image_pixels: int = Field(default=4096, ge=0)
    pdf_ocr_max_images_per_page: int = Field(default=20, ge=1, le=100)

    max_turns_per_run: int = 20
    retry_budget: int = 3
    tool_timeout: int = 60
    llm_timeout: int = 120
    summary_threshold: int = 10
    memory_sync_interval: int = 900
    memory_min_score: float = 0.7
    memory_ttl_days: int = 90
    memory_access_window_days: int = 7
    vector_weight: float = 0.75
    keyword_weight: float = 0.25
    rrf_k: int = 60
    retrieval_top_k: int = 5
    retrieval_candidate_k: int = 30
    retrieval_rerank_k: int = 10
    retrieval_context_k: int = 5
    memory_vector_min_score: float = 0.70
    memory_max_tokens: int = 2000
    memory_retrieval_timeout_seconds: float = Field(default=60.0, ge=0.1, le=60.0)

    task_understanding_timeout_seconds: float = Field(default=120.0, ge=0.1, le=120.0)
    knowledge_context_max_files: int = Field(default=5, ge=1, le=20)
    knowledge_context_limit_per_file: int = Field(default=5, ge=1, le=20)
    knowledge_context_max_items: int = Field(default=12, ge=1, le=50)
    knowledge_context_max_tokens: int = Field(default=4000, ge=100, le=20000)
    file_rerank_model: str = "BAAI/bge-reranker-v2-m3"
    file_rerank_device: str | None = None
    file_rerank_batch_size: int = Field(default=16, ge=1, le=128)
    file_rerank_max_chars: int = Field(default=4000, ge=256, le=20000)
    file_rerank_candidate_k: int = Field(default=50, ge=1, le=200)
    file_rerank_k: int = Field(default=20, ge=1, le=100)
    knowledge_rerank_candidate_k: int = Field(default=100, ge=1, le=500)
    knowledge_rerank_k: int = Field(default=30, ge=1, le=200)
    knowledge_result_k: int = Field(default=12, ge=1, le=50)

    max_context_tokens: int = 128000
    compression_threshold: float = 0.8
    keep_recent_turns: int = 3
    max_summary_tokens: int = 2000
    summary_incremental: bool = True
    save_summary_to_memory: bool = True
    approval_batch_mode: str = "sequential"
    approval_keyboard_shortcuts: bool = True
    approval_sound_alert: bool = False

    sandbox_enabled: bool = True
    sandbox_required: bool = False
    sandbox_docker_binary: str = "docker"
    sandbox_workspace_root: str = "./data/sandboxes"
    sandbox_shell_image: str = "python:3.12-slim"
    sandbox_image_allowlist: list[str] = Field(default_factory=list)
    sandbox_mcp_images: dict[str, str] = Field(default_factory=dict)
    sandbox_default_network: str = "none"
    sandbox_max_timeout: int = Field(default=60, ge=1, le=3600)
    sandbox_max_output_bytes: int = Field(default=1_000_000, ge=1024)
    sandbox_memory_mb: int = Field(default=512, ge=64)
    sandbox_cpu_limit: float = Field(default=1.0, gt=0, le=8)
    sandbox_pids_limit: int = Field(default=128, ge=16, le=4096)
    sandbox_require_digest: bool = False
    sandbox_mcp_auto_restore: bool = False
    exec_shell_enabled: bool = True

    @property
    def primary_llm(self) -> LLMProviderConfig:
        if not self.llm_providers:
            raise ValueError("llm_providers must not be empty")
        return self.llm_providers[0]

    @property
    def secondary_llm(self) -> LLMProviderConfig | None:
        return self.llm_providers[1] if len(self.llm_providers) > 1 else None

    @property
    def fallback_llm_list(self) -> list[LLMProviderConfig]:
        return self.llm_providers[2:]

    @property
    def postgres_url(self) -> str:
        from sqlalchemy.engine import URL

        return URL.create(
            "postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        ).render_as_string(hide_password=False)

    @property
    def postgres_conn_string(self) -> str:
        return self.postgres_url.replace("postgresql+psycopg://", "postgresql://", 1)

    @property
    def files_path(self) -> Path:
        return Path(self.file_storage_path)


@lru_cache
def get_settings() -> Settings:
    return Settings()


__all__ = ["LLMProviderConfig", "LLMRetrySettings", "Settings", "get_settings"]
