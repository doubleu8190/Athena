"""模型版本安全投影到 HTTP DTO 的转换。"""

from collections.abc import Sequence

from api.dto.llm import (
    CompressionPolicyResponse,
    ModelProfileResponse,
    UserModelCatalogItemResponse,
    UserModelCatalogResponse,
)
from domain.llm.model.compression_policy import CompressionPolicyVersion
from domain.llm.model.profile import ModelProfileVersion


def model_profile_to_response(value: ModelProfileVersion) -> ModelProfileResponse:
    """转换不含密钥明文的模型版本。"""
    return ModelProfileResponse(
        id=value.id,
        name=value.name,
        protocol=value.protocol,
        base_url=value.base_url,
        model=value.model,
        has_api_key=value.has_api_key,
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
        enabled=value.enabled,
        visible_to_users=value.visible_to_users,
        scenario_label=value.scenario_label,
        scenario_description=value.scenario_description,
        supports_vision=value.supports_vision,
        created_at=value.created_at,
    )


def user_model_catalog_to_response(
    profiles: Sequence[ModelProfileVersion],
    default_model_id: str | None,
) -> UserModelCatalogResponse:
    """将用户侧可见模型列表投影为不含运维参数的安全目录响应。"""
    return UserModelCatalogResponse(
        items=[
            UserModelCatalogItemResponse(
                id=item.id,
                name=item.name,
                scenario_label=item.scenario_label,
                scenario_description=item.scenario_description,
                is_default=(item.id == default_model_id),
                supports_vision=item.supports_vision,
            )
            for item in profiles
        ],
        default_model_id=default_model_id,
    )


def compression_policy_to_response(
    value: CompressionPolicyVersion | None,
) -> CompressionPolicyResponse:
    """转换当前压缩策略，不暴露凭据或摘要正文。"""
    return CompressionPolicyResponse(
        id=None if value is None else value.id,
        compression_model_profile_id=(
            None if value is None else value.compression_model_profile_id
        ),
        trigger_tokens=None if value is None else value.trigger_tokens,
        recent_ratio=None if value is None else value.recent_ratio,
        recent_token_cap=None if value is None else value.recent_token_cap,
        summary_ratio=None if value is None else value.summary_ratio,
        summary_token_cap=None if value is None else value.summary_token_cap,
        prompt_template_version=None if value is None else value.prompt_template_version,
        created_at=None if value is None else value.created_at,
    )


__all__ = [
    "compression_policy_to_response",
    "model_profile_to_response",
    "user_model_catalog_to_response",
]
