"""LLM 模型版本管理的持久化端口。"""

from __future__ import annotations

from typing import Protocol

from domain.auth import AuthContext
from domain.llm.model.compression_policy import CompressionPolicyInput, CompressionPolicyVersion
from domain.llm.model.profile import ModelProfileInput, ModelProfileVersion, RuntimeModelProfile


class ModelProfileRepository(Protocol):
    """管理员模型配置版本与默认指向持久化能力。"""

    async def create(self, actor: AuthContext, value: ModelProfileInput) -> ModelProfileVersion: ...

    async def list(self, actor: AuthContext) -> tuple[ModelProfileVersion, ...]: ...

    async def set_enabled(
        self, actor: AuthContext, profile_id: str, enabled: bool
    ) -> ModelProfileVersion: ...

    async def set_default(self, actor: AuthContext, profile_id: str) -> ModelProfileVersion: ...

    async def get_default_runtime(self) -> RuntimeModelProfile | None: ...

    async def get_default_id(self, actor: AuthContext) -> str | None: ...

    async def get_runtime(self, profile_id: str) -> RuntimeModelProfile | None: ...


class CompressionPolicyRepository(Protocol):
    """管理员配置和读取不可变压缩策略。"""

    async def get_current_compression_policy(self) -> CompressionPolicyVersion | None: ...

    async def get_compression_policy(self, policy_id: str) -> CompressionPolicyVersion | None: ...

    async def set_compression_policy(
        self, actor: AuthContext, value: CompressionPolicyInput
    ) -> CompressionPolicyVersion: ...


__all__ = ["CompressionPolicyRepository", "ModelProfileRepository"]
