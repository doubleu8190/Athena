"""配置管理 — 基于 Pydantic Settings 的统一配置入口。

所有配置项通过环境变量或 ``.env`` 文件注入，支持嵌套分隔符 ``__``。
使用 ``get_settings()`` 获取全局单例实例。

配置分组：
- 服务端: 服务端口、调试模式
- LLM 提供者: 主/副/兜底 LLM 配置
- 数据库: PostgreSQL 和 ChromaDB 配置
- File Intelligence: 文件处理参数
- Harness: Agent 执行参数
- Memory: 记忆系统参数
- 上下文压缩: 上下文压缩参数
- Docker 沙箱: 沙箱执行环境
- 审批: 审批流程参数
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMProviderConfig(BaseModel):
    """单个 LLM Provider 的配置。

    属性：
        name: 标识名 — ``"primary"``（主）/ ``"secondary"``（副）/ ``"fallback"``（兜底）。
        provider: 厂商标识 — ``"openai"`` / ``"anthropic"`` / ``"deepseek"`` / ``"ollama"``。
        model: 模型名称（如 ``"gpt-4o"``、``"claude-3-opus"``）。
        api_key: API 密钥（ollama 可留空）。
        base_url: 自定义 API 端点（留空使用厂商默认）。
        temperature: 采样温度（``-1`` 表示使用全局默认值）。
        max_tokens: 最大输出 token 数（``-1`` 表示使用全局默认值）。
        supports_vision: 是否支持视觉输入（图片分析功能）。
    """

    name: str = "primary"
    provider: str = "openai"
    model: str = "gpt-4o"
    api_key: str = ""
    base_url: str = ""
    temperature: float = -1
    max_tokens: int = -1
    supports_vision: bool = False


class LLMRetrySettings(BaseModel):
    """LLM 调用的指数退避重试参数。

    属性：
        max_attempts: 最大重试次数。
        min_delay_ms: 最小重试延迟（毫秒）。
        max_delay_ms: 最大重试延迟（毫秒）。
        jitter: 延迟抖动系数（0-1），防止重试风暴。
        timeout_ms: 单次调用超时时间（毫秒）。
    """

    max_attempts: int = Field(default=3, ge=1)
    min_delay_ms: int = Field(default=2000, ge=0)
    max_delay_ms: int = Field(default=30000, ge=0)
    jitter: float = Field(default=0.1, ge=0, le=1)
    timeout_ms: int = Field(default=60000, ge=1)


class Settings(BaseSettings):
    """全局配置，通过环境变量 / .env 文件注入。

    使用 ``env_nested_delimiter="__"`` 支持嵌套配置（如 ``LLM__TEMPERATURE``）。
    使用 ``get_settings()`` 获取缓存的单例实例。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # --- 服务端 ---
    host: str = "127.0.0.1"
    port: int = 8000
    debug: bool = True
    auth_enabled: bool = False
    auth_username: str = "athena"
    auth_password: str = "change-me"
    auth_session_secret: str = ""
    auth_session_ttl_hours: int = Field(default=24, ge=1)

    # --- LLM 提供者 ---
    # 按优先级排列：primary → secondary → fallback
    llm_providers: list[LLMProviderConfig] = Field(
        default_factory=lambda: [
            LLMProviderConfig(name="primary", provider="openai", model="gpt-4o"),
        ]
    )
    # 全局 LLM 参数（provider 级别的值为 -1 时使用这些默认值）
    llm_temperature: float = 0.7
    llm_max_tokens: int = 4096
    llm_retry: LLMRetrySettings = Field(default_factory=LLMRetrySettings)
    llm_secondary_retry: LLMRetrySettings = Field(
        default_factory=lambda: LLMRetrySettings(
            max_attempts=2,
            min_delay_ms=1000,
            max_delay_ms=15000,
            jitter=0.1,
            timeout_ms=30000,
        )
    )

    # --- PostgreSQL ---
    postgres_user: str = "doubleu"
    postgres_password: str = ""
    postgres_db: str = "athena"
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_pool_size: int = Field(default=10, ge=1)
    postgres_max_overflow: int = Field(default=10, ge=0)
    chromadb_path: str = "./data/chromadb"
    # Chroma 向量模型。默认模型覆盖中文和英文，避免使用 Chroma 的英文默认模型。
    chroma_embedding_provider: str = "sentence_transformers"
    chroma_embedding_model: str = "paraphrase-multilingual-MiniLM-L12-v2"
    chroma_embedding_device: str = "cpu"
    chroma_embedding_version: str = "v1"
    chroma_embedding_api_key: str = ""
    chroma_embedding_base_url: str = ""

    # --- 文件智能 ---
    file_storage_path: str = "./data/files"
    file_max_upload_bytes: int = 512 * 1024 * 1024  # 512MB
    file_chunk_tokens: int = 800  # 分块目标 token 数
    file_chunk_overlap_tokens: int = 80  # 分块重叠 token 数
    file_parse_concurrency: int = 2
    file_embedding_concurrency: int = 2
    # PDF 混合页 OCR：保留原生文本，同时识别页面内嵌图片中的文字。
    pdf_ocr_mixed_pages: bool = True
    pdf_ocr_min_image_pixels: int = Field(default=4096, ge=0)
    pdf_ocr_max_images_per_page: int = Field(default=20, ge=1, le=100)

    # --- Harness 执行引擎 ---
    max_turns_per_run: int = 20
    retry_budget: int = 3
    tool_timeout: int = 60  # 秒
    llm_stream_timeout: int = 120  # 秒
    approval_timeout: int = 120  # 秒

    # --- 记忆 ---
    summary_threshold: int = 10  # 每 N 轮对话触发摘要
    memory_sync_interval: int = 900  # 15 分钟
    memory_min_score: float = 0.7
    memory_ttl_days: int = 90
    memory_access_window_days: int = 7
    vector_weight: float = 0.75  # 向量检索权重
    keyword_weight: float = 0.25  # 关键词检索权重
    rrf_k: int = 60  # RRF 融合参数
    retrieval_top_k: int = 5
    # PR-02: 检索候选、重排和最终上下文使用独立的容量。
    retrieval_candidate_k: int = 30
    retrieval_rerank_k: int = 10
    retrieval_context_k: int = 5
    memory_vector_min_score: float = 0.70
    memory_max_tokens: int = 2000
    # Memory is an optional enhancement. It must not hold the first answer
    # indefinitely when the vector backend or embedding service is slow.
    memory_retrieval_timeout_seconds: float = Field(default=60.0, ge=0.1, le=60.0)

    # --- Task Understanding / Context Providers ---
    task_understanding_timeout_seconds: float = Field(default=120.0, ge=0.1, le=120.0)
    knowledge_context_max_files: int = Field(default=5, ge=1, le=20)
    knowledge_context_limit_per_file: int = Field(default=5, ge=1, le=20)
    knowledge_context_max_items: int = Field(default=12, ge=1, le=50)
    knowledge_context_max_tokens: int = Field(default=4000, ge=100, le=20000)

    # --- 上下文压缩 ---
    max_context_tokens: int = 128000
    compression_threshold: float = 0.8  # 上下文使用率阈值
    keep_recent_turns: int = 3
    max_summary_tokens: int = 2000
    summary_incremental: bool = True
    save_summary_to_memory: bool = True

    # --- 审批 ---
    approval_batch_mode: str = "sequential"  # 串行 / 批量
    approval_keyboard_shortcuts: bool = True
    approval_sound_alert: bool = False

    # --- Docker sandbox ---
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
        """获取主 provider 配置（列表第一个）。

        返回值：
            主 LLM 配置。

        异常：
            ValueError: llm_providers 为空时。
        """
        if not self.llm_providers:
            raise ValueError("llm_providers 不能为空")
        return self.llm_providers[0]

    @property
    def secondary_llm(self) -> LLMProviderConfig | None:
        """获取副 provider 配置（列表第二个，用于次要任务）。

        返回值：
            副 LLM 配置，不存在时返回 ``None``。
        """
        return self.llm_providers[1] if len(self.llm_providers) > 1 else None

    @property
    def fallback_llm_list(self) -> list[LLMProviderConfig]:
        """获取所有 fallback provider 配置（secondary 之后的）。

        返回值：
            fallback 配置列表。
        """
        return self.llm_providers[2:] if len(self.llm_providers) > 2 else []

    @property
    def postgres_url(self) -> str:
        """Return the SQLAlchemy async PostgreSQL URL with safely escaped credentials."""
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
        """Return the native psycopg connection string used by LangGraph."""
        return self.postgres_url.replace("postgresql+psycopg://", "postgresql://", 1)

    @property
    def chroma_path(self) -> Path:
        """ChromaDB 持久化目录路径。"""
        return Path(self.chromadb_path)

    @property
    def files_path(self) -> Path:
        """文件存储根目录路径。"""
        return Path(self.file_storage_path)

@lru_cache
def get_settings() -> Settings:
    """获取全局配置单例（缓存，进程生命周期内只创建一次）。

    返回值：
        ``Settings`` 实例。
    """
    return Settings()
