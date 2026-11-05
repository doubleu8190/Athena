"""项目目录用例与业务规则。"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime
from threading import Lock

from app.project.converter.result import project_page_to_result, project_to_result
from app.project.model.result import ProjectPageResult, ProjectResult
from common.ids import monotonic_timestamp_id_factory, now_utc
from domain.project import (
    InvalidProjectNameError,
    Project,
    ProjectNotFoundError,
    ProjectRepository,
)

MAX_PROJECT_NAME_LENGTH = 120
MAX_PAGE_SIZE = 100

_project_id_factory = monotonic_timestamp_id_factory()
_project_id_factory_lock = Lock()


def _default_id_factory() -> str:
    """使用进程级单调 UTC 毫秒工厂生成项目主键。"""
    with _project_id_factory_lock:
        return _project_id_factory()


def _normalize_name(name: str) -> str:
    """去除名称两端空白并执行长度和控制字符校验。"""
    normalized = name.strip()
    has_control_character = any(unicodedata.category(character) == "Cc" for character in normalized)
    if not normalized or len(normalized) > MAX_PROJECT_NAME_LENGTH or has_control_character:
        raise InvalidProjectNameError("project_name_invalid")
    return normalized


def _utc_value(value: datetime) -> datetime:
    """把时钟输出规范化为 UTC。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _normalized_owner_id(owner_id: str) -> str:
    """校验服务端提供的项目所有者。"""
    normalized = owner_id.strip()
    if not normalized:
        raise ValueError("owner_id_required")
    return normalized


class ProjectService:
    """创建、查询、改名和软删除项目。"""

    def __init__(
        self,
        repository: ProjectRepository,
        *,
        id_factory: Callable[[], str] = _default_id_factory,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        """保存仓储及可替换的 ID、时钟来源。"""
        self._repository = repository
        self._id_factory = id_factory
        self._clock = clock

    async def create(self, owner_id: str, name: str) -> ProjectResult:
        """创建允许重名的规范化项目。"""
        owner = _normalized_owner_id(owner_id)
        normalized = _normalize_name(name)
        current = _utc_value(self._clock())
        project = await self._repository.create(
            Project(
                id=self._id_factory(),
                owner_id=owner,
                name=normalized,
                created_at=current,
                updated_at=current,
            )
        )
        return project_to_result(project)

    async def get(self, owner_id: str, project_id: str) -> ProjectResult:
        """读取未删除项目。"""
        project = await self._repository.get(
            _normalized_owner_id(owner_id),
            project_id,
        )
        if project is None:
            raise ProjectNotFoundError("project_not_found")
        return project_to_result(project)

    async def list(
        self,
        owner_id: str,
        *,
        page: int,
        page_size: int,
    ) -> ProjectPageResult:
        """分页读取未删除项目。"""
        owner = _normalized_owner_id(owner_id)
        if page < 1 or not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError("invalid_pagination")
        result = await self._repository.list_active(
            owner,
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        return project_page_to_result(result)

    async def rename(
        self,
        owner_id: str,
        project_id: str,
        name: str,
    ) -> ProjectResult:
        """修改未删除项目名称。"""
        owner = _normalized_owner_id(owner_id)
        normalized = _normalize_name(name)
        project = await self._repository.rename(
            owner,
            project_id,
            normalized,
            _utc_value(self._clock()),
        )
        if project is None:
            raise ProjectNotFoundError("project_not_found")
        return project_to_result(project)

    async def delete(self, owner_id: str, project_id: str) -> None:
        """软删除项目，允许对已删除项目重复调用。"""
        owner = _normalized_owner_id(owner_id)
        deleted = await self._repository.soft_delete(
            owner,
            project_id,
            _utc_value(self._clock()),
        )
        if not deleted:
            raise ProjectNotFoundError("project_not_found")


__all__ = [
    "MAX_PAGE_SIZE",
    "MAX_PROJECT_NAME_LENGTH",
    "ProjectService",
]
