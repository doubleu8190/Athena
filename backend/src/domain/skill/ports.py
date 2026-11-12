"""Skill 管理、Run 快照和脚本执行附加记录的持久化端口。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

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
    SkillVersion,
    SkillVersionPage,
)


class SkillRepository(Protocol):
    """Skill 管理目录及执行附加记录的事务端口。"""

    async def list_skills(self) -> tuple[Skill, ...]:
        """读取全部稳定 Skill 身份。"""

    async def get_skill(self, skill_id: str) -> Skill | None:
        """读取一个管理目录项。"""

    async def get_public_directory(self) -> tuple[Skill, ...]:
        """返回已启用候选，当前依赖状态由服务层实时探测。"""

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
    ) -> tuple[Skill, SkillVersion]:
        """原子创建首发 Skill 与第一个未启用版本。"""

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
    ) -> Skill | None:
        """按修订号 CAS 切换版本、启停和审批配置。"""

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
            ),
        ),
        selected_skill_ids=(skill.id,),
    )


__all__ = ["SkillPackageObjectStore", "SkillRepository", "snapshot_from_skill"]
