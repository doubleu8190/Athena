"""项目目录用例与业务规则。"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Sequence
from datetime import datetime

from app.project.converter.result import project_page_to_result, project_to_result
from app.project.model.result import ProjectPageResult, ProjectResult
from common.date import as_utc, utc_now
from common.ids import new_id
from domain.project import (
    InvalidProjectInputError,
    InvalidProjectNameError,
    Project,
    ProjectNotFoundError,
    ProjectRepository,
)

MAX_PROJECT_NAME_LENGTH = 120
MAX_PROJECT_DESCRIPTION_LENGTH = 500
MAX_PROJECT_INSTRUCTIONS_LENGTH = 8000
MAX_PROJECT_DEFAULT_SKILLS = 20
MAX_SKILL_ID_LENGTH = 128
MAX_PAGE_SIZE = 100


def _default_id_factory() -> str:
    """使用全局随机唯一 ID 生成项目主键，消除单进程时间戳锁以支持多副本并发。"""
    return new_id("proj")


def _normalize_name(name: str) -> str:
    """去除名称两端空白并执行长度和控制字符校验。"""
    if not isinstance(name, str):
        raise InvalidProjectNameError("project_name_invalid")
    # 先校验原始字符串中的控制字符，防止 \x0b/\x0c 等被 strip 静默吞掉。
    if any(unicodedata.category(character) == "Cc" for character in name):
        raise InvalidProjectNameError("project_name_invalid")
    normalized = name.strip()
    if not normalized or len(normalized) > MAX_PROJECT_NAME_LENGTH:
        raise InvalidProjectNameError("project_name_invalid")
    return normalized


def _normalize_description(description: str | None) -> str | None:
    """规范化可选项目简介：长度 <= 500，拒绝全部 Cc 控制字符，纯空白归一为 None。"""
    if description is None:
        return None
    if not isinstance(description, str):
        raise InvalidProjectInputError("project_description_invalid")
    if any(unicodedata.category(character) == "Cc" for character in description):
        raise InvalidProjectInputError("project_description_invalid")
    normalized = description.strip()
    if not normalized:
        return None
    if len(normalized) > MAX_PROJECT_DESCRIPTION_LENGTH:
        raise InvalidProjectInputError("project_description_invalid")
    return normalized


def _normalize_instructions(instructions: str | None) -> str | None:
    """规范化可选项目背景指令：长度 <= 8000，允许换行/回车/制表符，拒绝其余 Cc 控制字符。"""
    if instructions is None:
        return None
    if not isinstance(instructions, str):
        raise InvalidProjectInputError("project_instructions_invalid")
    if any(
        unicodedata.category(character) == "Cc" and character not in ("\n", "\r", "\t")
        for character in instructions
    ):
        raise InvalidProjectInputError("project_instructions_invalid")
    normalized = instructions.strip()
    if not normalized:
        return None
    if len(normalized) > MAX_PROJECT_INSTRUCTIONS_LENGTH:
        raise InvalidProjectInputError("project_instructions_invalid")
    return normalized


def _normalize_default_skill_ids(values: Sequence[str] | None) -> tuple[str, ...]:
    """规范化并去重项目默认技能 ID 列表：最多 20 个非空且无控制字符的有效 ID。"""
    if values is None:
        return ()
    if isinstance(values, (str, bytes)) or len(values) > MAX_PROJECT_DEFAULT_SKILLS:
        raise InvalidProjectInputError("project_default_skill_ids_invalid")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, str) or any(
            unicodedata.category(character) == "Cc" for character in item
        ):
            raise InvalidProjectInputError("project_default_skill_ids_invalid")
        cleaned = item.strip()
        if not cleaned or len(cleaned) > MAX_SKILL_ID_LENGTH:
            raise InvalidProjectInputError("project_default_skill_ids_invalid")
        if cleaned not in seen:
            seen.add(cleaned)
            normalized.append(cleaned)
    return tuple(normalized)


def _normalized_owner_id(owner_id: str) -> str:
    """校验服务端提供的项目所有者。"""
    normalized = owner_id.strip()
    if not normalized:
        raise ValueError("owner_id_required")
    return normalized


class ProjectService:
    """创建、查询、更新和软删除项目。"""

    def __init__(
        self,
        repository: ProjectRepository,
        *,
        id_factory: Callable[[], str] = _default_id_factory,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """保存仓储及可替换的 ID、时钟来源。"""
        self._repository = repository
        self._id_factory = id_factory
        self._clock = clock

    async def create(
        self,
        owner_id: str,
        name: str,
        *,
        description: str | None = None,
        instructions: str | None = None,
        default_skill_ids: Sequence[str] | None = None,
    ) -> ProjectResult:
        """创建允许重名的规范化项目，可选携带简介、背景指令与默认技能列表。"""
        owner = _normalized_owner_id(owner_id)
        normalized = _normalize_name(name)
        normalized_description = _normalize_description(description)
        normalized_instructions = _normalize_instructions(instructions)
        normalized_skills = _normalize_default_skill_ids(default_skill_ids)
        current = as_utc(self._clock())
        project = await self._repository.create(
            Project(
                id=self._id_factory(),
                owner_id=owner,
                name=normalized,
                created_at=current,
                updated_at=current,
                description=normalized_description,
                instructions=normalized_instructions,
                default_skill_ids=normalized_skills,
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

    async def update(
        self,
        owner_id: str,
        project_id: str,
        *,
        update_name: bool = False,
        name: str | None = None,
        update_description: bool = False,
        description: str | None = None,
        update_instructions: bool = False,
        instructions: str | None = None,
        update_default_skills: bool = False,
        default_skill_ids: Sequence[str] | None = None,
    ) -> ProjectResult:
        """局部更新未删除项目的名称、简介、背景指令或默认技能集合。"""
        if not any((update_name, update_description, update_instructions, update_default_skills)):
            raise InvalidProjectInputError("project_update_invalid")
        owner = _normalized_owner_id(owner_id)
        normalized_name: str | None = None
        if update_name:
            if name is None:
                raise InvalidProjectNameError("project_name_invalid")
            normalized_name = _normalize_name(name)
        normalized_description = _normalize_description(description) if update_description else None
        normalized_instructions = (
            _normalize_instructions(instructions) if update_instructions else None
        )
        normalized_skills = (
            _normalize_default_skill_ids(default_skill_ids) if update_default_skills else None
        )
        project = await self._repository.update(
            owner,
            project_id,
            update_name=update_name,
            name=normalized_name,
            update_description=update_description,
            description=normalized_description,
            update_instructions=update_instructions,
            instructions=normalized_instructions,
            update_default_skills=update_default_skills,
            default_skill_ids=normalized_skills,
            updated_at=as_utc(self._clock()),
        )
        if project is None:
            raise ProjectNotFoundError("project_not_found")
        return project_to_result(project)

    async def delete(
        self,
        owner_id: str,
        project_id: str,
        *,
        detach_sessions: bool = False,
    ) -> None:
        """软删除项目，允许对已删除项目重复调用；支持按需先解绑无活跃 Run 的会话。"""
        owner = _normalized_owner_id(owner_id)
        deleted = await self._repository.soft_delete(
            owner,
            project_id,
            as_utc(self._clock()),
            detach_sessions=detach_sessions,
        )
        if not deleted:
            raise ProjectNotFoundError("project_not_found")


__all__ = [
    "MAX_PAGE_SIZE",
    "MAX_PROJECT_DEFAULT_SKILLS",
    "MAX_PROJECT_DESCRIPTION_LENGTH",
    "MAX_PROJECT_INSTRUCTIONS_LENGTH",
    "MAX_PROJECT_NAME_LENGTH",
    "ProjectService",
]
