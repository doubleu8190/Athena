"""项目与知识库的关联用例。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from app.access import AccessPolicyService
from app.project.service import ProjectService
from common.ids import now_utc
from domain.access import ResourceType
from domain.knowledge.model import KnowledgeBase
from domain.project import ProjectNotFoundError


class ProjectKnowledgeRepository(Protocol):
    """项目知识库关联持久化端口。"""

    async def list_project_bases(self, owner_id: str, project_id: str) -> list[KnowledgeBase]: ...

    async def bind_project_base(
        self, owner_id: str, project_id: str, base_id: str, at: datetime
    ) -> bool: ...

    async def unbind_project_base(self, owner_id: str, project_id: str, base_id: str) -> bool: ...


class ProjectKnowledgeService:
    """校验项目归属与知识库实时可读权限后管理项目知识库关联。"""

    def __init__(
        self,
        projects: ProjectService,
        repository: ProjectKnowledgeRepository,
        *,
        access_policy: AccessPolicyService | None = None,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        """保存项目查询用例、关联仓储、统一权限引擎和可替换时钟。"""
        self._projects = projects
        self._repository = repository
        self._access_policy = access_policy
        self._clock = clock

    async def list(self, owner_id: str, project_id: str) -> list[KnowledgeBase]:
        """读取当前用户项目关联且当前仍有读权限的知识库（员工调岗后旧部门库立即失效）。"""
        await self._projects.get(owner_id, project_id)
        bases = await self._repository.list_project_bases(owner_id, project_id)
        if self._access_policy is None or not bases:
            return bases
        readable_ids = await self._access_policy.filter_readable_ids(
            owner_id,
            ResourceType.KNOWLEDGE_BASE,
            [base.id for base in bases],
        )
        return [base for base in bases if base.id in readable_ids]

    async def bind(self, owner_id: str, project_id: str, base_id: str) -> None:
        """幂等关联个人项目与当前用户有读权限的任意层级知识库。"""
        await self._projects.get(owner_id, project_id)
        if self._access_policy is not None:
            can_read = await self._access_policy.can_read_resource(
                owner_id,
                ResourceType.KNOWLEDGE_BASE,
                base_id,
            )
            if not can_read:
                # 不可读知识库统一返回 404，防止探测其他部门或他人知识库 ID。
                raise KnowledgeNotFoundError("knowledge_base_not_found")
        try:
            found = await self._repository.bind_project_base(
                owner_id,
                project_id,
                base_id,
                self._clock(),
            )
        except LookupError as error:
            raise KnowledgeNotFoundError("knowledge_base_not_found") from error
        if not found:
            raise ProjectNotFoundError("project_not_found")

    async def unbind(self, owner_id: str, project_id: str, base_id: str) -> None:
        """幂等解除项目和知识库关联。"""
        await self._projects.get(owner_id, project_id)
        if not await self._repository.unbind_project_base(owner_id, project_id, base_id):
            raise ProjectNotFoundError("project_not_found")


class KnowledgeNotFoundError(LookupError):
    """知识库不存在或不属于当前用户。"""
