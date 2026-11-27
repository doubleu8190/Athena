"""P1-3a Skills 引擎治理与安全路径/输入写缓冲回归测试。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import zipfile
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO, cast

import pytest
from access_support import make_test_access_policy

from app.skill.execution_service import (
    _INPUT_WRITE_BUFFER_BYTES,
    SkillExecutionService,
    _open_exclusive_binary_0600,
)
from app.skill.package import SkillPackageValidator
from app.skill.personal_service import PersonalSkillService
from app.skill.runtime_reader import SkillRuntimeReader
from app.skill.service import (
    SkillAuthorizationError,
    SkillService,
    SkillUserAuthorizationError,
    _admin_markdown_version_fingerprint,
    _decode_utf8_resource_slice,
)
from common.safe_path import (
    reject_existing_symlink_components,
    resolve_safe_child_path,
    safe_relative_posix_parts,
)
from domain.file import PendingSessionFile, SessionFile, SessionFileSource
from domain.skill import (
    DependencyCheck,
    DependencyCheckStatus,
    RunSkillEntry,
    RunSkillSnapshot,
    SkillExecutionRecord,
    SkillExecutionStart,
    SkillExecutionStatus,
    SkillPackageError,
    SkillPackageManifest,
    SkillRepository,
    SkillScriptDefinition,
    SkillVersion,
)
from domain.skill.execution import (
    ScriptExecutionRequest,
    ScriptExecutionResult,
)
from domain.tool import ToolExecutionContext

_NOW = datetime(2026, 11, 27, tzinfo=UTC)
_CALL_RECORD_ID = f"toolcall_{'b' * 23}"


def test_safe_path_validation_and_symlink_rejection(tmp_path: Path) -> None:
    """校验 common/safe_path 的相对路径规则、尾部斜杠支持与 symlink 逐段拦截。"""
    assert safe_relative_posix_parts("scripts/run.py") == ("scripts", "run.py")
    assert safe_relative_posix_parts("scripts/", allow_trailing_slash=True) == ("scripts",)
    for invalid in (
        "",
        "/",
        "/etc/passwd",
        "scripts/",
        "scripts//run.py",
        "scripts/./run.py",
        "scripts/../run.py",
        "scripts\\run.py",
        "scripts/\x00run.py",
        "scripts/\nrun.py",
        "C:run.py",
        "C:/run.py",
    ):
        with pytest.raises(ValueError, match="safe_path_invalid"):
            safe_relative_posix_parts(invalid)

    for invalid_dir in ("", "/", "//", "scripts//", "./", "../", "C:/"):
        with pytest.raises(ValueError, match="safe_path_invalid"):
            safe_relative_posix_parts(invalid_dir, allow_trailing_slash=True)

    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, root / "linked_dir")

    class _CustomError(RuntimeError):
        pass

    with pytest.raises(_CustomError, match="safe_path_symlink_not_allowed"):
        reject_existing_symlink_components(
            root,
            ("linked_dir", "nested", "file.txt"),
            error_factory=_CustomError,
        )
    with pytest.raises(ValueError, match="safe_path_symlink_not_allowed"):
        resolve_safe_child_path(root, "linked_dir/file.txt")
    assert resolve_safe_child_path(root, "references/guide.md") == (
        root.resolve() / "references" / "guide.md"
    )


def test_skill_service_composition_and_compat_exports() -> None:
    """确认 SkillService 通过组合协作者提供服务且保留测试依赖的兼容导出。"""
    assert SkillAuthorizationError.__name__ == "SkillAuthorizationError"
    assert SkillUserAuthorizationError.__name__ == "SkillUserAuthorizationError"
    assert callable(_decode_utf8_resource_slice)
    assert callable(_admin_markdown_version_fingerprint)
    service = SkillService(
        cast(SkillRepository, object()),
        None,
        cast(Any, object()),
        access_policy=make_test_access_policy(),
    )
    assert isinstance(service._personal, PersonalSkillService)
    assert isinstance(service._runtime_reader, SkillRuntimeReader)


def _minimal_package(tmp_path: Path) -> tuple[bytes, SkillPackageManifest]:
    package_path = tmp_path / "pkg.zip"
    markdown = """
---
name: 缓冲测试
slug: buffer-test
description: 验证输入文件 1 MiB 写缓冲行为。
dependencies: []
scripts:
  - path: scripts/run.py
    label: 空跑脚本
    purpose: 验证输入写盘与完整性。
    parameters:
      type: object
      additionalProperties: false
      properties: {}
---
测试正文。
""".strip()
    with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("SKILL.md", markdown)
        archive.writestr("scripts/run.py", "print('ok')\n")
    validated = SkillPackageValidator().validate(package_path)
    return package_path.read_bytes(), validated.manifest


def test_prepare_input_files_buffers_small_chunks_and_preserves_order_and_integrity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """小块应先聚合成 1 MiB 批量写盘，混合大块时保持字节顺序与 SHA-256 严格一致。"""
    package_bytes, manifest = _minimal_package(tmp_path)
    small_chunks = [b"a" * 300_000, b"", b"b" * 300_000, b"c" * 600_000]
    large_chunk = b"L" * (_INPUT_WRITE_BUFFER_BYTES + 123)
    tail_chunk = b"z" * 42
    stream_chunks = [*small_chunks, large_chunk, tail_chunk]
    expected_payload = b"".join(stream_chunks)
    expected_sha256 = hashlib.sha256(expected_payload).hexdigest()

    input_file = SessionFile(
        id="file_buf_1",
        session_id="ses_buf",
        owner_id="usr_buf",
        source=SessionFileSource.UPLOAD,
        filename="big.bin",
        label=None,
        mime_type="application/octet-stream",
        size_bytes=len(expected_payload),
        sha256=expected_sha256,
        object_key="session-files/bb/big",
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
        id="skillver_buf",
        skill_id="skill_buf",
        ordinal=1,
        label="v1",
        manifest=manifest,
        object_key="skills/bb/pkg.zip",
        package_sha256=hashlib.sha256(package_bytes).hexdigest(),
        dependency_requirements=(),
        dependency_check=check,
        created_by=None,
        created_at=_NOW,
    )

    write_sizes: list[int] = []
    captured_bytes = bytearray()

    class _CountingWriter:
        def __init__(self, inner: BinaryIO) -> None:
            self._inner = inner

        def write(self, data: bytes) -> int:
            assert isinstance(data, bytes)
            write_sizes.append(len(data))
            captured_bytes.extend(data)
            return self._inner.write(data)

        def close(self) -> None:
            self._inner.close()

    def _open_counting(path: Path) -> BinaryIO:
        handle = _open_exclusive_binary_0600(path)
        if path.parent.name == "inputs":
            return cast(BinaryIO, _CountingWriter(handle))
        return handle

    monkeypatch.setattr(
        "app.skill.execution_service._open_exclusive_binary_0600",
        _open_counting,
    )

    class _SkillsStub:
        async def prepare_script_target(
            self, snapshot: RunSkillSnapshot, **kwargs: Any
        ) -> tuple[RunSkillEntry, SkillVersion, SkillScriptDefinition, DependencyCheck]:
            entry = snapshot.by_id(kwargs["skill_id"])
            assert entry is not None
            return entry, version, manifest.scripts[0], check

    class _FilesStub:
        async def get_many(self, *_args: Any) -> tuple[SessionFile, ...]:
            return (input_file,)

        async def prepare_artifact(self, **_kwargs: Any) -> PendingSessionFile:
            raise AssertionError("无输出文件")

        async def abandon_artifacts(self, _artifacts: tuple[PendingSessionFile, ...]) -> None:
            return None

    class _ObjectsStub:
        async def get_bytes(self, _key: str, *, maximum_bytes: int) -> bytes:
            assert maximum_bytes >= len(package_bytes)
            return package_bytes

        async def iter_range(self, key: str) -> AsyncIterator[bytes]:
            assert key == input_file.object_key
            for item in stream_chunks:
                yield item

        async def put_bytes(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    class _RunnerStub:
        @asynccontextmanager
        async def execute(
            self, request: ScriptExecutionRequest
        ) -> AsyncIterator[ScriptExecutionResult]:
            written_path = request.input_files["file_buf_1"]
            assert written_path.read_bytes() == expected_payload
            yield ScriptExecutionResult(
                exit_code=0,
                error_code=None,
                outputs=(),
                stdout=b"",
                stderr=b"",
                log_truncated=False,
                process_group_id=None,
            )

    class _RepoStub:
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

    service = SkillExecutionService(
        skills=cast(SkillService, _SkillsStub()),
        files=cast(Any, _FilesStub()),
        repository=cast(SkillRepository, _RepoStub()),
        object_store=cast(Any, _ObjectsStub()),
        runner=cast(Any, _RunnerStub()),
        temp_root=tmp_path / "exec-buf",
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
        owner_id="usr_buf",
        chat_session_id="ses_buf",
        project_id=None,
        run_id="run_buf",
        execution_epoch=1,
        auth_session_id="auth_buf",
        deadline_at=_NOW + timedelta(minutes=2),
        tool_call_record_id=_CALL_RECORD_ID,
        skill_snapshot=RunSkillSnapshot(skills=(entry,), selected_skill_ids=(entry.skill_id,)),
    )

    result = asyncio.run(
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
    assert result.outcome.status.value == "ok"
    assert bytes(captured_bytes) == expected_payload
    # 3 个非空小块（300K+300K+600K=1.2M）聚合成 1 次写盘，大块 1 次，尾部 42B 1 次。
    assert write_sizes == [1_200_000, len(large_chunk), len(tail_chunk)]


def test_package_validator_preserves_zip_path_error_code(tmp_path: Path) -> None:
    """SkillPackageValidator 复用 safe_path 后仍抛出 skill_zip_path_invalid。"""
    bad_zip = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad_zip, "w") as archive:
        archive.writestr("../escape.txt", "bad")
    with pytest.raises(SkillPackageError) as exc_info:
        SkillPackageValidator().validate(bad_zip)
    assert exc_info.value.code == "skill_zip_path_invalid"
