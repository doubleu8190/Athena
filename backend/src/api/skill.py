"""管理员 Skill 管理与普通用户目录 HTTP 接口。"""

from __future__ import annotations

from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Path,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import Response

from adapter.file.storage import FileStorageError
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
from api.dto.skill import (
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
from api.security import AdminAuth, CurrentAuth, verify_request_origin
from api.upload import UploadTooLargeError, staged_upload
from app.skill import SkillService
from app.skill.personal import PersonalSkillError
from app.skill.service import SkillAuthorizationError, SkillUserAuthorizationError
from domain.skill import (
    MAX_PACKAGE_ZIP_BYTES,
    MAX_PERSONAL_MARKDOWN_BYTES,
    SkillConflictError,
    SkillDependencyUnsatisfiedError,
    SkillNotFoundError,
    SkillPackageError,
)


def build_skill_user_router(service: SkillService) -> APIRouter:
    """创建登录用户可见的启用 Skill 目录。"""
    router = APIRouter(prefix="/api/v1/skills", tags=["skills"])

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
        dependencies=[Depends(verify_request_origin)],
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
            ) as path:
                draft = await service.parse_personal_document(
                    file.filename or "",
                    path,
                )
                return personal_draft_to_response(draft)
        except UploadTooLargeError as error:
            raise HTTPException(413, "skill_markdown_too_large") from error
        except PersonalSkillError as error:
            raise _personal_validation_error(error) from error
        except ValueError as error:
            code = "skill_markdown_empty" if str(error) == "upload_empty" else (
                "personal_skill_file_invalid"
            )
            raise HTTPException(422, code) from error
        except OSError as error:
            raise HTTPException(503, "file_storage_unavailable") from error

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
        except SkillUserAuthorizationError as error:
            raise _user_authorization_error(error) from error
        except SkillNotFoundError as error:
            raise HTTPException(404, error.code) from error
        except SkillConflictError as error:
            raise HTTPException(409, error.code) from error
        except (PersonalSkillError, ValueError) as error:
            raise _personal_validation_error(error) from error
        except FileStorageError as error:
            raise HTTPException(503, "file_storage_unavailable") from error

    @router.get("/skills/{skill_id}", response_model=SkillAdminDetailsResponse)
    async def get_personal_skill(
        actor: CurrentAuth,
        skill_id: Annotated[str, Path(min_length=1, max_length=128)],
    ) -> SkillAdminDetailsResponse:
        """返回本人个人 Skill 及当前版本正文。"""
        try:
            return admin_details_to_response(await service.get_personal(actor, skill_id))
        except SkillNotFoundError as error:
            raise HTTPException(404, error.code) from error
        except SkillConflictError as error:
            raise HTTPException(409, error.code) from error

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
        except SkillUserAuthorizationError as error:
            raise _user_authorization_error(error) from error
        except SkillNotFoundError as error:
            raise HTTPException(404, error.code) from error
        except SkillConflictError as error:
            raise HTTPException(409, error.code) from error
        except (PersonalSkillError, ValueError) as error:
            raise _personal_validation_error(error) from error
        except FileStorageError as error:
            raise HTTPException(503, "file_storage_unavailable") from error

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
        except SkillUserAuthorizationError as error:
            raise _user_authorization_error(error) from error
        except SkillNotFoundError as error:
            raise HTTPException(404, error.code) from error
        except SkillConflictError as error:
            raise HTTPException(409, error.code) from error
        except ValueError as error:
            raise HTTPException(422, _safe_code(error)) from error

    @router.delete("/skills/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
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
        except SkillUserAuthorizationError as error:
            raise _user_authorization_error(error) from error
        except SkillNotFoundError as error:
            raise HTTPException(404, error.code) from error
        except SkillConflictError as error:
            raise HTTPException(409, error.code) from error
        except ValueError as error:
            raise HTTPException(422, _safe_code(error)) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


def build_skill_admin_router(service: SkillService) -> APIRouter:
    """创建仅管理员可访问的 Skill 包与版本管理接口。"""
    router = APIRouter(
        prefix="/api/v1/admin",
        tags=["skill-admin"],
        dependencies=[Depends(verify_request_origin)],
    )

    @router.get("/skills", response_model=SkillAdminListResponse)
    async def list_skills(actor: AdminAuth) -> SkillAdminListResponse:
        """读取全部稳定 Skill 身份。"""
        try:
            return admin_list_to_response(await service.list_admin(actor))
        except SkillAuthorizationError as error:
            raise _authorization_error(error) from error

    @router.post(
        "/skills/import",
        response_model=SkillAdminDetailsResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def import_skill(
        actor: AdminAuth,
        package: Annotated[UploadFile, File()],
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key", min_length=1, max_length=128),
        ],
    ) -> SkillAdminDetailsResponse:
        """首发导入完整 ZIP，成功版本默认不启用。"""
        try:
            async with staged_upload(
                package,
                maximum_bytes=MAX_PACKAGE_ZIP_BYTES,
            ) as path:
                return admin_details_to_response(
                    await service.import_package(
                        actor,
                        path,
                        idempotency_key=idempotency_key,
                    )
                )
        except UploadTooLargeError as error:
            raise HTTPException(413, "skill_zip_too_large") from error
        except SkillAuthorizationError as error:
            raise _authorization_error(error) from error
        except SkillConflictError as error:
            raise HTTPException(409, error.code) from error
        except (SkillPackageError, ValueError) as error:
            raise HTTPException(422, _package_error_detail(error)) from error
        except OSError as error:
            raise HTTPException(503, "file_storage_unavailable") from error

    @router.get("/skills/{skill_id}", response_model=SkillAdminDetailsResponse)
    async def get_skill(
        actor: AdminAuth,
        skill_id: Annotated[str, Path(min_length=1, max_length=128)],
    ) -> SkillAdminDetailsResponse:
        """读取管理详情、完整指南、资源清单与最近 100 个版本。"""
        try:
            return admin_details_to_response(await service.get_admin(actor, skill_id))
        except SkillAuthorizationError as error:
            raise _authorization_error(error) from error
        except SkillNotFoundError as error:
            raise HTTPException(404, error.code) from error

    @router.post(
        "/skills/{skill_id}/versions",
        response_model=SkillVersionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def add_version(
        actor: AdminAuth,
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
        except UploadTooLargeError as error:
            raise HTTPException(413, "skill_zip_too_large") from error
        except SkillAuthorizationError as error:
            raise _authorization_error(error) from error
        except SkillNotFoundError as error:
            raise HTTPException(404, error.code) from error
        except SkillConflictError as error:
            raise HTTPException(409, error.code) from error
        except (SkillPackageError, ValueError) as error:
            raise HTTPException(422, _package_error_detail(error)) from error
        except OSError as error:
            raise HTTPException(503, "file_storage_unavailable") from error

    @router.get(
        "/skills/{skill_id}/versions",
        response_model=SkillVersionListResponse,
    )
    async def list_versions(
        actor: AdminAuth,
        skill_id: Annotated[str, Path(min_length=1, max_length=128)],
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> SkillVersionListResponse:
        """分页读取不可变历史版本。"""
        try:
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
        except SkillAuthorizationError as error:
            raise _authorization_error(error) from error
        except SkillNotFoundError as error:
            raise HTTPException(404, error.code) from error

    @router.get(
        "/skills/{skill_id}/versions/{version_id}",
        response_model=SkillVersionResponse,
    )
    async def get_version(
        actor: AdminAuth,
        skill_id: Annotated[str, Path(min_length=1, max_length=128)],
        version_id: Annotated[str, Path(min_length=1, max_length=128)],
    ) -> SkillVersionResponse:
        """读取指定历史版本完整正文。"""
        try:
            return version_to_response(
                await service.get_version(actor, skill_id, version_id)
            )
        except SkillAuthorizationError as error:
            raise _authorization_error(error) from error
        except SkillNotFoundError as error:
            raise HTTPException(404, error.code) from error

    @router.patch("/skills/{skill_id}", response_model=SkillAdminDetailsResponse)
    async def configure_skill(
        request: SkillConfigurationRequest,
        actor: AdminAuth,
        skill_id: Annotated[str, Path(min_length=1, max_length=128)],
    ) -> SkillAdminDetailsResponse:
        """按配置修订号原子切换版本、启停和脚本审批。"""
        try:
            await service.configure(
                actor,
                skill_id,
                expected_revision=request.expected_revision,
                enabled=request.enabled,
                current_version_id=request.current_version_id,
                script_requires_approval=request.script_requires_approval,
            )
            return admin_details_to_response(await service.get_admin(actor, skill_id))
        except SkillAuthorizationError as error:
            raise _authorization_error(error) from error
        except SkillNotFoundError as error:
            raise HTTPException(404, error.code) from error
        except SkillConflictError as error:
            raise HTTPException(409, error.code) from error
        except SkillDependencyUnsatisfiedError as error:
            raise HTTPException(422, error.code) from error

    @router.get(
        "/skill-executions/{call_id}",
        response_model=SkillExecutionResponse,
    )
    async def get_skill_execution(
        actor: AdminAuth,
        call_id: Annotated[str, Path(min_length=1, max_length=128)],
    ) -> SkillExecutionResponse:
        """读取脚本解释器、错误和日志可用性等排查摘要。"""
        try:
            return execution_to_response(
                await service.admin_execution(actor, call_id)
            )
        except SkillAuthorizationError as error:
            raise _authorization_error(error) from error
        except SkillNotFoundError as error:
            raise HTTPException(404, error.code) from error

    @router.get("/skill-executions/{call_id}/log")
    async def get_skill_execution_log(
        actor: AdminAuth,
        call_id: Annotated[str, Path(min_length=1, max_length=128)],
    ) -> Response:
        """管理员下载最多 5 MiB 原始日志，普通用户路由不暴露正文。"""
        try:
            content = await service.admin_log(actor, call_id)
        except SkillAuthorizationError as error:
            raise _authorization_error(error) from error
        except SkillNotFoundError as error:
            raise HTTPException(404, error.code) from error
        except OSError as error:
            raise HTTPException(503, "file_storage_unavailable") from error
        return Response(
            content=content,
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="skill-execution-{call_id}.log"'
                ),
                "X-Content-Type-Options": "nosniff",
            },
        )

    return router


def _authorization_error(error: SkillAuthorizationError) -> HTTPException:
    """把事务前后的管理员复核失败统一映射为 403。"""
    return HTTPException(status_code=403, detail=error.args[0] if error.args else "admin_required")


def _user_authorization_error(error: SkillUserAuthorizationError) -> HTTPException:
    """把个人写事务中的登录失效统一映射为 401。"""
    return HTTPException(
        status_code=401,
        detail=error.args[0] if error.args else "not_authenticated",
    )


def _personal_validation_error(error: Exception) -> HTTPException:
    """只向个人 Skill 客户端返回白名单形式的稳定校验码。"""
    explicit = getattr(error, "code", None)
    if isinstance(explicit, str):
        code = explicit
    else:
        value = str(error)
        code = value if value.replace("_", "").isalnum() and value.isascii() else (
            "skill_personal_content_invalid"
        )
    field = getattr(error, "field", None)
    if (
        isinstance(field, str)
        and 0 < len(field) <= 128
        and all(character.isprintable() for character in field)
    ):
        # 保留稳定错误码供前端映射，同时提供安全的表单字段定位；
        # 原始 Markdown、YAML 值和异常文本都不能进入响应。
        return HTTPException(
            status_code=422,
            detail={"code": code, "field": field},
        )
    return HTTPException(status_code=422, detail=code)


def _safe_code(error: Exception) -> str:
    """只向外返回领域稳定错误码，不回显原始异常文本。"""
    value = getattr(error, "code", None)
    return value if isinstance(value, str) else "skill_package_invalid"


def _package_error_detail(error: Exception) -> str | dict[str, str]:
    """为可定位的包成员返回安全路径，其余错误保持原字符串 detail。"""
    code = _safe_code(error)
    if not isinstance(error, SkillPackageError) or error.member_path is None:
        return code
    return {"code": code, "member_path": error.member_path}


__all__ = [
    "build_skill_admin_router",
    "build_skill_personal_router",
    "build_skill_user_router",
]
