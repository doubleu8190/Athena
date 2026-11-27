"""管理员 Skill 管理与普通用户目录 HTTP 接口。"""

from __future__ import annotations

import json
from typing import Annotated, Literal, NoReturn

from fastapi import (
    APIRouter,
    File,
    Form,
    Header,
    HTTPException,
    Path,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import Response
from pydantic import TypeAdapter, ValidationError

from api.converter.skill import (
    admin_details_to_response,
    admin_list_to_response,
    directory_to_response,
    execution_to_response,
    personal_draft_to_response,
    personal_list_to_response,
    version_page_to_response,
    version_to_response,
)
from api.dto.access import VisibilityGrantRequest
from api.dto.skill import (
    AdminSkillMarkdownCreateRequest,
    AdminSkillMarkdownVersionRequest,
    SkillAdminDetailsResponse,
    SkillAdminListResponse,
    SkillConfigurationRequest,
    SkillDirectoryResponse,
    SkillExecutionResponse,
    SkillMarkdownParseResponse,
    SkillPersonalCreateRequest,
    SkillPersonalListResponse,
    SkillPersonalToggleRequest,
    SkillPersonalVersionRequest,
    SkillVersionListResponse,
    SkillVersionResponse,
)
from api.errors import DomainErrorRoute, domain_error_to_http
from api.security import AdminAuth, CurrentAuth, SkillManagerAuth
from api.upload import staged_upload
from app.skill import SkillService
from common.unset import UNSET, UnsetType
from domain.access import VisibilityGrantInput
from domain.skill import (
    MAX_PACKAGE_ZIP_BYTES,
    MAX_PERSONAL_MARKDOWN_BYTES,
    SkillPackageError,
    SkillScope,
)

_GRANTS_ADAPTER: TypeAdapter[list[VisibilityGrantRequest]] = TypeAdapter(
    list[VisibilityGrantRequest]
)


def _parse_form_visibility_grants(raw: str | None) -> tuple[VisibilityGrantInput, ...] | None:
    """将 multipart 表单中的 JSON 字符串解析为领域可见性授权元组。"""
    if raw is None or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
        items = _GRANTS_ADAPTER.validate_python(parsed)
    except (ValueError, ValidationError) as error:
        raise SkillPackageError("visibility_grants_invalid") from error
    return tuple(item.to_domain() for item in items)


def _raise_safe_value_error(error: ValueError, *, mode: Literal["personal", "package"]) -> NoReturn:
    """优先使用统一领域映射，其余裸 ValueError 仅返回白名单安全错误码。"""
    if (mapped := domain_error_to_http(error)) is not None:
        raise mapped from error
    if mode == "package":
        raise HTTPException(422, "skill_package_invalid") from error
    value = str(error)
    code = (
        value
        if value.replace("_", "").isalnum() and value.isascii()
        else "skill_personal_content_invalid"
    )
    raise HTTPException(422, code) from error


def _raise_storage_os_error(error: OSError) -> NoReturn:
    """优先映射继承自 OSError 的领域权限异常，其余文件落盘/读取故障转 503。"""
    if (mapped := domain_error_to_http(error)) is not None:
        raise mapped from error
    raise HTTPException(503, "file_storage_unavailable") from error


def build_skill_user_router(service: SkillService) -> APIRouter:
    """创建登录用户可见的启用 Skill 目录。"""
    router = APIRouter(
        prefix="/api/v1/skills",
        tags=["skills"],
        route_class=DomainErrorRoute,
    )

    @router.get("", response_model=SkillDirectoryResponse)
    async def list_skills(actor: CurrentAuth) -> SkillDirectoryResponse:
        """返回当前仍可用的启用目录，不暴露管理员版本正文。"""
        return directory_to_response(await service.user_directory(actor))

    return router


def build_skill_personal_router(service: SkillService) -> APIRouter:
    """创建普通登录用户自己的 Markdown Skill 管理接口。"""
    router = APIRouter(
        prefix="/api/v1/me",
        tags=["personal-skills"],
        route_class=DomainErrorRoute,
    )

    @router.get("/skills", response_model=SkillPersonalListResponse)
    async def list_personal_skills(actor: CurrentAuth) -> SkillPersonalListResponse:
        """返回本人未删除的个人 Skill，包含停用项。"""
        return personal_list_to_response(await service.list_personal(actor))

    @router.post(
        "/skills/parse",
        response_model=SkillMarkdownParseResponse,
    )
    async def parse_personal_markdown(
        _: CurrentAuth,
        files: Annotated[list[UploadFile], File(alias="file")],
    ) -> SkillMarkdownParseResponse:
        """解析单个 Markdown 文件并返回编辑表单，不创建 Skill。"""
        if len(files) != 1:
            raise HTTPException(422, "personal_skill_file_invalid")
        file = files[0]
        try:
            async with staged_upload(
                file,
                maximum_bytes=MAX_PERSONAL_MARKDOWN_BYTES,
                too_large_code="skill_markdown_too_large",
            ) as path:
                draft = await service.parse_personal_document(
                    file.filename or "",
                    path,
                )
                return personal_draft_to_response(draft)
        except ValueError as error:
            if (mapped := domain_error_to_http(error)) is not None:
                raise mapped from error
            code = (
                "skill_markdown_empty"
                if str(error) == "upload_empty"
                else "personal_skill_file_invalid"
            )
            raise HTTPException(422, code) from error
        except OSError as error:
            _raise_storage_os_error(error)

    @router.post(
        "/skills",
        response_model=SkillAdminDetailsResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_personal_skill(
        request: SkillPersonalCreateRequest,
        actor: CurrentAuth,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=128),
        ],
    ) -> SkillAdminDetailsResponse:
        """创建并启用当前用户的首个个人 Skill。"""
        try:
            details = await service.create_personal(
                actor,
                name=request.name,
                description=request.description,
                guide=request.guide,
                idempotency_key=idempotency_key,
            )
            return admin_details_to_response(details)
        except ValueError as error:
            _raise_safe_value_error(error, mode="personal")

    @router.get("/skills/{skill_id}", response_model=SkillAdminDetailsResponse)
    async def get_personal_skill(
        actor: CurrentAuth,
        skill_id: Annotated[str, Path(min_length=1, max_length=128)],
    ) -> SkillAdminDetailsResponse:
        """返回本人个人 Skill 及当前版本正文。"""
        return admin_details_to_response(await service.get_personal(actor, skill_id))

    @router.post(
        "/skills/{skill_id}/versions",
        response_model=SkillAdminDetailsResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def publish_personal_version(
        request: SkillPersonalVersionRequest,
        actor: CurrentAuth,
        skill_id: Annotated[str, Path(min_length=1, max_length=128)],
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=128),
        ],
    ) -> SkillAdminDetailsResponse:
        """保存新版本并立即切换为当前启用版本。"""
        try:
            details = await service.publish_personal(
                actor,
                skill_id,
                name=request.name,
                description=request.description,
                guide=request.guide,
                expected_revision=request.expected_revision,
                idempotency_key=idempotency_key,
            )
            return admin_details_to_response(details)
        except ValueError as error:
            _raise_safe_value_error(error, mode="personal")

    @router.patch("/skills/{skill_id}", response_model=SkillAdminDetailsResponse)
    async def set_personal_skill_enabled(
        request: SkillPersonalToggleRequest,
        actor: CurrentAuth,
        skill_id: Annotated[str, Path(min_length=1, max_length=128)],
    ) -> SkillAdminDetailsResponse:
        """按修订号启停本人 Skill。"""
        try:
            details = await service.set_personal_enabled(
                actor,
                skill_id,
                enabled=request.enabled,
                expected_revision=request.expected_revision,
            )
            return admin_details_to_response(details)
        except ValueError as error:
            _raise_safe_value_error(error, mode="package")

    @router.delete(
        "/skills/{skill_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def delete_personal_skill(
        actor: CurrentAuth,
        skill_id: Annotated[str, Path(min_length=1, max_length=128)],
        expected_revision: Annotated[int, Query(ge=1)],
    ) -> Response:
        """软删除本人 Skill，已有 Run 快照和历史版本不受影响。"""
        try:
            await service.delete_personal(
                actor,
                skill_id,
                expected_revision=expected_revision,
            )
        except ValueError as error:
            _raise_safe_value_error(error, mode="package")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


def build_skill_admin_router(service: SkillService) -> APIRouter:
    """创建面向超管与部门管理员分权管理的 Skill 包与版本接口。"""
    router = APIRouter(
        prefix="/api/v1/admin",
        tags=["skill-admin"],
        route_class=DomainErrorRoute,
    )

    @router.get("/skills", response_model=SkillAdminListResponse)
    async def list_skills(actor: SkillManagerAuth) -> SkillAdminListResponse:
        """读取当前管理员可维护的公司级或管辖部门级 Skill。"""
        return admin_list_to_response(await service.list_admin(actor))

    @router.post(
        "/skills/import",
        response_model=SkillAdminDetailsResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def import_skill(
        actor: SkillManagerAuth,
        package: Annotated[UploadFile, File()],
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=128),
        ],
        scope: Annotated[Literal["company", "department"], Form()] = "company",
        owner_department_id: Annotated[str | None, Form()] = None,
        visibility_grants: Annotated[str | None, Form()] = None,
    ) -> SkillAdminDetailsResponse:
        """首发导入完整 ZIP；支持 company 与 department 级三层分权导入。"""
        try:
            parsed_grants = _parse_form_visibility_grants(visibility_grants)
            async with staged_upload(
                package,
                maximum_bytes=MAX_PACKAGE_ZIP_BYTES,
                too_large_code="skill_zip_too_large",
            ) as path:
                details = await service.import_package(
                    actor,
                    path,
                    idempotency_key=idempotency_key,
                    scope=SkillScope(scope),
                    owner_department_id=owner_department_id,
                    visibility_grants=parsed_grants,
                )
                return admin_details_to_response(details)
        except ValueError as error:
            _raise_safe_value_error(error, mode="package")
        except OSError as error:
            _raise_storage_os_error(error)

    @router.post(
        "/skills/parse-markdown",
        response_model=SkillMarkdownParseResponse,
    )
    async def parse_admin_markdown(
        actor: SkillManagerAuth,
        files: Annotated[list[UploadFile], File(alias="file")],
    ) -> SkillMarkdownParseResponse:
        """管理端解析单个 Markdown 文件并返回草稿，不落库且不创建版本。"""
        if len(files) != 1:
            raise HTTPException(422, "skill_markdown_file_invalid")
        file = files[0]
        try:
            async with staged_upload(
                file,
                maximum_bytes=MAX_PERSONAL_MARKDOWN_BYTES,
                too_large_code="skill_markdown_too_large",
            ) as path:
                draft = await service.parse_admin_markdown(
                    actor,
                    file.filename or "",
                    path,
                )
                return personal_draft_to_response(draft)
        except ValueError as error:
            if (mapped := domain_error_to_http(error)) is not None:
                raise mapped from error
            code = (
                "skill_markdown_empty"
                if str(error) == "upload_empty"
                else "skill_markdown_file_invalid"
            )
            raise HTTPException(422, code) from error
        except OSError as error:
            _raise_storage_os_error(error)

    @router.post(
        "/skills/markdown",
        response_model=SkillAdminDetailsResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_admin_markdown_skill(
        request: AdminSkillMarkdownCreateRequest,
        actor: SkillManagerAuth,
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=128),
        ],
    ) -> SkillAdminDetailsResponse:
        """管理端在线创建公司级或部门级纯 Markdown 技能，支持幂等与三层分权。"""
        try:
            grants = (
                None
                if request.visibility_grants is None
                else tuple(item.to_domain() for item in request.visibility_grants)
            )
            details = await service.create_admin_markdown(
                actor,
                name=request.name,
                description=request.description,
                guide=request.guide,
                idempotency_key=idempotency_key,
                scope=SkillScope(request.scope),
                owner_department_id=request.owner_department_id,
                visibility_grants=grants,
                slug=request.slug,
                enabled=request.enabled,
                featured=request.featured,
                starter_prompt=request.starter_prompt,
            )
            return admin_details_to_response(details)
        except ValueError as error:
            _raise_safe_value_error(error, mode="personal")
        except OSError as error:
            _raise_storage_os_error(error)

    @router.get("/skills/{skill_id}", response_model=SkillAdminDetailsResponse)
    async def get_skill(
        actor: SkillManagerAuth,
        skill_id: Annotated[str, Path(min_length=1, max_length=128)],
    ) -> SkillAdminDetailsResponse:
        """读取管理详情、完整指南、资源清单与最近 100 个版本。"""
        return admin_details_to_response(await service.get_admin(actor, skill_id))

    @router.post(
        "/skills/{skill_id}/versions",
        response_model=SkillVersionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def add_version(
        actor: SkillManagerAuth,
        skill_id: Annotated[str, Path(min_length=1, max_length=128)],
        package: Annotated[UploadFile, File()],
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=128),
        ],
        label: Annotated[str | None, Query(max_length=64)] = None,
    ) -> SkillVersionResponse:
        """上传完整更新版本，不自动替换当前指针。"""
        try:
            async with staged_upload(
                package,
                maximum_bytes=MAX_PACKAGE_ZIP_BYTES,
                too_large_code="skill_zip_too_large",
            ) as path:
                return version_to_response(
                    await service.add_version(
                        actor,
                        skill_id,
                        path,
                        idempotency_key=idempotency_key,
                        label=label,
                    )
                )
        except ValueError as error:
            _raise_safe_value_error(error, mode="package")
        except OSError as error:
            _raise_storage_os_error(error)

    @router.post(
        "/skills/{skill_id}/versions/markdown",
        response_model=SkillAdminDetailsResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def publish_admin_markdown_version(
        request: AdminSkillMarkdownVersionRequest,
        actor: SkillManagerAuth,
        skill_id: Annotated[str, Path(min_length=1, max_length=128)],
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=128),
        ],
    ) -> SkillAdminDetailsResponse:
        """管理端在线发布 Markdown 技能新版本；拒绝覆盖 ZIP 技能包并执行修订号乐观锁。"""
        try:
            details = await service.publish_admin_markdown_version(
                actor,
                skill_id,
                name=request.name,
                description=request.description,
                guide=request.guide,
                expected_revision=request.expected_revision,
                idempotency_key=idempotency_key,
                label=request.label,
                activate=request.activate,
            )
            return admin_details_to_response(details)
        except ValueError as error:
            _raise_safe_value_error(error, mode="personal")
        except OSError as error:
            _raise_storage_os_error(error)

    @router.get(
        "/skills/{skill_id}/versions",
        response_model=SkillVersionListResponse,
    )
    async def list_versions(
        actor: SkillManagerAuth,
        skill_id: Annotated[str, Path(min_length=1, max_length=128)],
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> SkillVersionListResponse:
        """分页读取不可变历史版本。"""
        value = await service.list_versions(
            actor,
            skill_id,
            page=page,
            page_size=page_size,
        )
        return version_page_to_response(
            value.items,
            total=value.total,
            page=page,
            page_size=page_size,
        )

    @router.get(
        "/skills/{skill_id}/versions/{version_id}",
        response_model=SkillVersionResponse,
    )
    async def get_version(
        actor: SkillManagerAuth,
        skill_id: Annotated[str, Path(min_length=1, max_length=128)],
        version_id: Annotated[str, Path(min_length=1, max_length=128)],
    ) -> SkillVersionResponse:
        """读取指定历史版本完整正文。"""
        return version_to_response(await service.get_version(actor, skill_id, version_id))

    @router.patch("/skills/{skill_id}", response_model=SkillAdminDetailsResponse)
    async def configure_skill(
        request: SkillConfigurationRequest,
        actor: SkillManagerAuth,
        skill_id: Annotated[str, Path(min_length=1, max_length=128)],
    ) -> SkillAdminDetailsResponse:
        """按配置修订号原子切换版本、启停、脚本审批、可见性授权及首页推荐元数据。"""
        # 借助 model_fields_set 精确区分“未传保持原值”与“显式 null 清空/非法”。
        featured_value: bool | UnsetType = UNSET
        if "featured" in request.model_fields_set:
            if request.featured is None:
                raise HTTPException(422, "skill_featured_invalid")
            featured_value = request.featured
        starter_prompt_value: str | None | UnsetType = (
            request.starter_prompt if "starter_prompt" in request.model_fields_set else UNSET
        )
        grants = (
            None
            if request.visibility_grants is None
            else tuple(item.to_domain() for item in request.visibility_grants)
        )
        await service.configure(
            actor,
            skill_id,
            expected_revision=request.expected_revision,
            enabled=request.enabled,
            current_version_id=request.current_version_id,
            script_requires_approval=request.script_requires_approval,
            visibility_grants=grants,
            featured=featured_value,
            starter_prompt=starter_prompt_value,
        )
        return admin_details_to_response(await service.get_admin(actor, skill_id))

    @router.delete(
        "/skills/{skill_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
    )
    async def delete_skill(
        actor: SkillManagerAuth,
        skill_id: Annotated[str, Path(min_length=1, max_length=128)],
        expected_revision: Annotated[int, Query(ge=1)],
    ) -> Response:
        """软删除部门级 Skill 并同事务清理关联的可见性授权。"""
        await service.delete_admin(
            actor,
            skill_id,
            expected_revision=expected_revision,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get(
        "/skill-executions/{call_id}",
        response_model=SkillExecutionResponse,
    )
    async def get_skill_execution(
        actor: AdminAuth,
        call_id: Annotated[str, Path(min_length=1, max_length=128)],
    ) -> SkillExecutionResponse:
        """读取脚本解释器、错误和日志可用性等排查摘要。"""
        return execution_to_response(await service.admin_execution(actor, call_id))

    @router.get(
        "/skill-executions/{call_id}/log",
        response_class=Response,
        responses={
            200: {
                "description": "Plain-text skill execution log",
                "content": {
                    "text/plain": {"schema": {"type": "string"}},
                },
            }
        },
    )
    async def get_skill_execution_log(
        actor: AdminAuth,
        call_id: Annotated[str, Path(min_length=1, max_length=128)],
    ) -> Response:
        """管理员下载最多 5 MiB 原始日志，普通用户路由不暴露正文。"""
        try:
            content = await service.admin_log(actor, call_id)
        except OSError as error:
            _raise_storage_os_error(error)
        return Response(
            content=content,
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": (f'attachment; filename="skill-execution-{call_id}.log"'),
                "X-Content-Type-Options": "nosniff",
            },
        )

    return router


__all__ = [
    "build_skill_admin_router",
    "build_skill_personal_router",
    "build_skill_user_router",
]
