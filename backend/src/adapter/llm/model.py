"""LLM 模型版本 ORM 映射。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from adapter.base import Base


class ModelProfileVersionModel(Base):
    """支持局部编辑、乐观锁与软删除的 LLM 模型配置表。"""

    __tablename__ = "llm_model_profile_versions"
    __table_args__ = (
        CheckConstraint("protocol = 'chat_completions'", name="ck_llm_profile_protocol"),
        CheckConstraint("context_window_tokens > 0", name="ck_llm_profile_context_window"),
        CheckConstraint(
            "tokenizer_encoding IN ('cl100k_base', 'o200k_base')",
            name="ck_llm_profile_tokenizer_encoding",
        ),
        CheckConstraint("supports_stream IN (true, false)", name="ck_llm_profile_supports_stream"),
        CheckConstraint("supports_tools IN (true, false)", name="ck_llm_profile_supports_tools"),
        CheckConstraint(
            "supports_parallel_tool_calls IN (true, false)",
            name="ck_llm_profile_supports_parallel_tool_calls",
        ),
        CheckConstraint("max_output_tokens > 0", name="ck_llm_profile_output_tokens"),
        CheckConstraint("request_timeout_seconds > 0", name="ck_llm_profile_request_timeout"),
        CheckConstraint("execution_timeout_seconds > 0", name="ck_llm_profile_execution_timeout"),
        CheckConstraint("max_concurrency > 0", name="ck_llm_profile_concurrency"),
        CheckConstraint("version >= 1", name="ck_llm_profile_version"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    protocol: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    # 全部模型配置均须持久化加密凭据三元组，数据库层收紧为非空。
    api_key_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    api_key_nonce: Mapped[bytes] = mapped_column(LargeBinary(12), nullable=False)
    api_key_key_version: Mapped[int] = mapped_column(Integer, nullable=False)
    context_window_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    tokenizer_encoding: Mapped[str] = mapped_column(String(32), nullable=False)
    supports_stream: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    supports_tools: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    supports_parallel_tool_calls: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    max_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    request_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    # 面向普通用户的场景化模型目录展示属性及软删除列。
    visible_to_users: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    scenario_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scenario_description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    supports_vision: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 乐观锁版本号与最近更新时间，支撑局部编辑与并发防覆盖。
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DefaultModelProfileModel(Base):
    """唯一当前默认模型版本指向。"""

    __tablename__ = "llm_default_model_profile"
    __table_args__ = (CheckConstraint("singleton_id = 1", name="ck_llm_default_singleton"),)

    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("llm_model_profile_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )


class CompressionPolicyVersionModel(Base):
    """不可变的压缩模型和预算参数版本。"""

    __tablename__ = "llm_compression_policy_versions"
    __table_args__ = (
        CheckConstraint("trigger_tokens > 0", name="ck_compression_trigger_tokens"),
        CheckConstraint("recent_ratio > 0 AND recent_ratio < 1", name="ck_compression_recent"),
        CheckConstraint("recent_token_cap > 0", name="ck_compression_recent_token_cap"),
        CheckConstraint("summary_ratio > 0 AND summary_ratio < 1", name="ck_compression_summary"),
        CheckConstraint("summary_token_cap > 0", name="ck_compression_summary_token_cap"),
        ForeignKeyConstraint(
            ["compression_model_profile_id"],
            ["llm_model_profile_versions.id"],
            name="fk_compression_policy_model",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    compression_model_profile_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    recent_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    recent_token_cap: Mapped[int] = mapped_column(BigInteger, nullable=False)
    summary_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    summary_token_cap: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CurrentCompressionPolicyModel(Base):
    """唯一当前压缩策略指向。"""

    __tablename__ = "llm_current_compression_policy"
    __table_args__ = (
        CheckConstraint("singleton_id = 1", name="ck_current_compression_policy_singleton"),
        ForeignKeyConstraint(
            ["policy_version_id"],
            ["llm_compression_policy_versions.id"],
            name="fk_current_compression_policy_version",
            ondelete="RESTRICT",
        ),
    )

    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    policy_version_id: Mapped[str] = mapped_column(String(64), nullable=False)


__all__ = [
    "CompressionPolicyVersionModel",
    "CurrentCompressionPolicyModel",
    "DefaultModelProfileModel",
    "ModelProfileVersionModel",
]
