"""管理员 Subagent 配置管理应用服务。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from common.ids import new_id, now_utc
from domain.auth import AuthContext, CurrentSessionReader, UserRole
from domain.subagent import (
    AvailableToolGroup,
    CreateSubagentCommand,
    SubagentAdminRequiredError,
    SubagentInvalidSessionError,
    SubagentModelChecker,
    SubagentModelUnavailableError,
    SubagentNotFoundError,
    SubagentPage,
    SubagentProfile,
    SubagentRepository,
    SubagentToolCatalog,
    SubagentUpdatePatch,
    SubagentValidationError,
    UnsetType,
    UpdateSubagentCommand,
    validate_subagent_allowed_tool_ids,
    validate_subagent_description,
    validate_subagent_max_llm_requests,
    validate_subagent_model_profile_id,
    validate_subagent_name,
    validate_subagent_slug,
    validate_subagent_system_prompt,
    validate_subagent_timeout_seconds,
)

MAX_SUBAGENT_PAGE_LIMIT = 100


class SubagentService:
    """编排 Subagent 配置 CRUD、启停、模型/工具校验与管理员权限复核。"""

    def __init__(
        self,
        repository: SubagentRepository,
        auth_reader: CurrentSessionReader,
        *,
        model_checker: SubagentModelChecker | None = None,
        tool_catalog: SubagentToolCatalog | None = None,
        id_factory: Callable[[str], str] = new_id,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        """保存仓储、会话读取、模型/工具校验端口与基础设施依赖。"""
        self._repository = repository
        self._auth_reader = auth_reader
        self._model_checker = model_checker
        self._tool_catalog = tool_catalog
        self._id_factory = id_factory
        self._clock = clock

    async def list_subagents(
        self,
        actor: AuthContext,
        *,
        offset: int = 0,
        limit: int = 20,
        enabled: bool | None = None,
    ) -> SubagentPage:
        """复核管理员权限后分页查询 Subagent 配置列表。"""
        await self._authorize(actor)
        _validate_page(offset, limit)
        return await self._repository.list_page(offset=offset, limit=limit, enabled=enabled)

    async def get_subagent(self, actor: AuthContext, subagent_id: str) -> SubagentProfile:
        """复核管理员权限后按 ID 读取单个 Subagent 配置。"""
        await self._authorize(actor)
        _validate_subagent_id(subagent_id)
        profile = await self._repository.get(subagent_id)
        if profile is None:
            raise SubagentNotFoundError()
        return profile

    async def create_subagent(
        self,
        actor: AuthContext,
        command: CreateSubagentCommand,
    ) -> SubagentProfile:
        """校验管理员权限、字段约束、模型可用性与工具白名单后创建 Subagent。"""
        await self._authorize(actor)
        slug = validate_subagent_slug(command.slug)
        name = validate_subagent_name(command.name)
        description = validate_subagent_description(command.description)
        system_prompt = validate_subagent_system_prompt(command.system_prompt)
        model_profile_id = validate_subagent_model_profile_id(command.model_profile_id)
        allowed_tool_ids = validate_subagent_allowed_tool_ids(command.allowed_tool_ids)
        max_llm_requests = validate_subagent_max_llm_requests(command.max_llm_requests)
        timeout_seconds = validate_subagent_timeout_seconds(command.timeout_seconds)
        if type(command.enabled) is not bool:
            raise SubagentValidationError("subagent_enabled_invalid")

        await self._validate_external_references(
            model_profile_id=model_profile_id,
            allowed_tool_ids=allowed_tool_ids,
        )

        now = self._clock()
        profile = SubagentProfile(
            id=self._id_factory("subagent"),
            slug=slug,
            name=name,
            description=description,
            system_prompt=system_prompt,
            model_profile_id=model_profile_id,
            allowed_tool_ids=allowed_tool_ids,
            enabled=command.enabled,
            max_llm_requests=max_llm_requests,
            timeout_seconds=timeout_seconds,
            version=1,
            created_by=actor.user.id,
            deleted_at=None,
            created_at=now,
            updated_at=now,
        )
        return await self._repository.create(actor, profile)

    async def update_subagent(
        self,
        actor: AuthContext,
        subagent_id: str,
        command: UpdateSubagentCommand,
    ) -> SubagentProfile:
        """校验管理员权限与乐观锁基线，合并部分更新字段后提交仓储。"""
        await self._authorize(actor)
        _validate_subagent_id(subagent_id)
        if type(command.expected_version) is not int or command.expected_version < 1:
            raise SubagentValidationError("subagent_version_invalid")

        current = await self._repository.get(subagent_id)
        if current is None:
            raise SubagentNotFoundError()

        slug = (
            current.slug
            if isinstance(command.slug, UnsetType)
            else validate_subagent_slug(command.slug)
        )
        name = (
            current.name
            if isinstance(command.name, UnsetType)
            else validate_subagent_name(command.name)
        )
        description = (
            current.description
            if isinstance(command.description, UnsetType)
            else validate_subagent_description(command.description)
        )
        system_prompt = (
            current.system_prompt
            if isinstance(command.system_prompt, UnsetType)
            else validate_subagent_system_prompt(command.system_prompt)
        )
        model_profile_id = (
            current.model_profile_id
            if isinstance(command.model_profile_id, UnsetType)
            else validate_subagent_model_profile_id(command.model_profile_id)
        )
        allowed_tool_ids = (
            current.allowed_tool_ids
            if isinstance(command.allowed_tool_ids, UnsetType)
            else validate_subagent_allowed_tool_ids(command.allowed_tool_ids)
        )
        max_llm_requests = (
            current.max_llm_requests
            if isinstance(command.max_llm_requests, UnsetType)
            else validate_subagent_max_llm_requests(command.max_llm_requests)
        )
        timeout_seconds = (
            current.timeout_seconds
            if isinstance(command.timeout_seconds, UnsetType)
            else validate_subagent_timeout_seconds(command.timeout_seconds)
        )

        # 仅当本次修改了模型或工具白名单，或当前仍绑定外部引用时复核其可用性。
        await self._validate_external_references(
            model_profile_id=(
                None if isinstance(command.model_profile_id, UnsetType) else model_profile_id
            ),
            allowed_tool_ids=(
                () if isinstance(command.allowed_tool_ids, UnsetType) else allowed_tool_ids
            ),
        )

        patch = SubagentUpdatePatch(
            slug=slug,
            name=name,
            description=description,
            system_prompt=system_prompt,
            model_profile_id=model_profile_id,
            allowed_tool_ids=allowed_tool_ids,
            max_llm_requests=max_llm_requests,
            timeout_seconds=timeout_seconds,
            updated_at=self._clock(),
        )
        return await self._repository.update(
            actor,
            subagent_id,
            expected_version=command.expected_version,
            patch=patch,
        )

    async def set_subagent_enabled(
        self,
        actor: AuthContext,
        subagent_id: str,
        enabled: bool,
    ) -> SubagentProfile:
        """复核管理员权限后切换 Subagent 启停状态。"""
        await self._authorize(actor)
        _validate_subagent_id(subagent_id)
        if type(enabled) is not bool:
            raise SubagentValidationError("subagent_enabled_invalid")
        return await self._repository.set_enabled(
            actor,
            subagent_id,
            enabled,
            updated_at=self._clock(),
        )

    async def delete_subagent(self, actor: AuthContext, subagent_id: str) -> None:
        """复核管理员权限后执行幂等软删除。"""
        await self._authorize(actor)
        _validate_subagent_id(subagent_id)
        await self._repository.soft_delete(
            actor,
            subagent_id,
            deleted_at=self._clock(),
        )

    async def list_available_tools(
        self,
        actor: AuthContext,
    ) -> tuple[AvailableToolGroup, ...]:
        """复核管理员权限后返回聚合的可选工具目录（不含嵌套调度工具）。"""
        await self._authorize(actor)
        if self._tool_catalog is None:
            return ()
        return await self._tool_catalog.list_available_tool_groups()

    async def _authorize(self, actor: AuthContext) -> None:
        """每次操作前通过当前会话读取器复核管理员身份。"""
        user = await self._auth_reader.get_current_session_user(
            actor.user.id,
            actor.session_id,
            self._clock(),
        )
        if user is None:
            raise SubagentInvalidSessionError()
        if user.role is not UserRole.ADMIN:
            raise SubagentAdminRequiredError()

    async def _validate_external_references(
        self,
        *,
        model_profile_id: str | None,
        allowed_tool_ids: tuple[str, ...],
    ) -> None:
        """校验绑定的模型配置可用性与白名单工具合法性。"""
        if model_profile_id is not None and self._model_checker is not None:
            available = await self._model_checker.is_model_available(model_profile_id)
            if not available:
                raise SubagentModelUnavailableError()
        if allowed_tool_ids and self._tool_catalog is not None:
            available_tool_ids = await self._tool_catalog.list_available_tool_ids()
            unknown_tools = [
                tool_id for tool_id in allowed_tool_ids if tool_id not in available_tool_ids
            ]
            if unknown_tools:
                raise SubagentValidationError("subagent_tool_not_found")


def _validate_page(offset: int, limit: int) -> None:
    """校验分页参数范围。"""
    if (
        type(offset) is not int
        or type(limit) is not int
        or offset < 0
        or limit < 1
        or limit > MAX_SUBAGENT_PAGE_LIMIT
    ):
        raise SubagentValidationError("subagent_page_invalid")


def _validate_subagent_id(subagent_id: str) -> None:
    """校验 Subagent ID 非空且不超过数据库列长。"""
    if not isinstance(subagent_id, str) or not subagent_id.strip() or len(subagent_id) > 64:
        raise SubagentValidationError("subagent_id_invalid")


__all__ = ["MAX_SUBAGENT_PAGE_LIMIT", "SubagentService"]
