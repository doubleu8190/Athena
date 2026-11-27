"""模型版本安全投影到 HTTP DTO 的转换。"""

from __future__ import annotations

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
    """转换不含密钥明文的模型版本（按 DTO 字段白名单投影）。"""
    return ModelProfileResponse.model_validate(value)


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
    """转换当前压缩策略，不暴露凭据或摘要正文；未配置时各字段显式为 None。"""
    if value is not None:
        return CompressionPolicyResponse.model_validate(value)
    return CompressionPolicyResponse(
        id=None,
        compression_model_profile_id=None,
        trigger_tokens=None,
        recent_ratio=None,
        recent_token_cap=None,
        summary_ratio=None,
        summary_token_cap=None,
        created_at=None,
    )


__all__ = [
    "compression_policy_to_response",
    "model_profile_to_response",
    "user_model_catalog_to_response",
]
