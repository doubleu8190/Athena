"""管理员模型配置版本接口。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status

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
    UserModelCatalogResponse,
)
from api.security import AdminAuth, CurrentAuth, verify_request_origin
from app.llm.profile_service import ModelProfileService
from domain.llm.model.compression_policy import CompressionPolicyInput
from domain.llm.model.profile import ModelProfileInput
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
        dependencies=[Depends(verify_request_origin)],
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
        dependencies=[Depends(verify_request_origin)],
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
        dependencies=[Depends(verify_request_origin)],
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
