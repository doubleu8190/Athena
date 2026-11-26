"""管理员模型配置版本接口。"""

from __future__ import annotations

from typing import Annotated, Literal, cast

from fastapi import APIRouter, HTTPException, Path, Response, status

from api.converter.llm import (
    compression_policy_to_response,
    model_profile_to_response,
    user_model_catalog_to_response,
)
from api.dto.llm import (
    CompressionPolicyRequest,
    CompressionPolicyResponse,
    ModelProfileCreateRequest,
    ModelProfileDefaultRequest,
    ModelProfileEnabledRequest,
    ModelProfileListResponse,
    ModelProfileResponse,
    ModelProfileUpdateRequest,
    UserModelCatalogResponse,
)
from api.security import AdminAuth, CurrentAuth
from app.llm.profile_service import ModelProfileService
from domain.llm.model.compression_policy import CompressionPolicyInput
from domain.llm.model.profile import (
    MODEL_PROFILE_UNSET,
    ModelProfileInput,
    ModelProfileUpdateInput,
)
from domain.llm.model.profile_errors import (
    ModelProfileAuthorizationError,
    ModelProfileConflictError,
    ModelProfileError,
    ModelProfileNotFoundError,
)


def build_llm_admin_router(service: ModelProfileService) -> APIRouter:
    """绑定管理用例并创建管理员路由。"""
    profile_router = APIRouter(
        prefix="/api/v1/admin/llm/model-profiles",
        tags=["llm-admin"],
    )

    @profile_router.get("", response_model=ModelProfileListResponse)
    async def list_profiles(actor: AdminAuth) -> ModelProfileListResponse:
        """列出安全配置投影。"""
        try:
            profiles, default_profile_id = await service.list_with_default(actor)
        except ModelProfileAuthorizationError as error:
            raise _authorization_error(error) from error
        return ModelProfileListResponse(
            items=[model_profile_to_response(item) for item in profiles],
            default_profile_id=default_profile_id,
        )

    @profile_router.post(
        "", response_model=ModelProfileResponse, status_code=status.HTTP_201_CREATED
    )
    async def create_profile(
        request: ModelProfileCreateRequest,
        actor: AdminAuth,
    ) -> ModelProfileResponse:
        """创建不可变配置版本。"""
        try:
            item = await service.create(actor, ModelProfileInput(**request.model_dump()))
        except ModelProfileAuthorizationError as error:
            raise _authorization_error(error) from error
        except ModelProfileConflictError as error:
            raise HTTPException(409, error.code) from error
        except ModelProfileError as error:
            raise HTTPException(422, error.code) from error
        return model_profile_to_response(item)

    @profile_router.put("/{profile_id}/enabled", response_model=ModelProfileResponse)
    async def set_enabled(
        actor: AdminAuth,
        profile_id: Annotated[str, Path(min_length=1, max_length=64)],
        request: ModelProfileEnabledRequest,
    ) -> ModelProfileResponse:
        """切换版本是否允许新调用。"""
        try:
            item = await service.set_enabled(actor, profile_id, request.enabled)
        except ModelProfileAuthorizationError as error:
            raise _authorization_error(error) from error
        except ModelProfileNotFoundError as error:
            raise HTTPException(404, error.code) from error
        return model_profile_to_response(item)

    @profile_router.patch("/{profile_id}", response_model=ModelProfileResponse)
    async def update_profile(
        actor: AdminAuth,
        profile_id: Annotated[str, Path(min_length=1, max_length=64)],
        request: ModelProfileUpdateRequest,
    ) -> ModelProfileResponse:
        """局部更新模型配置；不传 api_key 或传 null 时保留原加密凭据。"""
        fields_set = request.model_fields_set
        updatable = fields_set - {"expected_version"}
        if not updatable:
            raise HTTPException(422, "llm_model_profile_invalid")
        # 严禁将不可空配置项显式置为 null
        # （temperature / scenario_label / scenario_description / api_key 除外）。
        non_nullable = {
            "name",
            "base_url",
            "model",
            "context_window_tokens",
            "tokenizer_encoding",
            "supports_stream",
            "supports_tools",
            "supports_parallel_tool_calls",
            "max_output_tokens",
            "request_timeout_seconds",
            "execution_timeout_seconds",
            "max_concurrency",
            "enabled",
            "visible_to_users",
            "supports_vision",
        }
        for field_name in updatable & non_nullable:
            if getattr(request, field_name) is None:
                raise HTTPException(422, "llm_model_profile_invalid")
        patch = ModelProfileUpdateInput(
            expected_version=request.expected_version,
            name=(MODEL_PROFILE_UNSET if "name" not in fields_set else cast(str, request.name)),
            base_url=(
                MODEL_PROFILE_UNSET if "base_url" not in fields_set else cast(str, request.base_url)
            ),
            model=(MODEL_PROFILE_UNSET if "model" not in fields_set else cast(str, request.model)),
            api_key=(MODEL_PROFILE_UNSET if "api_key" not in fields_set else request.api_key),
            context_window_tokens=(
                MODEL_PROFILE_UNSET
                if "context_window_tokens" not in fields_set
                else cast(int, request.context_window_tokens)
            ),
            tokenizer_encoding=(
                MODEL_PROFILE_UNSET
                if "tokenizer_encoding" not in fields_set
                else cast(Literal["cl100k_base", "o200k_base"], request.tokenizer_encoding)
            ),
            supports_stream=(
                MODEL_PROFILE_UNSET
                if "supports_stream" not in fields_set
                else cast(bool, request.supports_stream)
            ),
            supports_tools=(
                MODEL_PROFILE_UNSET
                if "supports_tools" not in fields_set
                else cast(bool, request.supports_tools)
            ),
            supports_parallel_tool_calls=(
                MODEL_PROFILE_UNSET
                if "supports_parallel_tool_calls" not in fields_set
                else cast(bool, request.supports_parallel_tool_calls)
            ),
            max_output_tokens=(
                MODEL_PROFILE_UNSET
                if "max_output_tokens" not in fields_set
                else cast(int, request.max_output_tokens)
            ),
            request_timeout_seconds=(
                MODEL_PROFILE_UNSET
                if "request_timeout_seconds" not in fields_set
                else cast(int, request.request_timeout_seconds)
            ),
            execution_timeout_seconds=(
                MODEL_PROFILE_UNSET
                if "execution_timeout_seconds" not in fields_set
                else cast(int, request.execution_timeout_seconds)
            ),
            max_concurrency=(
                MODEL_PROFILE_UNSET
                if "max_concurrency" not in fields_set
                else cast(int, request.max_concurrency)
            ),
            temperature=(
                MODEL_PROFILE_UNSET if "temperature" not in fields_set else request.temperature
            ),
            enabled=(
                MODEL_PROFILE_UNSET if "enabled" not in fields_set else cast(bool, request.enabled)
            ),
            visible_to_users=(
                MODEL_PROFILE_UNSET
                if "visible_to_users" not in fields_set
                else cast(bool, request.visible_to_users)
            ),
            scenario_label=(
                MODEL_PROFILE_UNSET
                if "scenario_label" not in fields_set
                else request.scenario_label
            ),
            scenario_description=(
                MODEL_PROFILE_UNSET
                if "scenario_description" not in fields_set
                else request.scenario_description
            ),
            supports_vision=(
                MODEL_PROFILE_UNSET
                if "supports_vision" not in fields_set
                else cast(bool, request.supports_vision)
            ),
        )
        try:
            item = await service.update(actor, profile_id, patch)
        except ModelProfileAuthorizationError as error:
            raise _authorization_error(error) from error
        except ModelProfileNotFoundError as error:
            raise HTTPException(404, error.code) from error
        except ModelProfileConflictError as error:
            raise HTTPException(409, error.code) from error
        except ModelProfileError as error:
            raise HTTPException(422, error.code) from error
        return model_profile_to_response(item)

    @profile_router.delete(
        "/{profile_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def delete_profile(
        actor: AdminAuth,
        profile_id: Annotated[str, Path(min_length=1, max_length=64)],
    ) -> Response:
        """软删除未被默认指向、压缩策略或活跃子智能体强引用的模型配置。"""
        try:
            await service.delete(actor, profile_id)
        except ModelProfileAuthorizationError as error:
            raise _authorization_error(error) from error
        except ModelProfileNotFoundError as error:
            raise HTTPException(404, error.code) from error
        except ModelProfileConflictError as error:
            raise HTTPException(409, error.code) from error
        except ModelProfileError as error:
            raise HTTPException(422, error.code) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @profile_router.put("/default", response_model=ModelProfileResponse)
    async def set_default(
        request: ModelProfileDefaultRequest,
        actor: AdminAuth,
    ) -> ModelProfileResponse:
        """将默认模型指向指定的已启用版本。"""
        try:
            item = await service.set_default(actor, request.profile_id)
        except ModelProfileAuthorizationError as error:
            raise _authorization_error(error) from error
        except ModelProfileNotFoundError as error:
            raise HTTPException(404, error.code) from error
        return model_profile_to_response(item)

    policy_router = APIRouter(
        prefix="/api/v1/admin/llm/compression-policy",
        tags=["llm-admin"],
    )

    @policy_router.get("")
    async def get_compression_policy(actor: AdminAuth) -> CompressionPolicyResponse:
        """读取当前压缩策略；未配置时各字段为空。"""
        try:
            value = await service.get_compression_policy(actor)
        except ModelProfileAuthorizationError as error:
            raise _authorization_error(error) from error
        return compression_policy_to_response(value)

    @policy_router.put("")
    async def set_compression_policy(
        request: CompressionPolicyRequest, actor: AdminAuth
    ) -> CompressionPolicyResponse:
        """创建并启用新的不可变压缩策略版本。"""
        try:
            value = await service.set_compression_policy(
                actor,
                CompressionPolicyInput(
                    compression_model_profile_id=request.compression_model_profile_id,
                    trigger_tokens=request.trigger_tokens,
                    recent_ratio=request.recent_ratio,
                    recent_token_cap=request.recent_token_cap,
                    summary_ratio=request.summary_ratio,
                    summary_token_cap=request.summary_token_cap,
                ),
            )
        except ModelProfileAuthorizationError as error:
            raise _authorization_error(error) from error
        except ModelProfileNotFoundError as error:
            raise HTTPException(404, error.code) from error
        except ModelProfileError as error:
            raise HTTPException(422, error.code) from error
        return compression_policy_to_response(value)

    router = APIRouter()
    router.include_router(profile_router)
    router.include_router(policy_router)
    return router


def build_llm_user_router(service: ModelProfileService) -> APIRouter:
    """创建面向已登录普通用户的场景化只读模型目录路由。"""
    router = APIRouter(
        prefix="/api/v1/models",
        tags=["models"],
    )

    @router.get("", response_model=UserModelCatalogResponse)
    async def list_user_models(current_auth: CurrentAuth) -> UserModelCatalogResponse:
        """返回对普通用户可见、已启用且未软删除的安全模型目录。"""
        del current_auth
        profiles, default_model_id = await service.list_user_catalog()
        return user_model_catalog_to_response(profiles, default_model_id)

    return router


def _authorization_error(error: ModelProfileAuthorizationError) -> HTTPException:
    """将最终身份复核失败映射为统一禁止响应。"""
    return HTTPException(status_code=403, detail="admin_required")


__all__ = ["build_llm_admin_router", "build_llm_user_router"]
