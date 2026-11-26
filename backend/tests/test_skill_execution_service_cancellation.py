"""SkillExecutionService 对真实 runner 清理结论的持久化边界测试。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import threading
import zipfile
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO, cast

import pytest

from adapter.skill.execution import PythonScriptExecutor
from app.skill.execution_service import SkillExecutionService, _open_exclusive_binary_0600
from app.skill.package import SkillPackageValidator
from app.skill.service import SkillService
from domain.file import PendingSessionFile, SessionFile, SessionFileSource
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
    ScriptCleanupErrorCode,
    ScriptExecutionCancelled,
    ScriptExecutionErrorCode,
    ScriptExecutionRequest,
    ScriptExecutionResult,
)
from domain.tool import ToolExecutionContext

_NOW = datetime(2026, 11, 12, tzinfo=UTC)
_CALL_RECORD_ID = f"toolcall_{'a' * 23}"


def _package_and_manifest(
    tmp_path: Path,
    *,
    script_source: str = "import time\ntime.sleep(60)\n",
) -> tuple[bytes, SkillPackageManifest]:
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
            script_source,
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
def test_service_cancellation_records_killpg_failure_and_cleans_runner_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """runner 接管目录后 killpg 失败时，回退强杀直接子进程、清理目录并持久化清理错误码。"""
    script_source = (
        "import os, time\n"
        "from pathlib import Path\n"
        'ready = Path(os.environ["ATHENA_INPUT_PATH"]).parent / "ready.pid"\n'
        'ready.write_text(str(os.getpid()), encoding="utf-8")\n'
        "time.sleep(60)\n"
    )
    package, manifest = _package_and_manifest(tmp_path, script_source=script_source)
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
        ready_path: Path | None = None
        deadline = asyncio.get_running_loop().time() + 3
        while ready_path is None:
            directories = tuple((tmp_path / "executions").glob("skill-*"))
            for directory in directories:
                candidate = directory / "ready.pid"
                if candidate.is_file() and candidate.read_text(encoding="utf-8").strip():
                    ready_path = candidate
                    break
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("真实脚本启动标记未出现")
            await asyncio.sleep(0.01)
        working_directory = ready_path.parent
        child_pid = int(ready_path.read_text(encoding="utf-8").strip())
        execution.cancel()
        with pytest.raises(ScriptExecutionCancelled):
            await execution

        assert not working_directory.exists()
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        assert repository.finish is not None
        assert repository.finish["status"] is SkillExecutionStatus.UNKNOWN
        assert (
            repository.finish["cleanup_error_code"]
            == ScriptCleanupErrorCode.PROCESS_GROUP_CLEANUP_FAILED.value
        )

    asyncio.run(scenario())


def test_prepare_input_files_close_cancellation_propagates_without_starting_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入文件句柄 target.close() 阻塞期间收到取消时必须传播 CancelledError 且不启动 runner。"""
    package_bytes, manifest = _package_and_manifest(tmp_path)
    payload = b"authorized input payload"
    input_file = SessionFile(
        id="file_in_1",
        session_id="ses_cancel",
        owner_id="usr_cancel",
        source=SessionFileSource.UPLOAD,
        filename="notes.txt",
        label=None,
        mime_type="text/plain",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        object_key="session-files/aa/in1",
        source_call_id=None,
        output_index=None,
        created_at=_NOW,
    )
    check = DependencyCheck(
        status=DependencyCheckStatus.SATISFIED,
        python_version=f"{sys.version_info.major}.{sys.version_info.minor}",
        python_implementation=sys.implementation.name,
        items=(),
        checked_at=_NOW,
    )
    version = SkillVersion(
        id="skillver_close_cancel",
        skill_id="skill_close_cancel",
        ordinal=1,
        label="v1",
        manifest=manifest,
        object_key="skills/cc/pkg.zip",
        package_sha256=hashlib.sha256(package_bytes).hexdigest(),
        dependency_requirements=(),
        dependency_check=check,
        created_by=None,
        created_at=_NOW,
    )

    close_entered = threading.Event()
    close_release = threading.Event()
    close_finished = threading.Event()

    class _BlockingCloseWrapper:
        def __init__(self, inner: BinaryIO) -> None:
            self._inner = inner

        def write(self, data: bytes) -> int:
            return self._inner.write(data)

        def close(self) -> None:
            close_entered.set()
            try:
                close_release.wait(timeout=5.0)
                self._inner.close()
            finally:
                close_finished.set()

    def _open_with_blocking_input_close(path: Path) -> BinaryIO:
        handle = _open_exclusive_binary_0600(path)
        # 仅包装 inputs/ 下的会话授权输入文件句柄，保持技能包解压走原生 with 上下文。
        if path.parent.name == "inputs":
            return cast(BinaryIO, _BlockingCloseWrapper(handle))
        return handle

    monkeypatch.setattr(
        "app.skill.execution_service._open_exclusive_binary_0600",
        _open_with_blocking_input_close,
    )

    class _SkillsStub:
        async def prepare_script_target(self, snapshot: RunSkillSnapshot, **kwargs: Any) -> Any:
            entry = snapshot.by_id(kwargs["skill_id"])
            assert entry is not None
            return entry, version, manifest.scripts[0], check

    class _FilesStub:
        async def get_many(self, *_args: Any) -> tuple[SessionFile, ...]:
            return (input_file,)

    class _ObjectsStub:
        async def get_bytes(self, key: str, *, maximum_bytes: int) -> bytes:
            del key, maximum_bytes
            return package_bytes

        async def iter_range(self, key: str) -> AsyncIterator[bytes]:
            assert key == input_file.object_key
            yield payload

    runner_started = False

    class _ForbiddenRunner:
        @asynccontextmanager
        async def execute(
            self, _request: ScriptExecutionRequest
        ) -> AsyncIterator[ScriptExecutionResult]:
            nonlocal runner_started
            runner_started = True
            raise AssertionError("runner must not start after cancellation during close()")
            yield  # pragma: no cover

    class _RepoStub:
        def __init__(self) -> None:
            self.finish: dict[str, Any] | None = None

        async def start_script_execution(self, **kwargs: Any) -> SkillExecutionStart:
            return SkillExecutionStart(
                record=SkillExecutionRecord(
                    id=kwargs["execution_id"],
                    run_id=kwargs["run_id"],
                    session_id=kwargs["session_id"],
                    tool_call_record_id=kwargs["tool_call_record_id"],
                    skill_id=kwargs["skill_id"],
                    skill_version_id=kwargs["skill_version_id"],
                    script_path=kwargs["script_path"],
                    input_file_ids=kwargs["input_file_ids"],
                    parameters_sha256=kwargs["parameters_sha256"],
                    python_version=kwargs["python_version"],
                    dependency_check=kwargs["dependency_check"],
                    status=SkillExecutionStatus.RUNNING,
                    log_object_key=None,
                    log_truncated=False,
                    log_error_code=None,
                    error_code=None,
                    cleanup_error_code=None,
                    result_artifacts=(),
                    created_at=kwargs["created_at"],
                    finished_at=None,
                ),
                created=True,
            )

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
        ) -> None:
            del execution_id, log_object_key, log_truncated, log_error_code, artifacts, finished_at
            self.finish = {
                "status": status,
                "error_code": error_code,
                "cleanup_error_code": cleanup_error_code,
            }

    temp_root = tmp_path / "close-cancel-exec"
    repo = _RepoStub()
    service = SkillExecutionService(
        skills=cast(SkillService, _SkillsStub()),
        files=cast(Any, _FilesStub()),
        repository=cast(SkillRepository, repo),
        object_store=cast(Any, _ObjectsStub()),
        runner=cast(Any, _ForbiddenRunner()),
        temp_root=temp_root,
        clock=lambda: _NOW,
    )
    entry = RunSkillEntry(
        skill_id=version.skill_id,
        slug=manifest.slug,
        name=manifest.name,
        description=manifest.description,
        version_id=version.id,
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
        skill_snapshot=RunSkillSnapshot(skills=(entry,), selected_skill_ids=(entry.skill_id,)),
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            service.execute(
                {
                    "skill_id": entry.skill_id,
                    "script_path": "scripts/run.py",
                    "parameters": {},
                    "input_file_ids": [input_file.id],
                },
                context,
            )
        )
        try:
            deadline = asyncio.get_running_loop().time() + 5.0
            while not close_entered.is_set():
                if task.done():
                    raise AssertionError(
                        f"任务在进入输入文件 target.close() 前提前结束: {task.result()!r}"
                    )
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("等待进入输入文件 target.close() 超时")
                await asyncio.sleep(0.005)
            task.cancel()
            await asyncio.sleep(0.02)
            # 句柄尚未完成关闭前，任务不得提前退出或抢删目录。
            assert not task.done()
            close_release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert close_finished.is_set()
            assert runner_started is False
            assert list(temp_root.glob("skill-*")) == []
            assert repo.finish is not None
            assert repo.finish["status"] is SkillExecutionStatus.UNKNOWN
            assert repo.finish["error_code"] == "skill_execution_cancelled"
        finally:
            close_release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
