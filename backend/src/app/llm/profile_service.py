"""管理员模型配置版本用例。"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from urllib.parse import urlsplit

from common.ids import now_utc
from domain.auth import AuthContext, CurrentSessionReader, UserRole
from domain.llm.model.compression_policy import (
    MAX_COMPRESSION_TOKEN_BUDGET,
    CompressionPolicyInput,
    CompressionPolicyVersion,
)
from domain.llm.model.profile import (
    MAX_CONTEXT_WINDOW_TOKENS,
    MAX_OUTPUT_TOKENS,
    MAX_SCENARIO_DESCRIPTION_LENGTH,
    MAX_SCENARIO_LABEL_LENGTH,
    ModelProfileInput,
    ModelProfileVersion,
)
from domain.llm.model.profile_errors import ModelProfileAuthorizationError, ModelProfileError
from domain.llm.profile_ports import ModelProfileRepository
from domain.mcp import MAX_MCP_SECRET_BYTES


class ModelProfileService:
    """校验管理员输入并管理模型版本和默认指向。"""

    def __init__(
        self,
        repository: ModelProfileRepository,
        auth_reader: CurrentSessionReader,
        *,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        """保存版本仓储、当前会话读取能力和时钟。"""
        self._repository = repository
        self._auth_reader = auth_reader
        self._clock = clock

    async def create(self, actor: AuthContext, value: ModelProfileInput) -> ModelProfileVersion:
        """验证配置并创建不可变版本。"""
        await self._authorize(actor)
        normalized = _validate_and_normalize_input(value)
        _validate_api_key(normalized.api_key)
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


def _normalize_optional_scenario_text(value: str | None, *, maximum: int) -> str | None:
    """校验场景标签与说明：拒绝全部 Cc 控制字符、超长，空白字符串归一为 None。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ModelProfileError()
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ModelProfileError()
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > maximum:
        raise ModelProfileError()
    return normalized


def _validate_and_normalize_input(value: ModelProfileInput) -> ModelProfileInput:
    """校验限制地址、密钥引用、场景元数据和执行参数，并返回规范化后的输入对象。"""
    try:
        parsed = urlsplit(value.base_url)
        valid_url = (
            parsed.scheme == "https"
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and (parsed.port is None or 1 <= parsed.port <= 65535)
        )
    except ValueError:
        valid_url = False
    if (
        not value.name.strip()
        or len(value.name) > 120
        or not value.model.strip()
        or len(value.model) > 200
        or not valid_url
        or not 1 <= value.context_window_tokens <= MAX_CONTEXT_WINDOW_TOKENS
        or value.tokenizer_encoding not in {"cl100k_base", "o200k_base"}
        or type(value.supports_stream) is not bool
        or type(value.supports_tools) is not bool
        or type(value.supports_parallel_tool_calls) is not bool
        or type(value.visible_to_users) is not bool
        or type(value.supports_vision) is not bool
        or (
            value.supports_parallel_tool_calls
            and not value.supports_tools
        )
        or not 1 <= value.max_output_tokens <= min(
            value.context_window_tokens, MAX_OUTPUT_TOKENS
        )
        or not 1 <= value.request_timeout_seconds <= 3600
        or not 1 <= value.execution_timeout_seconds <= 3600
        or not 1 <= value.max_concurrency <= 128
        or (value.temperature is not None and not 0 <= value.temperature <= 2)
    ):
        raise ModelProfileError()
    normalized_label = _normalize_optional_scenario_text(
        value.scenario_label,
        maximum=MAX_SCENARIO_LABEL_LENGTH,
    )
    normalized_description = _normalize_optional_scenario_text(
        value.scenario_description,
        maximum=MAX_SCENARIO_DESCRIPTION_LENGTH,
    )
    return replace(
        value,
        scenario_label=normalized_label,
        scenario_description=normalized_description,
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


def _validate_api_key(value: str) -> None:
    """验证 API Key 可放入加密信封，避免过大或非法输入触发存储错误。"""
    if not value.strip() or "\x00" in value:
        raise ModelProfileError("llm_model_credential_invalid")
    try:
        serialized = json.dumps(
            {"api_key": value},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except UnicodeError:
        raise ModelProfileError("llm_model_credential_invalid") from None
    if len(serialized) > MAX_MCP_SECRET_BYTES:
        raise ModelProfileError("llm_model_credential_invalid")


__all__ = ["ModelProfileService"]
