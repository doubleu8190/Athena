"""系统设置路由 — 只读展示启动时加载的配置."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from athena.config.settings import LLMRetrySettings

router = APIRouter(prefix="/settings", tags=["settings"])


class SettingsView(BaseModel):
    """系统设置只读视图（绝不包含 api_key）."""

    # 服务端
    host: str
    port: int
    debug: bool

    # 数据库
    postgres_user: str
    postgres_db: str
    postgres_host: str
    postgres_port: int
    embedding_provider: str
    embedding_model: str
    embedding_version: str
    embedding_dimension: int

    # Harness 执行引擎
    max_turns_per_run: int
    retry_budget: int
    tool_timeout: int
    llm_timeout: int

    # LLM 全局参数
    llm_temperature: float
    llm_max_tokens: int
    llm_retry: LLMRetrySettings
    llm_secondary_retry: LLMRetrySettings

    # 记忆
    memory_ttl_days: int
    memory_min_score: float
    summary_threshold: int
    memory_sync_interval: int

    # 上下文压缩
    max_context_tokens: int
    compression_threshold: float
    keep_recent_turns: int
    max_summary_tokens: int

    # 审批
    approval_batch_mode: str
    approval_keyboard_shortcuts: bool


@router.get("")
async def get_settings_view() -> SettingsView:
    """返回启动时加载的系统配置（只读）. api_key 永不返回."""
    from athena.config.settings import get_settings
    s = get_settings()
    return SettingsView(
        host=s.host,
        port=s.port,
        debug=s.debug,
        postgres_user=s.postgres_user,
        postgres_db=s.postgres_db,
        postgres_host=s.postgres_host,
        postgres_port=s.postgres_port,
        embedding_provider=s.embedding_provider,
        embedding_model=s.embedding_model,
        embedding_version=s.embedding_version,
        embedding_dimension=s.embedding_dimension,
        max_turns_per_run=s.max_turns_per_run,
        retry_budget=s.retry_budget,
        tool_timeout=s.tool_timeout,
        llm_timeout=s.llm_timeout,
        llm_temperature=s.llm_temperature,
        llm_max_tokens=s.llm_max_tokens,
        llm_retry=s.llm_retry,
        llm_secondary_retry=s.llm_secondary_retry,
        memory_ttl_days=s.memory_ttl_days,
        memory_min_score=s.memory_min_score,
        summary_threshold=s.summary_threshold,
        memory_sync_interval=s.memory_sync_interval,
        max_context_tokens=s.max_context_tokens,
        compression_threshold=s.compression_threshold,
        keep_recent_turns=s.keep_recent_turns,
        max_summary_tokens=s.max_summary_tokens,
        approval_batch_mode=s.approval_batch_mode,
        approval_keyboard_shortcuts=s.approval_keyboard_shortcuts,
    )
