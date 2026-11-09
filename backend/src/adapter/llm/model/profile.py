"""LLM 模型版本 ORM 映射。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from adapter.base import Base


class ModelProfileVersionModel(Base):
    """不可变连接设置和可单独停用的模型版本。"""

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
        CheckConstraint("max_output_tokens > 0", name="ck_llm_profile_output_tokens"),
        CheckConstraint("request_timeout_seconds > 0", name="ck_llm_profile_request_timeout"),
        CheckConstraint("execution_timeout_seconds > 0", name="ck_llm_profile_execution_timeout"),
        CheckConstraint("max_concurrency > 0", name="ck_llm_profile_concurrency"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    protocol: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    credential_env: Mapped[str] = mapped_column(String(128), nullable=False)
    context_window_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    tokenizer_encoding: Mapped[str] = mapped_column(String(32), nullable=False)
    supports_stream: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    supports_tools: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    max_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    request_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DefaultModelProfileModel(Base):
    """唯一当前默认模型版本指向。"""

    __tablename__ = "llm_default_model_profile"

    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[str] = mapped_column(
        ForeignKey("llm_model_profile_versions.id", ondelete="RESTRICT"), nullable=False
    )


class CompressionPolicyVersionModel(Base):
    """不可变的压缩模型和预算参数版本。"""

    __tablename__ = "llm_compression_policy_versions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    compression_model_profile_id: Mapped[str] = mapped_column(
        ForeignKey("llm_model_profile_versions.id", ondelete="RESTRICT"), nullable=False
    )
    trigger_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    recent_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    summary_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    prompt_template_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CurrentCompressionPolicyModel(Base):
    """唯一当前压缩策略指向。"""

    __tablename__ = "llm_current_compression_policy"

    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    policy_version_id: Mapped[str] = mapped_column(
        ForeignKey("llm_compression_policy_versions.id", ondelete="RESTRICT"), nullable=False
    )


__all__ = [
    "CompressionPolicyVersionModel",
    "CurrentCompressionPolicyModel",
    "DefaultModelProfileModel",
    "ModelProfileVersionModel",
]
