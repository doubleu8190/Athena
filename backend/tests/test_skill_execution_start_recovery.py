"""脚本执行记录提交成功但回包丢失时的幂等恢复测试。"""

from __future__ import annotations

import asyncio
import sys
import zipfile
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from app.skill.execution_service import SkillExecutionService
from app.skill.package import SkillPackageValidator
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
    ScriptExecutionRequest,
    ScriptExecutionResult,
)
from domain.tool import ToolExecutionContext, ToolOutcomeStatus

NOW = datetime(2026, 11, 12, tzinfo=UTC)
CALL_ID = "toolcall_start_recovery"


def _package_and_manifest(tmp_path: Path) -> tuple[bytes, SkillPackageManifest]:
    package_path = tmp_path / "skill.zip"
    markdown = """
---
name: 启动恢复
slug: start-recovery
description: 验证执行记录回包丢失后的幂等恢复。
dependencies: []
scripts:
  - path: scripts/run.py
    label: 启动恢复脚本
    purpose: 返回确定成功结果。
    parameters:
      type: object
      additionalProperties: false
      properties: {}
---
返回成功。
""".strip()
    with zipfile.ZipFile(package_path, "w") as archive:
        archive.writestr("SKILL.md", markdown)
        archive.writestr("scripts/run.py", 'print("ok")\n')
    validated = SkillPackageValidator().validate(package_path)
    return package_path.read_bytes(), validated.manifest


def _dependency_check() -> DependencyCheck:
    return DependencyCheck(
        status=DependencyCheckStatus.SATISFIED,
        python_version=f"{sys.version_info.major}.{sys.version_info.minor}",
        python_implementation=sys.implementation.name,
        items=(),
        checked_at=NOW,
    )


class _Skills:
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
            object_key="skills/aa/start-recovery.zip",
            package_sha256="a" * 64,
            dependency_requirements=(),
            dependency_check=self.check,
            created_by=None,
            created_at=NOW,
        )
        return entry, version, self.manifest.scripts[0], self.check


class _Files:
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
        raise AssertionError(f"该测试不生成产物: {kwargs}")

    async def abandon_artifacts(
        self,
        artifacts: tuple[PendingSessionFile, ...],
    ) -> None:
        del artifacts


class _Objects:
    def __init__(self, package: bytes) -> None:
        self.package = package

    async def get_bytes(self, key: str, *, maximum_bytes: int) -> bytes:
        assert key == "skills/aa/start-recovery.zip"
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


class _Runner:
    def __init__(self) -> None:
        self.calls = 0

    @asynccontextmanager
    async def execute(
        self,
        request: ScriptExecutionRequest,
    ) -> AsyncIterator[ScriptExecutionResult]:
        self.calls += 1
        assert request.parameters == {}
        yield ScriptExecutionResult(
            exit_code=0,
            error_code=None,
            outputs=(),
            stdout=b"ok\n",
            stderr=b"",
            log_truncated=False,
            process_group_id=None,
        )


class _Repository:
    def __init__(
        self,
        template: SkillExecutionRecord,
        *,
        start_error: Exception | None = None,
        lookup_error: Exception | None = None,
        commit_on_error: bool = True,
        mismatch: bool = False,
        created: bool = True,
    ) -> None:
        self.template = template
        self.start_error = start_error
        self.lookup_error = lookup_error
        self.commit_on_error = commit_on_error
        self.mismatch = mismatch
        self.created = created
        self.committed: SkillExecutionRecord | None = None
        self.lookup_calls = 0

    async def start_script_execution(
        self,
        **kwargs: object,
    ) -> SkillExecutionStart | None:
        record = replace(
            self.template,
            id=cast(str, kwargs["execution_id"]),
            run_id=cast(str, kwargs["run_id"]),
            session_id=cast(str, kwargs["session_id"]),
            tool_call_record_id=cast(str, kwargs["tool_call_record_id"]),
            skill_id=(
                "skill_mismatched"
                if self.mismatch
                else cast(str, kwargs["skill_id"])
            ),
            skill_version_id=cast(str, kwargs["skill_version_id"]),
            script_path=cast(str, kwargs["script_path"]),
            input_file_ids=cast(tuple[str, ...], kwargs["input_file_ids"]),
            parameters_sha256=cast(str, kwargs["parameters_sha256"]),
            python_version=cast(str, kwargs["python_version"]),
            dependency_check=cast(
                DependencyCheck,
                kwargs["dependency_check"],
            ),
            created_at=cast(datetime, kwargs["created_at"]),
        )
        self.committed = record
        if self.start_error is not None:
            if not self.commit_on_error:
                self.committed = None
            raise self.start_error
        return SkillExecutionStart(record=record, created=self.created)

    async def get_script_execution(
        self,
        tool_call_record_id: str,
    ) -> SkillExecutionRecord | None:
        self.lookup_calls += 1
        if self.lookup_error is not None:
            raise self.lookup_error
        if (
            self.committed is None
            or self.committed.tool_call_record_id != tool_call_record_id
        ):
            return None
        return self.committed

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
        del (
            execution_id,
            status,
            log_object_key,
            log_truncated,
            log_error_code,
            error_code,
            cleanup_error_code,
            artifacts,
            finished_at,
        )
        assert self.committed is not None
        return self.committed


def _context(entry: RunSkillEntry) -> ToolExecutionContext:
    return ToolExecutionContext(
        owner_id="usr_start_recovery",
        chat_session_id="ses_start_recovery",
        project_id=None,
        run_id="run_start_recovery",
        execution_epoch=1,
        auth_session_id="auth_start_recovery",
        deadline_at=NOW + timedelta(minutes=2),
        tool_call_record_id=CALL_ID,
        skill_snapshot=RunSkillSnapshot(
            skills=(entry,),
            selected_skill_ids=(entry.skill_id,),
        ),
    )


def _service(
    tmp_path: Path,
    repository: _Repository,
) -> tuple[SkillExecutionService, RunSkillEntry, _Runner]:
    package, manifest = _package_and_manifest(tmp_path)
    check = _dependency_check()
    entry = RunSkillEntry(
        skill_id=repository.template.skill_id,
        slug=manifest.slug,
        name=manifest.name,
        description=manifest.description,
        version_id=repository.template.skill_version_id,
        version_label="v1",
        requires_approval=False,
    )
    runner = _Runner()
    service = SkillExecutionService(
        skills=cast(Any, _Skills(manifest, check)),
        files=cast(Any, _Files()),
        repository=cast(SkillRepository, repository),
        object_store=cast(Any, _Objects(package)),
        runner=runner,
        temp_root=tmp_path / "executions",
        worker_instance_id="worker_start_recovery",
        clock=lambda: NOW,
    )
    return service, entry, runner


def _template(check: DependencyCheck) -> SkillExecutionRecord:
    return SkillExecutionRecord(
        id="skexec_template",
        run_id="run_start_recovery",
        session_id="ses_start_recovery",
        tool_call_record_id=CALL_ID,
        skill_id="skill_start_recovery",
        skill_version_id="skillver_start_recovery",
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
        created_at=NOW,
        finished_at=None,
    )


def test_start_response_loss_returns_unknown_without_reexecuting(tmp_path: Path) -> None:
    """数据库已提交但回包异常时，重查到 running 也只收敛为 UNKNOWN，不重跑。"""
    check = _dependency_check()
    repository = _Repository(
        _template(check),
        start_error=RuntimeError("response lost after commit"),
    )
    service, entry, runner = _service(tmp_path, repository)

    result = asyncio.run(
        service.execute(
            {
                "skill_id": entry.skill_id,
                "script_path": "scripts/run.py",
                "parameters": {},
                "input_file_ids": [],
            },
            _context(entry),
        )
    )

    assert repository.lookup_calls == 1
    assert repository.committed is not None
    assert runner.calls == 0
    assert result.outcome.status is ToolOutcomeStatus.UNKNOWN
    assert result.skill_execution_completion is not None
    assert result.skill_execution_completion.execution_id == repository.committed.id
    assert result.skill_execution_completion.status is SkillExecutionStatus.UNKNOWN


def test_start_lookup_failure_returns_unknown_without_fabricated_completion(
    tmp_path: Path,
) -> None:
    """提交结果与重查均不可确认时，不得猜测可能不存在的 execution_id。"""
    check = _dependency_check()
    repository = _Repository(
        _template(check),
        start_error=RuntimeError("response lost"),
        lookup_error=RuntimeError("database unavailable"),
    )
    service, entry, runner = _service(tmp_path, repository)

    result = asyncio.run(
        service.execute(
            {
                "skill_id": entry.skill_id,
                "script_path": "scripts/run.py",
                "parameters": {},
                "input_file_ids": [],
            },
            _context(entry),
        )
    )

    assert repository.lookup_calls == 1
    assert repository.committed is not None
    assert runner.calls == 0
    assert result.outcome.status is ToolOutcomeStatus.UNKNOWN
    assert result.outcome.code == "skill_execution_start_unknown"
    assert result.skill_execution_completion is None


def test_start_failure_with_confirmed_absence_returns_start_failed(
    tmp_path: Path,
) -> None:
    """重查明确未命中时才返回确定失败，不能误报 UNKNOWN。"""
    check = _dependency_check()
    repository = _Repository(
        _template(check),
        start_error=RuntimeError("insert rolled back"),
        commit_on_error=False,
    )
    service, entry, runner = _service(tmp_path, repository)

    result = asyncio.run(
        service.execute(
            {
                "skill_id": entry.skill_id,
                "script_path": "scripts/run.py",
                "parameters": {},
                "input_file_ids": [],
            },
            _context(entry),
        )
    )

    assert repository.lookup_calls == 1
    assert repository.committed is None
    assert runner.calls == 0
    assert result.outcome.status is ToolOutcomeStatus.ERROR
    assert result.outcome.code == "skill_execution_start_failed"
    assert result.skill_execution_completion is None


def test_start_lookup_mismatch_is_not_executed(tmp_path: Path) -> None:
    """重查记录与本次请求不一致时不得复用其审计身份执行脚本。"""
    check = _dependency_check()
    repository = _Repository(
        _template(check),
        start_error=RuntimeError("response lost"),
        mismatch=True,
    )
    service, entry, runner = _service(tmp_path, repository)

    result = asyncio.run(
        service.execute(
            {
                "skill_id": entry.skill_id,
                "script_path": "scripts/run.py",
                "parameters": {},
                "input_file_ids": [],
            },
            _context(entry),
        )
    )

    assert repository.committed is not None
    assert runner.calls == 0
    assert result.outcome.status is ToolOutcomeStatus.UNKNOWN
    assert result.skill_execution_completion is not None
    assert result.skill_execution_completion.execution_id == repository.committed.id


def test_start_lookup_terminal_record_is_not_reexecuted_or_overwritten(
    tmp_path: Path,
) -> None:
    """提交异常后重查到终态记录时，保留原审计且不启动 Runner。"""
    check = _dependency_check()
    repository = _Repository(
        replace(
            _template(check),
            status=SkillExecutionStatus.SUCCEEDED,
            finished_at=NOW,
        ),
        start_error=RuntimeError("response lost"),
    )
    service, entry, runner = _service(tmp_path, repository)
    result = asyncio.run(
        service.execute(
            {
                "skill_id": entry.skill_id,
                "script_path": "scripts/run.py",
                "parameters": {},
                "input_file_ids": [],
            },
            _context(entry),
        )
    )

    assert repository.committed is not None
    assert repository.committed.status is SkillExecutionStatus.SUCCEEDED
    assert runner.calls == 0
    assert result.outcome.status is ToolOutcomeStatus.UNKNOWN
    assert result.skill_execution_completion is None


@pytest.mark.parametrize(
    "status",
    (
        SkillExecutionStatus.RUNNING,
        SkillExecutionStatus.SUCCEEDED,
        SkillExecutionStatus.FAILED,
        SkillExecutionStatus.UNKNOWN,
    ),
)
def test_existing_execution_never_starts_runner(
    tmp_path: Path,
    status: SkillExecutionStatus,
) -> None:
    """running 和任何终态已有记录都不得再次启动脚本。"""
    check = _dependency_check()
    template = replace(_template(check), status=status)
    repository = _Repository(template, created=False)
    service, entry, runner = _service(tmp_path, repository)

    result = asyncio.run(
        service.execute(
            {
                "skill_id": entry.skill_id,
                "script_path": "scripts/run.py",
                "parameters": {},
                "input_file_ids": [],
            },
            _context(entry),
        )
    )

    assert runner.calls == 0
    assert result.outcome.status is ToolOutcomeStatus.UNKNOWN
    if status is SkillExecutionStatus.RUNNING:
        assert result.skill_execution_completion is not None
        assert repository.committed is not None
        assert (
            result.skill_execution_completion.execution_id
            == repository.committed.id
        )
    else:
        # 终态审计不能被 UNKNOWN completion 覆盖；工具调用单独按调用身份收敛。
        assert result.skill_execution_completion is None


def test_concurrent_same_tool_call_starts_runner_at_most_once(tmp_path: Path) -> None:
    """并发首次启动只有一个 created=True，其余只返回 UNKNOWN 审计结果。"""
    check = _dependency_check()
    template = _template(check)

    class ConcurrentRepository(_Repository):
        def __init__(self) -> None:
            super().__init__(template)
            self._lock = asyncio.Lock()
            self._winner: SkillExecutionRecord | None = None
            self.created_count = 0

        async def start_script_execution(
            self,
            **kwargs: object,
        ) -> SkillExecutionStart | None:
            async with self._lock:
                if self._winner is not None:
                    return SkillExecutionStart(
                        record=self._winner,
                        created=False,
                    )
                start = await super().start_script_execution(**kwargs)
                assert start is not None
                self._winner = start.record
                self.created_count += 1
                return start

    repository = ConcurrentRepository()
    service, entry, runner = _service(tmp_path, repository)
    context = _context(entry)
    arguments = {
        "skill_id": entry.skill_id,
        "script_path": "scripts/run.py",
        "parameters": {},
        "input_file_ids": [],
    }

    async def scenario() -> tuple[Any, ...]:
        return await asyncio.gather(
            service.execute(arguments, context),
            service.execute(arguments, context),
        )

    results = asyncio.run(scenario())

    assert repository.created_count == 1
    assert runner.calls == 1
    assert sum(result.outcome.status is ToolOutcomeStatus.OK for result in results) == 1
    unknown = next(
        result for result in results if result.outcome.status is ToolOutcomeStatus.UNKNOWN
    )
    assert unknown.skill_execution_completion is not None
    assert repository._winner is not None
    assert unknown.skill_execution_completion.execution_id == repository._winner.id
