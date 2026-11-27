"""管理员模型配置版本用例。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from common.date import utc_now
from common.unset import UNSET
from domain.auth import AuthContext, CurrentSessionReader, UserRole
from domain.llm.model.compression_policy import (
    MAX_COMPRESSION_TOKEN_BUDGET,
    CompressionPolicyInput,
    CompressionPolicyVersion,
)
from domain.llm.model.profile import (
    ModelProfileInput,
    ModelProfileUpdateInput,
    ModelProfileVersion,
    validate_model_profile_api_key,
    validate_model_profile_fields,
)
from domain.llm.model.profile_errors import ModelProfileAuthorizationError, ModelProfileError
from domain.llm.profile_ports import ModelProfileRepository


class ModelProfileService:
    """校验管理员输入并管理模型配置版本、局部编辑、软删除和默认指向。"""

    def __init__(
        self,
        repository: ModelProfileRepository,
        auth_reader: CurrentSessionReader,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """保存版本仓储、当前会话读取能力和时钟。"""
        self._repository = repository
        self._auth_reader = auth_reader
        self._clock = clock

    async def create(self, actor: AuthContext, value: ModelProfileInput) -> ModelProfileVersion:
        """验证配置并创建模型配置版本。"""
        await self._authorize(actor)
        normalized = _validate_and_normalize_input(value)
        validate_model_profile_api_key(normalized.api_key)
        return await self._repository.create(actor, normalized)

    async def list(self, actor: AuthContext) -> tuple[ModelProfileVersion, ...]:
        """读取管理员可见的安全配置投影。"""
        await self._authorize(actor)
        return await self._repository.list(actor)

    async def list_with_default(
        self, actor: AuthContext
    ) -> tuple[tuple[ModelProfileVersion, ...], str | None]:
        """单次查询读取管理员可见的安全配置投影与当前默认版本标识。"""
        await self._authorize(actor)
        return await self._repository.list_with_default(actor)

    async def list_user_catalog(
        self,
    ) -> tuple[tuple[ModelProfileVersion, ...], str | None]:
        """面向已登录普通用户返回启用、可见且未软删除的场景化模型目录与默认模型标识。"""
        return await self._repository.list_user_visible_with_default()

    async def get_default_id(self, actor: AuthContext) -> str | None:
        """读取当前默认配置版本标识。"""
        await self._authorize(actor)
        return await self._repository.get_default_id(actor)

    async def get_compression_policy(self, actor: AuthContext) -> CompressionPolicyVersion | None:
        """读取管理员可见的当前上下文压缩策略。"""
        await self._authorize(actor)
        return await self._repository.get_current_compression_policy()

    async def set_compression_policy(
        self, actor: AuthContext, value: CompressionPolicyInput
    ) -> CompressionPolicyVersion:
        """验证管理员身份并创建新的压缩策略版本。"""
        await self._authorize(actor)
        _validate_compression_policy(value)
        try:
            return await self._repository.set_compression_policy(actor, value)
        except ValueError as error:
            raise ModelProfileError("llm_compression_policy_invalid") from error

    async def set_enabled(
        self, actor: AuthContext, profile_id: str, enabled: bool
    ) -> ModelProfileVersion:
        """启用或停用指定模型版本。"""
        await self._authorize(actor)
        return await self._repository.set_enabled(actor, profile_id, enabled)

    async def update(
        self, actor: AuthContext, profile_id: str, patch: ModelProfileUpdateInput
    ) -> ModelProfileVersion:
        """复核管理员权限后局部更新模型配置；不传 api_key 或传 None 时保留原加密凭据。"""
        await self._authorize(actor)
        if not profile_id or len(profile_id) > 64:
            raise ModelProfileError()
        if not patch.has_updates():
            raise ModelProfileError()
        if patch.expected_version is not None and (
            type(patch.expected_version) is not int or patch.expected_version < 1
        ):
            raise ModelProfileError()
        if patch.api_key is not UNSET and patch.api_key is not None:
            validate_model_profile_api_key(patch.api_key)
        return await self._repository.update(actor, profile_id, patch)

    async def delete(self, actor: AuthContext, profile_id: str) -> None:
        """复核管理员权限后软删除闲置模型配置，受默认/压缩策略/子智能体强引用保护。"""
        await self._authorize(actor)
        if not profile_id or len(profile_id) > 64:
            raise ModelProfileError()
        await self._repository.delete(actor, profile_id)

    async def set_default(self, actor: AuthContext, profile_id: str) -> ModelProfileVersion:
        """将默认配置指向支持流式和工具调用的版本。"""
        await self._authorize(actor)
        return await self._repository.set_default(actor, profile_id)

    async def _authorize(self, actor: AuthContext) -> None:
        user = await self._auth_reader.get_current_session_user(
            actor.user.id, actor.session_id, self._clock()
        )
        if user is None or user.role is not UserRole.SUPER_ADMIN:
            raise ModelProfileAuthorizationError()


def _validate_and_normalize_input(value: ModelProfileInput) -> ModelProfileInput:
    """校验限制地址、密钥引用、场景元数据和执行参数，并返回规范化后的输入对象。"""
    validated = validate_model_profile_fields(
        name=value.name,
        base_url=value.base_url,
        model=value.model,
        context_window_tokens=value.context_window_tokens,
        tokenizer_encoding=value.tokenizer_encoding,
        supports_stream=value.supports_stream,
        supports_tools=value.supports_tools,
        supports_parallel_tool_calls=value.supports_parallel_tool_calls,
        max_output_tokens=value.max_output_tokens,
        request_timeout_seconds=value.request_timeout_seconds,
        execution_timeout_seconds=value.execution_timeout_seconds,
        max_concurrency=value.max_concurrency,
        temperature=value.temperature,
        enabled=True,
        visible_to_users=value.visible_to_users,
        scenario_label=value.scenario_label,
        scenario_description=value.scenario_description,
        supports_vision=value.supports_vision,
        require_output_strictly_less_than_context=False,
    )
    return replace(
        value,
        scenario_label=validated.scenario_label,
        scenario_description=validated.scenario_description,
    )


def _validate_compression_policy(value: CompressionPolicyInput) -> None:
    """校验绝对阈值、Token 上限和两个仍由管理员配置的比例。"""
    if (
        not value.compression_model_profile_id
        or type(value.trigger_tokens) is not int
        or not 1 <= value.trigger_tokens <= MAX_COMPRESSION_TOKEN_BUDGET
        or type(value.recent_ratio) is not float
        or not 0 < value.recent_ratio < 1
        or type(value.recent_token_cap) is not int
        or not 1 <= value.recent_token_cap <= MAX_COMPRESSION_TOKEN_BUDGET
        or type(value.summary_ratio) is not float
        or not 0 < value.summary_ratio < 1
        or type(value.summary_token_cap) is not int
        or not 1 <= value.summary_token_cap <= MAX_COMPRESSION_TOKEN_BUDGET
        or value.recent_ratio + value.summary_ratio >= 1
    ):
        raise ModelProfileError("llm_compression_policy_invalid")


__all__ = ["ModelProfileService"]
