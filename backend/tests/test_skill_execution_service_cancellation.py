"""SkillExecutionService 对真实 runner 清理结论的持久化边界测试。"""

from __future__ import annotations

import asyncio
import os
import sys
import zipfile
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from adapter.skill.execution import PythonScriptExecutor
from app.skill.execution_service import SkillExecutionService
from app.skill.package import SkillPackageValidator
from app.skill.service import SkillService
from domain.file import PendingSessionFile
from domain.skill import (
    DependencyCheck,
    DependencyCheckStatus,
    RunSkillEntry,
    RunSkillSnapshot,
    SkillExecutionRecord,
    SkillExecutionStart,
    SkillExecutionStatus,
    SkillPackageManifest,
    SkillRepository,
    SkillScriptDefinition,
    SkillVersion,
)
from domain.skill.execution import (
    SCRIPT_PROCESS_DIAGNOSTIC_FILENAME,
    ScriptCleanupErrorCode,
    ScriptExecutionCancelled,
    ScriptExecutionErrorCode,
    ScriptExecutionRequest,
    ScriptExecutionResult,
)
from domain.tool import ToolExecutionContext

_NOW = datetime(2026, 11, 12, tzinfo=UTC)
_CALL_RECORD_ID = f"toolcall_{'a' * 23}"


def _package_and_manifest(tmp_path: Path) -> tuple[bytes, SkillPackageManifest]:
    """构造包含长运行脚本的最小合法技能包。"""
    package_path = tmp_path / "skill.zip"
    markdown = """
---
name: 取消测试
slug: cancel-test
description: 验证真实进程取消后的目录所有权。
dependencies: []
scripts:
  - path: scripts/run.py
    label: 长运行脚本
    purpose: 保持运行供测试取消。
    parameters:
      type: object
      additionalProperties: false
      properties: {}
---
保持运行。
""".strip()
    with zipfile.ZipFile(
        package_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        archive.writestr("SKILL.md", markdown)
        archive.writestr(
            "scripts/run.py",
            "import time\ntime.sleep(60)\n",
        )
    validated = SkillPackageValidator().validate(package_path)
    return package_path.read_bytes(), validated.manifest


def _dependency_check() -> DependencyCheck:
    """返回不要求第三方包的满足检查。"""
    return DependencyCheck(
        status=DependencyCheckStatus.SATISFIED,
        python_version=f"{sys.version_info.major}.{sys.version_info.minor}",
        python_implementation=sys.implementation.name,
        items=(),
        checked_at=_NOW,
    )


def _record(check: DependencyCheck) -> SkillExecutionRecord:
    """构造执行服务启动后返回的运行中审计记录。"""
    return SkillExecutionRecord(
        id="skexec_cancel",
        run_id="run_cancel",
        session_id="ses_cancel",
        tool_call_record_id=_CALL_RECORD_ID,
        skill_id="skill_cancel",
        skill_version_id="skillver_cancel",
        script_path="scripts/run.py",
        input_file_ids=(),
        parameters_sha256="0" * 64,
        python_version=check.python_version,
        dependency_check=check,
        status=SkillExecutionStatus.RUNNING,
        log_object_key=None,
        log_truncated=False,
        log_error_code=None,
        error_code=None,
        cleanup_error_code=None,
        result_artifacts=(),
        created_at=_NOW,
        finished_at=None,
    )


class _Skills:
    """返回固定版本和当前满足依赖的最小 SkillService 替身。"""

    def __init__(
        self,
        manifest: SkillPackageManifest,
        check: DependencyCheck,
    ) -> None:
        self.manifest = manifest
        self.check = check

    async def prepare_script_target(
        self,
        snapshot: RunSkillSnapshot,
        *,
        skill_id: str,
        script_path: str,
        parameters: dict[str, object],
    ) -> tuple[
        RunSkillEntry,
        SkillVersion,
        SkillScriptDefinition,
        DependencyCheck,
    ]:
        del parameters
        entry = snapshot.by_id(skill_id)
        assert entry is not None and script_path == "scripts/run.py"
        version = SkillVersion(
            id=entry.version_id,
            skill_id=entry.skill_id,
            ordinal=1,
            label=entry.version_label,
            manifest=self.manifest,
            object_key="skills/aa/cancel.zip",
            package_sha256="1" * 64,
            dependency_requirements=(),
            dependency_check=self.check,
            created_by=None,
            created_at=_NOW,
        )
        return entry, version, self.manifest.scripts[0], self.check


class _Files:
    """取消发生在产物上传前，因此只实现输入读取与预留接口。"""

    async def get_many(
        self,
        session_id: str,
        owner_id: str,
        file_ids: tuple[str, ...],
    ) -> tuple[object, ...]:
        del session_id, owner_id
        assert file_ids == ()
        return ()

    async def prepare_artifact(
        self,
        **kwargs: object,
    ) -> PendingSessionFile:
        raise AssertionError(f"取消路径不应准备产物: {kwargs}")

    async def abandon_artifacts(
        self,
        artifacts: tuple[PendingSessionFile, ...],
    ) -> None:
        del artifacts


class _Objects:
    """提供技能包并记录日志写入，不访问外部对象存储。"""

    def __init__(self, package: bytes) -> None:
        self.package = package

    async def get_bytes(self, key: str, *, maximum_bytes: int) -> bytes:
        assert key == "skills/aa/cancel.zip"
        assert maximum_bytes > len(self.package)
        return self.package

    async def put_bytes(
        self,
        key: str,
        content: bytes,
        *,
        content_type: str,
    ) -> None:
        del key, content, content_type

    async def delete(self, key: str) -> None:
        del key


class _Repository:
    """记录取消终态，模拟 SkillRepository 的执行附加表。"""

    def __init__(self, record: SkillExecutionRecord) -> None:
        self.record = record
        self.finish: dict[str, object] | None = None
        self.version: SkillVersion | None = None
        self.refreshed_version: SkillVersion | None = None
        self.started: dict[str, object] | None = None

    async def start_script_execution(self, **kwargs: object) -> SkillExecutionStart:
        self.started = dict(kwargs)
        return SkillExecutionStart(record=self.record, created=True)

    async def get_version(self, version_id: str) -> SkillVersion | None:
        if self.version is None or self.version.id != version_id:
            return None
        return self.version

    async def record_dependency_check(
        self,
        version_id: str,
        dependency_check: DependencyCheck,
    ) -> SkillVersion | None:
        if (
            self.refreshed_version is None
            or self.refreshed_version.id != version_id
            or self.refreshed_version.dependency_check != dependency_check
        ):
            return None
        return self.refreshed_version

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
    ) -> SkillExecutionRecord:
        self.finish = {
            "execution_id": execution_id,
            "status": status,
            "log_object_key": log_object_key,
            "log_truncated": log_truncated,
            "log_error_code": log_error_code,
            "error_code": error_code,
            "cleanup_error_code": cleanup_error_code,
            "artifacts": tuple(artifacts),
            "finished_at": finished_at,
        }
        return self.record

def test_execution_audit_uses_refreshed_target_check_and_version(
    tmp_path: Path,
) -> None:
    """环境变化但仍满足时，审计记录必须保存本次检查及刷新后的版本。"""
    package, manifest = _package_and_manifest(tmp_path)
    old_check = _dependency_check()
    refreshed_check = DependencyCheck(
        status=DependencyCheckStatus.SATISFIED,
        python_version="3.99.1",
        python_implementation="refreshed-target",
        items=(),
        checked_at=_NOW + timedelta(seconds=1),
    )
    old_version = SkillVersion(
        id="skillver_cancel",
        skill_id="skill_cancel",
        ordinal=1,
        label="v1",
        manifest=manifest,
        object_key="skills/aa/cancel.zip",
        package_sha256="1" * 64,
        dependency_requirements=(),
        dependency_check=old_check,
        created_by=None,
        created_at=_NOW,
    )
    refreshed_version = replace(old_version, dependency_check=refreshed_check)

    class Checker:
        def check(
            self,
            requirements: tuple[str, ...],
            *,
            checked_at: datetime,
        ) -> DependencyCheck:
            del requirements
            assert checked_at >= _NOW
            return refreshed_check

    repository = _Repository(_record(old_check))
    repository.version = old_version
    repository.refreshed_version = refreshed_version
    skills = SkillService(
        cast(SkillRepository, repository),
        cast(Any, _Objects(package)),
        cast(Any, None),
        dependency_checker=cast(Any, Checker()),
        clock=lambda: _NOW,
    )
    service = SkillExecutionService(
        skills=skills,
        files=cast(Any, _Files()),
        repository=cast(SkillRepository, repository),
        object_store=cast(Any, _Objects(package)),
        runner=cast(Any, _FailingRunner()),
        temp_root=tmp_path / "executions",
    )
    entry = RunSkillEntry(
        skill_id=old_version.skill_id,
        slug=manifest.slug,
        name=manifest.name,
        description=manifest.description,
        version_id=old_version.id,
        version_label=old_version.label,
        requires_approval=False,
    )
    context = ToolExecutionContext(
        owner_id="usr_cancel",
        chat_session_id="ses_cancel",
        project_id=None,
        run_id="run_cancel",
        execution_epoch=1,
        auth_session_id="auth_cancel",
        deadline_at=_NOW + timedelta(minutes=2),
        tool_call_record_id=_CALL_RECORD_ID,
        skill_snapshot=RunSkillSnapshot(
            skills=(entry,),
            selected_skill_ids=(entry.skill_id,),
        ),
    )

    result = asyncio.run(
        service.execute(
            {
                "skill_id": entry.skill_id,
                "script_path": "scripts/run.py",
                "parameters": {},
                "input_file_ids": [],
            },
            context,
        )
    )

    assert result.skill_execution_completion is not None
    assert repository.started is not None
    assert repository.started["dependency_check"] == refreshed_check
    assert repository.started["python_version"] == refreshed_check.python_version
    assert repository.started["skill_version_id"] == refreshed_version.id


class _FailingRunner:
    """不启动子进程，只返回确定的进程失败结果供审计接线测试。"""

    @asynccontextmanager
    async def execute(
        self,
        request: ScriptExecutionRequest,
    ) -> AsyncIterator[ScriptExecutionResult]:
        assert request.parameters == {}
        yield ScriptExecutionResult(
            exit_code=None,
            error_code=ScriptExecutionErrorCode.PROCESS_FAILED,
            outputs=(),
            stdout=b"",
            stderr=b"",
            log_truncated=False,
            process_group_id=None,
        )


@pytest.mark.skipif(
    os.name != "posix"
    or not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="真实进程身份核验需要 POSIX",
)
def test_service_cancellation_preserves_runner_directory_after_killpg_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """runner 已接管目录后 killpg 失败时，Service 不得抢删仍待恢复的目录。"""
    package, manifest = _package_and_manifest(tmp_path)
    check = _dependency_check()
    repository = _Repository(_record(check))
    service = SkillExecutionService(
        skills=cast(Any, _Skills(manifest, check)),
        files=cast(Any, _Files()),
        repository=cast(SkillRepository, repository),
        object_store=cast(Any, _Objects(package)),
        runner=PythonScriptExecutor(Path(sys.executable)),
        temp_root=tmp_path / "executions",
    )
    entry = RunSkillEntry(
        skill_id="skill_cancel",
        slug=manifest.slug,
        name=manifest.name,
        description=manifest.description,
        version_id="skillver_cancel",
        version_label="v1",
        requires_approval=False,
    )
    context = ToolExecutionContext(
        owner_id="usr_cancel",
        chat_session_id="ses_cancel",
        project_id=None,
        run_id="run_cancel",
        execution_epoch=1,
        auth_session_id="auth_cancel",
        deadline_at=_NOW + timedelta(minutes=2),
        tool_call_record_id=_CALL_RECORD_ID,
        skill_snapshot=RunSkillSnapshot(
            skills=(entry,),
            selected_skill_ids=(entry.skill_id,),
        ),
    )

    def reject_killpg(process_group_id: int, signal_number: int) -> None:
        del process_group_id, signal_number
        raise PermissionError("killpg denied for test")

    monkeypatch.setattr(os, "killpg", reject_killpg)

    async def scenario() -> None:
        execution = asyncio.create_task(
            service.execute(
                {
                    "skill_id": entry.skill_id,
                    "script_path": "scripts/run.py",
                    "parameters": {},
                    "input_file_ids": [],
                },
                context,
            )
        )
        diagnostic_path: Path | None = None
        deadline = asyncio.get_running_loop().time() + 3
        while diagnostic_path is None:
            directories = tuple((tmp_path / "executions").glob("skill-*"))
            for directory in directories:
                candidate = directory / SCRIPT_PROCESS_DIAGNOSTIC_FILENAME
                if candidate.is_file():
                    diagnostic_path = candidate
                    break
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("真实脚本诊断记录未出现")
            await asyncio.sleep(0.01)
        working_directory = diagnostic_path.parent
        execution.cancel()
        with pytest.raises(ScriptExecutionCancelled):
            await execution

        assert working_directory.is_dir()
        assert diagnostic_path.is_file()
        assert repository.finish is not None
        assert repository.finish["status"] is SkillExecutionStatus.UNKNOWN
        assert (
            repository.finish["cleanup_error_code"]
            == ScriptCleanupErrorCode.PROCESS_GROUP_CLEANUP_FAILED.value
        )

    asyncio.run(scenario())
