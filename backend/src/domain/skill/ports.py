"""Skill 管理、Run 快照和脚本执行附加记录的持久化端口。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from domain.access import VisibilityGrantInput
from domain.auth import AuthContext
from domain.file import PendingSessionFile
from domain.skill.model import (
    DependencyCheck,
    RunSkillSnapshot,
    Skill,
    SkillExecutionRecord,
    SkillExecutionStart,
    SkillExecutionStatus,
    SkillImportRequestResult,
    SkillPackageManifest,
    SkillScope,
    SkillVersion,
    SkillVersionPage,
)


class SkillRepository(Protocol):
    """Skill 管理目录及执行附加记录的事务端口。"""

    async def list_skills(self) -> tuple[Skill, ...]:
        """读取全部未删除的公共稳定 Skill 身份。"""

    async def get_skill(self, skill_id: str) -> Skill | None:
        """读取一个未删除的公共管理目录项。"""

    async def get_public_directory(self) -> tuple[Skill, ...]:
        """返回已启用候选，当前依赖状态由服务层实时探测。"""

    async def get_public_directory_entries(self) -> tuple[tuple[Skill, SkillVersion], ...]:
        """单次 JOIN 返回已启用公共候选及其当前不可变版本，消除目录 N+1 查询。"""

    async def get_user_directory(self, owner_id: str) -> tuple[Skill, ...]:
        """返回公共及该用户本人已启用且未删除的 Skill 候选。"""

    async def get_user_directory_entries(
        self, owner_id: str
    ) -> tuple[tuple[Skill, SkillVersion], ...]:
        """单次 JOIN 返回公共及本人已启用候选及其当前版本，消除目录 N+1 查询。"""

    async def list_personal_skills(self, owner_id: str) -> tuple[Skill, ...]:
        """按稳定顺序读取指定用户未删除的个人 Skill。"""

    async def get_personal_skill(self, owner_id: str, skill_id: str) -> Skill | None:
        """只在指定归属范围内读取一个未删除的个人 Skill。"""

    async def list_versions(
        self, skill_id: str, *, page: int, page_size: int
    ) -> SkillVersionPage | None:
        """读取不可变版本历史。"""

    async def get_version(self, version_id: str) -> SkillVersion | None:
        """读取任意不可变版本。"""

    async def create_skill(
        self,
        *,
        skill_id: str,
        version_id: str,
        label: str,
        manifest: SkillPackageManifest,
        object_key: str,
        package_sha256: str,
        dependency_check: DependencyCheck,
        actor: AuthContext,
        idempotency_key: str,
        request_fingerprint: str,
        created_at: datetime,
        scope: SkillScope = SkillScope.COMPANY,
        owner_department_id: str | None = None,
        visibility_grants: Sequence[VisibilityGrantInput] = (),
    ) -> tuple[Skill, SkillVersion]:
        """原子创建首发公司/部门 Skill 与第一个未启用版本及可见性授权。"""

    async def create_personal_skill(
        self,
        *,
        skill_id: str,
        version_id: str,
        manifest: SkillPackageManifest,
        object_key: str,
        package_sha256: str,
        dependency_check: DependencyCheck,
        actor: AuthContext,
        idempotency_key: str,
        request_fingerprint: str,
        created_at: datetime,
    ) -> SkillImportRequestResult:
        """原子创建本人 Skill 与已启用首版，并保持请求键幂等。"""

    async def add_version(
        self,
        skill_id: str,
        *,
        version_id: str,
        label: str | None,
        manifest: SkillPackageManifest,
        object_key: str,
        package_sha256: str,
        dependency_check: DependencyCheck,
        actor: AuthContext,
        idempotency_key: str,
        request_fingerprint: str,
        created_at: datetime,
    ) -> SkillVersion | None:
        """追加完整更新版本，不改变当前指针和启用状态。"""

    async def publish_personal_version(
        self,
        skill_id: str,
        *,
        version_id: str,
        expected_revision: int,
        manifest: SkillPackageManifest,
        object_key: str,
        package_sha256: str,
        dependency_check: DependencyCheck,
        actor: AuthContext,
        idempotency_key: str,
        request_fingerprint: str,
        created_at: datetime,
    ) -> SkillImportRequestResult | None:
        """按修订号 CAS 追加个人版本，并原子切换当前指针和启用状态。"""

    async def find_import_result(
        self,
        actor: AuthContext,
        *,
        operation: str,
        target_skill_id: str | None,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> SkillImportRequestResult | None:
        """读取已提交导入请求；键冲突或目标不一致时抛出稳定冲突。"""

    async def find_personal_request_result(
        self,
        actor: AuthContext,
        *,
        operation: str,
        target_skill_id: str | None,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> SkillImportRequestResult | None:
        """只在当前用户归属内读取已提交的个人创建或发布请求。"""

    async def update_configuration(
        self,
        skill_id: str,
        *,
        expected_revision: int,
        enabled: bool,
        current_version_id: str,
        script_requires_approval: bool,
        dependency_check: DependencyCheck,
        actor: AuthContext,
        updated_at: datetime,
        visibility_grants: Sequence[VisibilityGrantInput] | None = None,
    ) -> Skill | None:
        """按修订号 CAS 切换版本、启停、审批配置及可选的部门可见性授权。"""

    async def delete_admin_skill(
        self,
        skill_id: str,
        *,
        expected_revision: int,
        actor: AuthContext,
        deleted_at: datetime,
    ) -> bool:
        """软删除部门级 Skill 并同事务清理可见性授权。"""

    async def set_personal_enabled(
        self,
        skill_id: str,
        *,
        expected_revision: int,
        enabled: bool,
        actor: AuthContext,
        updated_at: datetime,
    ) -> Skill | None:
        """按修订号 CAS 启停本人 Skill，不接受版本或脚本审批修改。"""

    async def delete_personal_skill(
        self,
        skill_id: str,
        *,
        expected_revision: int,
        actor: AuthContext,
        deleted_at: datetime,
    ) -> bool:
        """软删除本人 Skill；同 revision 重放成功，冲突时抛出稳定业务异常。"""

    async def record_dependency_check(
        self,
        version_id: str,
        dependency_check: DependencyCheck,
    ) -> SkillVersion | None:
        """保存随当前执行环境刷新的依赖检查，不改包正文。"""

    async def start_script_execution(
        self,
        *,
        execution_id: str,
        run_id: str,
        session_id: str,
        tool_call_record_id: str,
        skill_id: str,
        skill_version_id: str,
        script_path: str,
        input_file_ids: tuple[str, ...],
        parameters_sha256: str,
        python_version: str,
        dependency_check: DependencyCheck,
        created_at: datetime,
    ) -> SkillExecutionStart | None:
        """幂等读取或创建执行记录，并返回本次是否真正新建。"""

    async def finish_script_execution(
        self,
        execution_id: str,
        *,
        status: SkillExecutionStatus,
        log_object_key: str | None,
        log_truncated: bool,
        log_error_code: str | None,
        error_code: str | None,
        cleanup_error_code: str | None,
        artifacts: Sequence[PendingSessionFile],
        finished_at: datetime,
    ) -> SkillExecutionRecord | None:
        """保存脚本终态和待发布产物元数据。"""

    async def get_script_execution(
        self, tool_call_record_id: str
    ) -> SkillExecutionRecord | None:
        """按现有工具调用记录读取一对一附加信息。"""

    async def get_admin_execution(self, call_id: str) -> SkillExecutionRecord | None:
        """管理员按工具调用记录读取排查信息。"""

class SkillPackageObjectStore(Protocol):
    """供 Skill 包和日志复用的最小对象存储接口。"""

    async def get_bytes(self, key: str, *, maximum_bytes: int) -> bytes:
        """读取有界技能包或日志对象。"""


def snapshot_from_skill(skill: Skill, version: SkillVersion) -> RunSkillSnapshot:
    """构造只含单个选中项的快照辅助函数，供测试和调用方复用。"""
    from domain.skill.model import RunSkillEntry

    return RunSkillSnapshot(
        skills=(
            RunSkillEntry(
                skill_id=skill.id,
                slug=skill.slug,
                name=skill.name,
                description=skill.description,
                version_id=version.id,
                version_label=version.label,
                requires_approval=skill.script_requires_approval,
                scope=skill.scope,
            ),
        ),
        selected_skill_ids=(skill.id,),
    )


__all__ = ["SkillPackageObjectStore", "SkillRepository", "snapshot_from_skill"]
