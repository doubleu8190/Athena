"""进程共享的集中式强类型配置（Pydantic v2）与严格环境变量解析。"""

from __future__ import annotations

import math
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator

from app.run.config import RunWorkerConfig, worker_config_from_environment

DEFAULT_DATABASE_POOL_SIZE_SETTING = 5
DEFAULT_DATABASE_MAX_OVERFLOW_SETTING = 10
DEFAULT_KNOWLEDGE_SEMANTIC_MIN_SCORE = 0.35
DEFAULT_KNOWLEDGE_OWNER_CONCURRENCY = 2
DEFAULT_KNOWLEDGE_WORKER_CONCURRENCY = 2
MAX_KNOWLEDGE_WORKER_CONCURRENCY = 16


def _source(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def is_production_env(environ: Mapping[str, str] | None = None) -> bool:
    """判断当前是否处于 ATHENA_ENV=production 生产运行环境。"""
    return (_source(environ).get("ATHENA_ENV") or "").strip().lower() == "production"


def env_flag(
    name: str,
    *,
    default: bool,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """解析严格布尔环境变量，拒绝含糊配置。"""
    value = _source(environ).get(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name}_invalid")


def env_int(
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int | None = None,
    error_code: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """解析带边界约束的环境整数，拒绝 bool 语义和含糊文本。"""
    code = error_code or f"{name}_invalid"
    value = _source(environ).get(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value.strip())
    except ValueError as error:
        raise ValueError(code) from error
    if parsed < minimum or (maximum is not None and parsed > maximum):
        raise ValueError(code)
    return parsed


def env_float(
    name: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
    error_code: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> float:
    """解析有界浮点环境变量，非法值、NaN/Inf 或越界时直接拒绝。"""
    code = error_code or f"{name.lower()}_invalid"
    raw = _source(environ).get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError as error:
        raise ValueError(code) from error
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(code)
    return value


def env_csv_set(
    name: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> frozenset[str]:
    """解析逗号分隔的环境变量名白名单，拒绝空项和非法变量名。"""
    raw = _source(environ).get(name, "")
    if not raw.strip():
        return frozenset()
    parts = [part.strip() for part in raw.split(",")]
    if any(not part for part in parts):
        raise ValueError(f"{name}_invalid")
    valid = all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) for part in parts)
    if not valid:
        raise ValueError(f"{name}_invalid")
    return frozenset(parts)


class DatabaseSettings(BaseModel):
    """数据库连接地址与有界连接池设置。"""

    model_config = ConfigDict(frozen=True)

    url: str | None = None
    pool_size: int = DEFAULT_DATABASE_POOL_SIZE_SETTING
    max_overflow: int = DEFAULT_DATABASE_MAX_OVERFLOW_SETTING

    @model_validator(mode="after")
    def _validate_bounds(self) -> DatabaseSettings:
        if self.pool_size < 1:
            raise ValueError("ATHENA_DATABASE_POOL_SIZE_invalid")
        if self.max_overflow < 0:
            raise ValueError("ATHENA_DATABASE_MAX_OVERFLOW_invalid")
        return self

    @classmethod
    def url_from_environment(cls, environ: Mapping[str, str] | None = None) -> str | None:
        """仅读取规范化后的 ATHENA_DATABASE_URL，避免在前置安全校验前解析连接池参数。"""
        raw = (_source(environ).get("ATHENA_DATABASE_URL") or "").strip()
        return raw or None

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> DatabaseSettings:
        source = _source(environ)
        return cls(
            url=cls.url_from_environment(source),
            pool_size=env_int(
                "ATHENA_DATABASE_POOL_SIZE",
                default=DEFAULT_DATABASE_POOL_SIZE_SETTING,
                minimum=1,
                environ=source,
            ),
            max_overflow=env_int(
                "ATHENA_DATABASE_MAX_OVERFLOW",
                default=DEFAULT_DATABASE_MAX_OVERFLOW_SETTING,
                minimum=0,
                environ=source,
            ),
        )


class RedisSettings(BaseModel):
    """共享 Redis 连接设置。"""

    model_config = ConfigDict(frozen=True)

    url: str | None = None

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> RedisSettings:
        raw = (_source(environ).get("ATHENA_REDIS_URL") or "").strip()
        return cls(url=raw or None)


class AuthSettings(BaseModel):
    """Cookie 安全、Origin 校验与登录模式开关。"""

    model_config = ConfigDict(frozen=True)

    cookie_secure: bool = False
    public_origin: str | None = None
    mock_enabled: bool = False
    feishu_enabled: bool = False

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> AuthSettings:
        source = _source(environ)
        origin = (source.get("ATHENA_PUBLIC_ORIGIN") or "").strip() or None
        return cls(
            cookie_secure=env_flag("ATHENA_COOKIE_SECURE", default=False, environ=source),
            public_origin=origin,
            mock_enabled=env_flag("ATHENA_AUTH_MOCK_ENABLED", default=False, environ=source),
            feishu_enabled=env_flag("ATHENA_FEISHU_ENABLED", default=False, environ=source),
        )


class McpSettings(BaseModel):
    """MCP stdio 宿主环境变量白名单设置。"""

    model_config = ConfigDict(frozen=True)

    stdio_base_env_names: frozenset[str] = frozenset()

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> McpSettings:
        source = _source(environ)
        return cls(
            stdio_base_env_names=env_csv_set(
                "ATHENA_MCP_STDIO_BASE_ENV_NAMES",
                environ=source,
            ),
        )

    def stdio_base_environment(
        self,
        environ: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """仅按白名单提取允许透传给 MCP stdio 子进程的宿主环境变量。"""
        source = _source(environ)
        return {name: source[name] for name in self.stdio_base_env_names if name in source}


class KnowledgeSettings(BaseModel):
    """知识库查询与后台解析 Worker 设置。"""

    model_config = ConfigDict(frozen=True)

    semantic_min_score: float = DEFAULT_KNOWLEDGE_SEMANTIC_MIN_SCORE
    ocr_cache_root: str | None = None
    owner_concurrency: int = DEFAULT_KNOWLEDGE_OWNER_CONCURRENCY
    worker_concurrency: int = DEFAULT_KNOWLEDGE_WORKER_CONCURRENCY

    @model_validator(mode="after")
    def _validate_bounds(self) -> KnowledgeSettings:
        if not math.isfinite(self.semantic_min_score) or not 0.0 <= self.semantic_min_score <= 1.0:
            raise ValueError("athena_knowledge_semantic_min_score_invalid")
        if self.owner_concurrency < 1:
            raise ValueError("ATHENA_KNOWLEDGE_OWNER_CONCURRENCY_invalid")
        if not 1 <= self.worker_concurrency <= MAX_KNOWLEDGE_WORKER_CONCURRENCY:
            raise ValueError("knowledge_worker_concurrency_invalid")
        return self

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> KnowledgeSettings:
        source = _source(environ)
        ocr_cache_root = (source.get("ATHENA_KNOWLEDGE_OCR_CACHE_ROOT") or "").strip() or None
        return cls(
            semantic_min_score=env_float(
                "ATHENA_KNOWLEDGE_SEMANTIC_MIN_SCORE",
                default=DEFAULT_KNOWLEDGE_SEMANTIC_MIN_SCORE,
                minimum=0.0,
                maximum=1.0,
                environ=source,
            ),
            ocr_cache_root=ocr_cache_root,
            owner_concurrency=env_int(
                "ATHENA_KNOWLEDGE_OWNER_CONCURRENCY",
                default=DEFAULT_KNOWLEDGE_OWNER_CONCURRENCY,
                minimum=1,
                environ=source,
            ),
            worker_concurrency=env_int(
                "ATHENA_KNOWLEDGE_WORKER_CONCURRENCY",
                default=DEFAULT_KNOWLEDGE_WORKER_CONCURRENCY,
                minimum=1,
                maximum=MAX_KNOWLEDGE_WORKER_CONCURRENCY,
                error_code="knowledge_worker_concurrency_invalid",
                environ=source,
            ),
        )


class FileSkillSettings(BaseModel):
    """技能脚本运行时路径设置。"""

    model_config = ConfigDict(frozen=True)

    python_executable: Path = Path(sys.executable)
    temp_root: Path = Path(sys.prefix) / "tmp" / "athena-skill-executions"

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> FileSkillSettings:
        source = _source(environ)
        python_executable = Path(source.get("ATHENA_SKILL_PYTHON_EXECUTABLE") or sys.executable)
        temp_root = Path(
            source.get("ATHENA_SKILL_TEMP_ROOT")
            or str(Path(sys.prefix) / "tmp" / "athena-skill-executions")
        )
        return cls(
            python_executable=python_executable,
            temp_root=temp_root,
        )


class RunWorkerSettings(BaseModel):
    """Run Worker 并发、租约、轮询与工具执行边界设置。"""

    model_config = ConfigDict(frozen=True)

    concurrency: int = 4
    lease_seconds: int = 30
    heartbeat_seconds: int = 5
    execution_seconds: int = 300
    cancel_poll_seconds: float = 1.0
    queue_poll_seconds: float = 1.0
    reclaim_seconds: float = 5.0
    llm_request_limit: int = 40
    tool_timeout_seconds: float = 30.0
    tool_argument_bytes: int = 64 * 1024
    tool_result_bytes: int = 64 * 1024
    tool_cleanup_seconds: float = 10.0
    tool_batch_limit: int = 8
    tool_concurrency: int = 4

    @model_validator(mode="after")
    def _validate_run_worker_invariants(self) -> RunWorkerSettings:
        # 复用领域侧 RunWorkerConfig 校验不变量，保证错误码 run_worker_config_invalid 单源一致。
        self.to_config()
        return self

    def to_config(self) -> RunWorkerConfig:
        """转换为应用层消费的不可变 RunWorkerConfig 值对象。"""
        return RunWorkerConfig(
            concurrency=self.concurrency,
            lease_seconds=self.lease_seconds,
            heartbeat_seconds=self.heartbeat_seconds,
            execution_seconds=self.execution_seconds,
            cancel_poll_seconds=self.cancel_poll_seconds,
            queue_poll_seconds=self.queue_poll_seconds,
            reclaim_seconds=self.reclaim_seconds,
            llm_request_limit=self.llm_request_limit,
            tool_timeout_seconds=self.tool_timeout_seconds,
            tool_argument_bytes=self.tool_argument_bytes,
            tool_result_bytes=self.tool_result_bytes,
            tool_cleanup_seconds=self.tool_cleanup_seconds,
            tool_batch_limit=self.tool_batch_limit,
            tool_concurrency=self.tool_concurrency,
        )

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> RunWorkerSettings:
        # 通过 app.run.config.worker_config_from_environment 单源解析并保留全部既有错误码。
        config = worker_config_from_environment(environ)
        return cls(
            concurrency=config.concurrency,
            lease_seconds=config.lease_seconds,
            heartbeat_seconds=config.heartbeat_seconds,
            execution_seconds=config.execution_seconds,
            cancel_poll_seconds=config.cancel_poll_seconds,
            queue_poll_seconds=config.queue_poll_seconds,
            reclaim_seconds=config.reclaim_seconds,
            llm_request_limit=config.llm_request_limit,
            tool_timeout_seconds=config.tool_timeout_seconds,
            tool_argument_bytes=config.tool_argument_bytes,
            tool_result_bytes=config.tool_result_bytes,
            tool_cleanup_seconds=config.tool_cleanup_seconds,
            tool_batch_limit=config.tool_batch_limit,
            tool_concurrency=config.tool_concurrency,
        )


class LoggingSettings(BaseModel):
    """进程日志级别设置（统一读取 ATHENA_LOG_LEVEL，默认 INFO）。"""

    model_config = ConfigDict(frozen=True)

    level: str = "INFO"

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> LoggingSettings:
        source = _source(environ)
        level = (source.get("ATHENA_LOG_LEVEL") or "").strip()
        return cls(level=(level or "INFO").upper())


__all__ = [
    "DEFAULT_DATABASE_MAX_OVERFLOW_SETTING",
    "DEFAULT_DATABASE_POOL_SIZE_SETTING",
    "DEFAULT_KNOWLEDGE_OWNER_CONCURRENCY",
    "DEFAULT_KNOWLEDGE_SEMANTIC_MIN_SCORE",
    "DEFAULT_KNOWLEDGE_WORKER_CONCURRENCY",
    "MAX_KNOWLEDGE_WORKER_CONCURRENCY",
    "AuthSettings",
    "DatabaseSettings",
    "FileSkillSettings",
    "KnowledgeSettings",
    "LoggingSettings",
    "McpSettings",
    "RedisSettings",
    "RunWorkerSettings",
    "env_csv_set",
    "env_flag",
    "env_float",
    "env_int",
    "is_production_env",
]
