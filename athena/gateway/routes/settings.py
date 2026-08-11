"""系统设置路由 — 只读展示启动时加载的配置."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/settings", tags=["settings"])


class SettingsView(BaseModel):
    """系统设置只读视图（绝不包含 api_key）."""

    # Server
    host: str
    port: int
    debug: bool

    # Database
    sqlite_db_path: str
    chromadb_path: str

    # Harness
    max_turns_per_run: int
    retry_budget: int
    tool_timeout: int
    llm_stream_timeout: int
    approval_timeout: int

    # LLM 全局参数
    llm_temperature: float
    llm_max_tokens: int

    # Memory
    memory_ttl_days: int
    memory_min_score: float
    summary_threshold: int
    memory_sync_interval: int

    # Context Compression
    max_context_tokens: int
    compression_threshold: float
    keep_recent_turns: int
    max_summary_tokens: int

    # Sandbox
    sandbox_enabled: bool
    sandbox_image: str
    sandbox_network_disabled: bool

    # Approval
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
        sqlite_db_path=s.sqlite_db_path,
        chromadb_path=s.chromadb_path,
        max_turns_per_run=s.max_turns_per_run,
        retry_budget=s.retry_budget,
        tool_timeout=s.tool_timeout,
        llm_stream_timeout=s.llm_stream_timeout,
        approval_timeout=s.approval_timeout,
        llm_temperature=s.llm_temperature,
        llm_max_tokens=s.llm_max_tokens,
        memory_ttl_days=s.memory_ttl_days,
        memory_min_score=s.memory_min_score,
        summary_threshold=s.summary_threshold,
        memory_sync_interval=s.memory_sync_interval,
        max_context_tokens=s.max_context_tokens,
        compression_threshold=s.compression_threshold,
        keep_recent_turns=s.keep_recent_turns,
        max_summary_tokens=s.max_summary_tokens,
        sandbox_enabled=s.sandbox_enabled,
        sandbox_image=s.sandbox_image,
        sandbox_network_disabled=s.sandbox_network_disabled,
        approval_batch_mode=s.approval_batch_mode,
        approval_keyboard_shortcuts=s.approval_keyboard_shortcuts,
    )
