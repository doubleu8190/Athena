"""准备固定 Skill 工作目录并通过领域端口执行登记脚本。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import stat
import sys
import tempfile
import threading
import zipfile
from collections.abc import Callable, Mapping
from datetime import datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, cast
from uuid import uuid4

from app.file.service import SessionFileService
from app.skill.errors import public_skill_error_code
from app.skill.package_cache import SkillPackageCache, resolve_safe_child_path
from app.skill.service import SkillService
from common.async_utils import await_task_shielded, run_thread_shielded
from common.date import utc_now
from common.ids import new_id
from domain.file import FileObjectStore, PendingSessionFile, SessionFile
from domain.skill import (
    MAX_PACKAGE_FILE_BYTES,
    MAX_PACKAGE_FILES,
    MAX_PACKAGE_UNCOMPRESSED_BYTES,
    MAX_PACKAGE_ZIP_BYTES,
    DependencyCheck,
    SkillExecutionCompletion,
    SkillExecutionRecord,
    SkillExecutionStatus,
    SkillNotFoundError,
    SkillPackageManifest,
    SkillRepository,
    SkillVersion,
)
from domain.skill.execution import (
    JsonValue,
    ScriptExecutionPort,
    ScriptExecutionRequest,
    ScriptExecutionResult,
)
from domain.tool import (
    ToolExecutionContext,
    ToolExecutionResult,
    ToolOutcome,
    ToolOutcomeStatus,
)

logger = logging.getLogger(__name__)


class _DirectoryPreparationCancelled(Exception):
    """后台工作目录准备线程收到协同取消信号。"""


class SkillExecutionService:
    """在平台临时目录中准备可信脚本输入并上传显式交付文件。"""

    def __init__(
        self,
        *,
        skills: SkillService,
        files: SessionFileService,
        repository: SkillRepository,
        object_store: FileObjectStore,
        runner: ScriptExecutionPort,
        temp_root: Path | None = None,
        package_cache: SkillPackageCache | None = None,
        worker_instance_id: str | None = None,
        id_factory: Callable[[str], str] = new_id,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """保存固定版本、文件存储、可选本地包缓存、审计和执行端口。"""
        self._skills = skills
        self._files = files
        self._repository = repository
        self._object_store = object_store
        self._runner = runner
        self._temp_root = temp_root or (Path(tempfile.gettempdir()) / "athena-skill-executions")
        self._package_cache = package_cache
        self._worker_instance_id = worker_instance_id or str(uuid4())
        self._active_working_directories: set[Path] = set()
        self._id_factory = id_factory
        self._clock = clock

    async def execute(
        self,
        arguments: Mapping[str, Any],
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        """执行一次已批准调用；临时文件仅在 runner 上下文作用域内上传。"""
        snapshot = context.skill_snapshot
        call_id = context.tool_call_record_id
        if snapshot is None:
            return _error("skill_snapshot_unavailable", "当前 Run 没有固定 Skill 目录。")
        if call_id is None:
            return _error("skill_tool_call_context_missing", "脚本调用缺少持久化身份。")
        parsed = _parse_execution_arguments(arguments)
        if parsed is None:
            return _error("skill_parameters_invalid", "脚本调用参数结构不合法。")
        skill_id, script_path, parameters, input_file_ids = parsed
        try:
            entry, version, script, dependency_check = await self._skills.prepare_script_target(
                snapshot,
                skill_id=skill_id,
                script_path=script_path,
                parameters=parameters,
            )
            if version.object_key is None:
                # 纯 Markdown 版本不含对象存储 ZIP 包，显式拒绝进入脚本解包执行链路。
                raise SkillNotFoundError("skill_version_unavailable")
            object_key = version.object_key
            files = await self._files.get_many(
                context.chat_session_id,
                context.owner_id,
                input_file_ids,
            )
        except Exception as error:
            return _safe_preparation_error(error)

        execution_id = self._id_factory("skexec")
        parameters_sha256 = hashlib.sha256(
            json.dumps(
                parameters,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        try:
            start = await self._repository.start_script_execution(
                execution_id=execution_id,
                run_id=context.run_id,
                session_id=context.chat_session_id,
                tool_call_record_id=call_id,
                skill_id=entry.skill_id,
                skill_version_id=version.id,
                script_path=script_path,
                input_file_ids=input_file_ids,
                parameters_sha256=parameters_sha256,
                python_version=dependency_check.python_version,
                dependency_check=dependency_check,
                created_at=self._clock(),
            )
        except Exception as start_error:
            # 提交回包丢失时按工具调用身份重查；即使重查到本次新建的 running，
            # 也不能再次启动 Runner，统一由审计收敛为 UNKNOWN。
            try:
                record = await self._repository.get_script_execution(call_id)
            except Exception as lookup_error:
                logger.warning(
                    "skill_execution_start_unknown call_id=%s exception_type=%s "
                    "lookup_exception_type=%s",
                    call_id,
                    type(start_error).__name__,
                    type(lookup_error).__name__,
                )
                # 回查失败时没有可信 execution_id；不能伪造审计完成记录，
                # 由 executor 的 UNKNOWN 收敛按 tool_call_record_id 处理 running 审计。
                return _unknown_start_result()
            if record is None:
                return _error("skill_execution_start_failed", "脚本执行记录创建失败。")
            if not _execution_start_matches(
                record,
                run_id=context.run_id,
                session_id=context.chat_session_id,
                tool_call_record_id=call_id,
                skill_id=entry.skill_id,
                skill_version_id=version.id,
                script_path=script_path,
                input_file_ids=input_file_ids,
                parameters_sha256=parameters_sha256,
                python_version=dependency_check.python_version,
                dependency_check=dependency_check,
            ):
                logger.warning(
                    "skill_execution_start_conflict call_id=%s execution_id=%s",
                    call_id,
                    record.id,
                )
                return _unknown_existing_start_result(record, self._clock())
            logger.warning(
                "skill_execution_start_recovered call_id=%s execution_id=%s exception_type=%s",
                call_id,
                record.id,
                type(start_error).__name__,
            )
            return _unknown_existing_start_result(record, self._clock())
        if start is None:
            return _error("skill_execution_start_failed", "脚本执行记录创建失败。")
        if not start.created:
            # 已有记录可能来自并发请求或更早的重试；任何已有审计都不得再次执行。
            logger.warning(
                "skill_execution_start_existing call_id=%s execution_id=%s",
                call_id,
                start.record.id,
            )
            return _unknown_existing_start_result(start.record, self._clock())
        record = start.record

        working_directory: Path | None = None
        created_directory_holder: list[Path] = []
        runner_owns_working_directory = False
        uploaded_artifacts: tuple[PendingSessionFile, ...] = ()
        uploaded_log_key: str | None = None
        uploaded_log_truncated = False
        log_error_code: str | None = None
        try:
            # 统一经有界内存 ZIP 字节缓存或对象存储获取包体，再单路径解包至 0o600 独占工作目录。
            if self._package_cache is not None:
                package = await self._package_cache.get_zip_bytes(
                    version,
                    fetch_bytes=lambda: self._object_store.get_bytes(
                        object_key,
                        maximum_bytes=MAX_PACKAGE_ZIP_BYTES,
                    ),
                )
            else:
                package = await self._object_store.get_bytes(
                    object_key,
                    maximum_bytes=MAX_PACKAGE_ZIP_BYTES,
                )
            working_directory = await self._create_working_directory_async(
                call_id,
                created_directory_holder,
            )
            self._active_working_directories.add(working_directory)
            await self._prepare_directory(
                working_directory,
                package=package,
                version=version,
                input_files=files,
            )
            request = ScriptExecutionRequest(
                working_directory=working_directory,
                script_path=script_path,
                parameters=cast(Mapping[str, JsonValue], parameters),
                input_files={
                    file.id: working_directory / "inputs" / f"{index:02d}-{file.filename}"
                    for index, file in enumerate(files)
                },
                output_directory=working_directory / "outputs",
                run_id=context.run_id,
                tool_call_id=call_id,
                worker_instance_id=self._worker_instance_id,
            )
            script_result: ScriptExecutionResult
            # 从这里开始目录生命周期由 runner 负责；即使取消发生在
            # __aenter__，也必须尊重 runner 携带的清理结论，不能由 Service 抢删。
            runner_owns_working_directory = True
            async with self._runner.execute(request) as script_result:
                uploaded_log_key, log_error_code = await self._upload_logs(
                    record,
                    call_id,
                    script_result,
                )
                uploaded_log_truncated = script_result.log_truncated
                if script_result.succeeded:
                    uploaded_artifacts = await self._upload_artifacts(
                        call_id,
                        script_result,
                    )
        except asyncio.CancelledError as error:
            # 取消语义不能被审计落库覆盖；runner 会把进程、管道和目录清理结论
            # 放在该异常上，取消路径也必须原样持久化，供恢复扫描和排查使用。
            target_directory = working_directory or (
                created_directory_holder[0] if created_directory_holder else None
            )
            runner_cleanup_error = _runner_cleanup_error_code(error)
            if runner_owns_working_directory:
                local_cleanup_error = None
            else:
                local_cleanup_error, _ = await self._remove_unentered_directory_async(
                    target_directory
                )
            await self._finish_cancelled_execution(
                record,
                log_object_key=uploaded_log_key,
                log_truncated=uploaded_log_truncated,
                log_error_code=log_error_code,
                error_code="skill_execution_cancelled",
                cleanup_error_code=runner_cleanup_error or local_cleanup_error,
            )
            raise
        except Exception as error:
            logger.warning(
                "skill_execution_persistence_failed execution_id=%s exception_type=%s safe_code=%s",
                record.id,
                type(error).__name__,
                public_skill_error_code(
                    error,
                    fallback="skill_execution_internal_failed",
                ),
            )
            target_directory = working_directory or (
                created_directory_holder[0] if created_directory_holder else None
            )
            runner_cleanup_error = _runner_cleanup_error_code(error)
            if runner_owns_working_directory:
                local_cleanup_error = None
            else:
                (
                    local_cleanup_error,
                    cleanup_cancelled,
                ) = await self._remove_unentered_directory_async(target_directory)
                if cleanup_cancelled is not None:
                    await self._finish_cancelled_execution(
                        record,
                        log_object_key=uploaded_log_key,
                        log_truncated=uploaded_log_truncated,
                        log_error_code=log_error_code,
                        error_code="skill_execution_cancelled",
                        cleanup_error_code=runner_cleanup_error or local_cleanup_error,
                    )
                    raise cleanup_cancelled from None
            cleanup_error_code = runner_cleanup_error or local_cleanup_error
            completion_error_code = (
                "skill_artifact_invalid"
                if uploaded_log_key is not None
                else "skill_execution_failed"
            )
            completion = SkillExecutionCompletion(
                execution_id=record.id,
                status=SkillExecutionStatus.FAILED,
                log_object_key=uploaded_log_key,
                log_truncated=uploaded_log_truncated,
                log_error_code=log_error_code,
                error_code=completion_error_code,
                cleanup_error_code=cleanup_error_code,
                finished_at=self._clock(),
            )
            return ToolExecutionResult(
                outcome=_error(
                    completion_error_code,
                    "脚本产物校验或持久化失败，未交付任何文件。",
                ).outcome,
                skill_execution_completion=completion,
            )
        finally:
            if working_directory is not None:
                self._active_working_directories.discard(working_directory)
            elif created_directory_holder:
                self._active_working_directories.discard(created_directory_holder[0])

        cleanup_error_code = script_result.cleanup_error_code
        status = (
            SkillExecutionStatus.SUCCEEDED
            if script_result.succeeded
            else (
                SkillExecutionStatus.UNKNOWN
                if script_result.error_code
                in {
                    # 进程启动/等待失败无法确认是否已产生外部效果。
                    "script_process_start_failed",
                    "script_process_failed",
                    "script_process_cleanup_failed",
                }
                else SkillExecutionStatus.FAILED
            )
        )
        outcome_status = (
            ToolOutcomeStatus.OK
            if status is SkillExecutionStatus.SUCCEEDED
            else (
                ToolOutcomeStatus.UNKNOWN
                if status is SkillExecutionStatus.UNKNOWN
                else ToolOutcomeStatus.ERROR
            )
        )
        error_code = None if script_result.error_code is None else script_result.error_code.value
        completion = SkillExecutionCompletion(
            execution_id=record.id,
            status=status,
            log_object_key=uploaded_log_key,
            log_truncated=script_result.log_truncated,
            log_error_code=log_error_code,
            error_code=error_code,
            cleanup_error_code=cleanup_error_code,
            finished_at=self._clock(),
        )
        outcome = ToolOutcome(
            status=outcome_status,
            code=(
                "skill_execution_succeeded"
                if outcome_status is ToolOutcomeStatus.OK
                else (error_code or "skill_execution_failed")
            ),
            message=(
                "脚本执行成功。"
                if outcome_status is ToolOutcomeStatus.OK
                else _script_failure_message(error_code)
            ),
            data={
                "skill_id": entry.skill_id,
                "skill_version_id": version.id,
                "artifact_count": len(uploaded_artifacts),
                "log_truncated": script_result.log_truncated,
            },
            cleanup_error_code=cleanup_error_code,
        )
        return ToolExecutionResult(
            outcome=outcome,
            artifacts=uploaded_artifacts,
            skill_execution_completion=completion,
        )

    async def _create_working_directory_async(
        self,
        call_id: str,
        created_directory_holder: list[Path],
    ) -> Path:
        """在工作线程创建独占目录；若中途取消则等待创建线程退出并登记目录供统一清理。"""
        task = asyncio.create_task(
            asyncio.to_thread(self._create_working_directory, call_id),
            name=f"athena-skill-mkdir-{call_id[:16]}",
        )
        cancelled = await await_task_shielded(task)
        if cancelled is not None:
            if not task.cancelled() and task.exception() is None:
                created_directory_holder.append(task.result())
            raise cancelled
        directory = task.result()
        created_directory_holder.append(directory)
        return directory

    def _create_working_directory(self, call_id: str) -> Path:
        """在部署指定根目录下创建权限收紧的唯一工作目录。"""
        self._temp_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = Path(
            tempfile.mkdtemp(
                prefix=f"skill-{call_id[:32]}-",
                dir=self._temp_root,
            )
        )
        os.chmod(directory, 0o700)
        # runner 会按真实路径确认所有子路径都在工作根内；提前消除 /tmp 等
        # 宿主符号链接，避免同一逻辑目录因词法路径不同而被安全校验拒绝。
        return directory.resolve(strict=True)

    async def _prepare_directory(
        self,
        working_directory: Path,
        *,
        package: bytes,
        version: SkillVersion,
        input_files: tuple[SessionFile, ...],
    ) -> None:
        """只解压清单声明的 Skill 文件与当前会话授权输入（全部磁盘 I/O 下放工作线程）。"""
        cancel_event = threading.Event()
        # 通知后台线程尽快中止并在 shield 中等待其退出，杜绝并发写删工作目录竞态。
        await run_thread_shielded(
            _extract_package_files_sync,
            working_directory,
            package=package,
            version=version,
            cancel_event=cancel_event,
            name="athena-skill-prepare-zip",
            on_cancel=cancel_event.set,
        )
        await self._prepare_input_files(working_directory, input_files)

    async def _prepare_input_files(
        self,
        working_directory: Path,
        input_files: tuple[SessionFile, ...],
    ) -> None:
        """在工作线程打开、写入并关闭会话授权输入文件，取消时先收敛当前写线程再关闭句柄。"""
        inputs = working_directory / "inputs"
        for index, file in enumerate(input_files):
            destination = inputs / f"{index:02d}-{file.filename}"
            digest = hashlib.sha256()
            size = 0
            target = await run_thread_shielded(
                _open_exclusive_binary_0600,
                destination,
                name="athena-skill-input-open",
            )
            try:
                async for chunk in self._object_store.iter_range(file.object_key):
                    size += len(chunk)
                    await run_thread_shielded(
                        _write_and_hash_chunk,
                        target,
                        chunk,
                        digest,
                        name="athena-skill-input-write",
                    )
            finally:
                # 无论正常完成还是取消，均在工作线程安全关闭句柄后再释放目录；
                # 无活跃异常时保留关闭期间收到的取消信号或关闭失败，避免静默吞噬。
                active_error = sys.exc_info()[1]
                close_task = asyncio.create_task(
                    asyncio.to_thread(target.close),
                    name="athena-skill-input-close",
                )
                close_cancelled = await await_task_shielded(close_task)
                if active_error is None:
                    if close_cancelled is not None:
                        raise close_cancelled
                    close_task.result()
                elif not close_task.cancelled():
                    close_task.exception()
            if size != file.size_bytes or digest.hexdigest() != file.sha256:
                raise ValueError("session_file_integrity_invalid")

    async def _upload_logs(
        self,
        record: SkillExecutionRecord,
        call_id: str,
        result: ScriptExecutionResult,
    ) -> tuple[str | None, str | None]:
        """日志失败只产生管理员审计错误码，不反转脚本业务结果。"""
        content = result.stdout + result.stderr
        digest = hashlib.sha256(call_id.encode("utf-8")).hexdigest()
        key = f"skill-logs/{digest[:2]}/{digest}/{record.id}.log"
        try:
            await self._object_store.put_bytes(
                key,
                content,
                content_type="text/plain; charset=utf-8",
            )
        except Exception:
            await self._abandon_log_object(key)
            return None, "skill_log_persist_failed"
        return key, None

    async def _upload_artifacts(
        self,
        call_id: str,
        result: ScriptExecutionResult,
    ) -> tuple[PendingSessionFile, ...]:
        """按清单顺序校验类型并上传全部文件；任一失败整组不返回。"""
        artifacts: list[PendingSessionFile] = []
        try:
            for index, output in enumerate(result.outputs):
                artifacts.append(
                    await self._files.prepare_artifact(
                        source_call_id=call_id,
                        output_index=index,
                        path=output.source_path,
                        filename=PurePosixPath(output.path).name,
                        label=output.label,
                    )
                )
        except BaseException:
            # 当前适配器在返回值前已完成目录清理；已上传前缀在这里显式放弃，
            # 不把整组失败留给保留期扫描。
            await self._files.abandon_artifacts(tuple(artifacts))
            raise
        return tuple(artifacts)

    async def _abandon_log_object(self, key: str) -> None:
        """日志写入明确失败时删除已知对象。"""
        try:
            await self._object_store.delete(key)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _best_effort_finish(
        self,
        record: SkillExecutionRecord,
        *,
        status: SkillExecutionStatus,
        log_object_key: str | None,
        log_truncated: bool,
        log_error_code: str | None,
        error_code: str | None,
        cleanup_error_code: str | None,
        artifacts: tuple[PendingSessionFile, ...],
    ) -> None:
        try:
            await self._repository.finish_script_execution(
                record.id,
                status=status,
                log_object_key=log_object_key,
                log_truncated=log_truncated,
                log_error_code=log_error_code,
                error_code=error_code,
                cleanup_error_code=cleanup_error_code,
                artifacts=artifacts,
                finished_at=self._clock(),
            )
        except Exception:
            return

    async def _finish_cancelled_execution(
        self,
        record: SkillExecutionRecord,
        *,
        log_object_key: str | None,
        log_truncated: bool,
        log_error_code: str | None,
        error_code: str,
        cleanup_error_code: str | None,
    ) -> None:
        """取消后尽力独立落审计终态，同时继续等待原始取消传播。"""
        task = asyncio.create_task(
            self._best_effort_finish(
                record,
                status=SkillExecutionStatus.UNKNOWN,
                log_object_key=log_object_key,
                log_truncated=log_truncated,
                log_error_code=log_error_code,
                error_code=error_code,
                cleanup_error_code=cleanup_error_code,
                artifacts=(),
            ),
            name=f"athena-skill-cancel-finish-{record.id}",
        )
        # 上层可能再次投递取消；shield 保持审计任务完成，原始取消仍由调用方抛出。
        await await_task_shielded(task)
        if not task.cancelled() and task.exception() is None:
            task.result()

    async def _remove_unentered_directory_async(
        self,
        path: Path | None,
    ) -> tuple[str | None, asyncio.CancelledError | None]:
        """在工作线程异步清理未移交 runner 的工作目录，套用抗二次取消盾护循环。"""
        if path is None:
            return None, None
        task = asyncio.create_task(
            asyncio.to_thread(self._remove_unentered_directory, path),
            name="athena-skill-remove-unentered",
        )
        cancelled = await await_task_shielded(task)
        try:
            return task.result(), cancelled
        except asyncio.CancelledError as error:
            return "script_working_directory_cleanup_failed", cancelled or error
        except Exception:
            return "script_working_directory_cleanup_failed", cancelled

    @staticmethod
    def _remove_unentered_directory(path: Path | None) -> str | None:
        """runner 未进入上下文时清理本服务刚创建的目录。"""
        if path is None:
            return None
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            return None
        except OSError:
            return "script_working_directory_cleanup_failed"
        return None


def _open_exclusive_binary_0600(destination: Path) -> BinaryIO:
    """原子独占创建 0o600 常规文件并返回二进制写入句柄。"""
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.chmod(destination, 0o600)
        return os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        raise


def _write_and_hash_chunk(
    target: BinaryIO,
    chunk: bytes,
    digest: Any,
) -> None:
    """在工作线程合并写盘与 SHA-256 增量摘要，避免大块数据阻塞事件循环。"""
    digest.update(chunk)
    target.write(chunk)


def _extract_package_files_sync(
    working_directory: Path,
    *,
    package: bytes,
    version: SkillVersion,
    cancel_event: threading.Event,
) -> None:
    """在工作线程流式解压至 0o600 独占目录，执行路径穿越、有界大小与逐成员 SHA-256 校验。"""
    manifest = SkillPackageManifest.model_validate(version.manifest)
    if len(manifest.files) > MAX_PACKAGE_FILES:
        raise ValueError("skill_package_integrity_invalid")
    resolved_work = working_directory.resolve(strict=True)
    total_written = 0
    try:
        with zipfile.ZipFile(BytesIO(package)) as archive:
            for resource in manifest.files:
                if cancel_event.is_set():
                    raise _DirectoryPreparationCancelled()
                member = archive.getinfo(resource.path)
                if member.flag_bits & 0x1:
                    raise ValueError("skill_package_integrity_invalid")
                mode = (member.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(mode)
                if file_type and not stat.S_ISREG(file_type):
                    raise ValueError("skill_package_integrity_invalid")
                destination = resolve_safe_child_path(resolved_work, resource.path)
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if not destination.parent.resolve(strict=True).is_relative_to(resolved_work):
                    raise ValueError("skill_package_integrity_invalid")
                digest = hashlib.sha256()
                written = 0
                with (
                    archive.open(member, "r") as source,
                    _open_exclusive_binary_0600(destination) as target,
                ):
                    while chunk := source.read(128 * 1024):
                        if cancel_event.is_set():
                            raise _DirectoryPreparationCancelled()
                        written += len(chunk)
                        total_written += len(chunk)
                        if (
                            written > resource.size_bytes
                            or written > MAX_PACKAGE_FILE_BYTES
                            or total_written > MAX_PACKAGE_UNCOMPRESSED_BYTES
                        ):
                            raise ValueError("skill_package_integrity_invalid")
                        digest.update(chunk)
                        target.write(chunk)
                if written != resource.size_bytes or digest.hexdigest() != resource.sha256:
                    raise ValueError("skill_package_integrity_invalid")
        if cancel_event.is_set():
            raise _DirectoryPreparationCancelled()
        (resolved_work / "inputs").mkdir(mode=0o700)
        (resolved_work / "outputs").mkdir(mode=0o700)
    except _DirectoryPreparationCancelled:
        raise
    except (KeyError, OSError, zipfile.BadZipFile, ValueError) as error:
        raise ValueError("skill_package_integrity_invalid") from error


def _parse_execution_arguments(
    arguments: Mapping[str, Any],
) -> tuple[str, str, dict[str, Any], tuple[str, ...]] | None:
    """从已通过 tool JSON Schema 的参数中恢复强类型调用。"""
    skill_id = arguments.get("skill_id")
    script_path = arguments.get("script_path")
    parameters = arguments.get("parameters")
    input_file_ids = arguments.get("input_file_ids", [])
    if (
        not isinstance(skill_id, str)
        or not isinstance(script_path, str)
        or not isinstance(parameters, dict)
        or not isinstance(input_file_ids, list)
        or any(not isinstance(item, str) for item in input_file_ids)
        or len(input_file_ids) != len(set(input_file_ids))
    ):
        return None
    return skill_id, script_path, parameters, tuple(input_file_ids)


def _safe_preparation_error(error: Exception) -> ToolExecutionResult:
    """把目录、依赖和文件授权失败映射为稳定用户错误。"""
    code = public_skill_error_code(error, fallback="skill_preparation_failed")
    messages = {
        "skill_not_in_run_snapshot": "脚本不属于当前 Run 固定的 Skill 目录。",
        "skill_version_unavailable": "当前 Run 固定的 Skill 版本不可用。",
        "skill_script_not_registered": "请求的脚本未在 Skill 中登记。",
        "skill_parameters_invalid": "脚本参数不符合登记结构。",
        "skill_dependency_unsatisfied": "执行环境不满足 Skill 依赖。",
        "session_file_not_found": "脚本输入文件不存在或当前无权访问。",
        "session_file_ids_duplicate": "脚本输入文件 ID 不能重复。",
    }
    return _error(code, messages.get(code, "脚本调用准备失败。"))


def _runner_cleanup_error_code(error: BaseException) -> str | None:
    """读取显式字段，并兼容 runner 对原异常追加的稳定清理 note。"""
    explicit = cast(str | None, getattr(error, "cleanup_error_code", None))
    if explicit:
        return explicit
    prefix = "script_cleanup_error_codes="
    for note in getattr(error, "__notes__", ()):
        if not isinstance(note, str) or not note.startswith(prefix):
            continue
        values = note[len(prefix) :].split(",")
        if values and values[0]:
            return values[0]
    return None


def _execution_start_matches(
    record: SkillExecutionRecord,
    *,
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
) -> bool:
    """响应丢失后的重查记录必须与本次启动请求逐字段一致。"""
    return (
        record.run_id == run_id
        and record.session_id == session_id
        and record.tool_call_record_id == tool_call_record_id
        and record.skill_id == skill_id
        and record.skill_version_id == skill_version_id
        and record.script_path == script_path
        and record.input_file_ids == input_file_ids
        and record.parameters_sha256 == parameters_sha256
        and record.python_version == python_version
        and record.dependency_check == dependency_check
    )


def _unknown_start_result() -> ToolExecutionResult:
    """启动状态无法确认且没有可信审计身份时，仅返回 UNKNOWN 结果。"""
    return ToolExecutionResult(
        outcome=ToolOutcome(
            status=ToolOutcomeStatus.UNKNOWN,
            code="skill_execution_start_unknown",
            message="脚本执行记录状态无法确认。",
        )
    )


def _unknown_start_audit_result(
    execution_id: str,
    finished_at: datetime,
) -> ToolExecutionResult:
    """已找到冲突记录时使用已知审计身份收敛，不能猜测不存在的新 ID。"""
    code = "skill_execution_start_unknown"
    return ToolExecutionResult(
        outcome=ToolOutcome(
            status=ToolOutcomeStatus.UNKNOWN,
            code=code,
            message="脚本执行记录状态无法确认。",
        ),
        skill_execution_completion=SkillExecutionCompletion(
            execution_id=execution_id,
            status=SkillExecutionStatus.UNKNOWN,
            error_code=code,
            finished_at=finished_at,
        ),
    )


def _unknown_existing_start_result(
    record: SkillExecutionRecord,
    finished_at: datetime,
) -> ToolExecutionResult:
    """已有审计记录绝不重跑；只有 running 需要显式收敛为 UNKNOWN。"""
    if record.status is SkillExecutionStatus.RUNNING:
        return _unknown_start_audit_result(record.id, finished_at)
    # 终态审计已经可信，不能再提交一条 UNKNOWN completion 覆盖或与之冲突；
    # 工具调用仍按 UNKNOWN 收敛，executor 会按 tool_call_record_id 处理遗留状态。
    return _unknown_start_result()


def _error(code: str, message: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        outcome=ToolOutcome(
            status=ToolOutcomeStatus.ERROR,
            code=code,
            message=message,
        )
    )


def _script_failure_message(error_code: str | None) -> str:
    """只返回稳定、可理解的脚本失败原因。"""
    mapping = {
        "script_process_start_failed": "脚本进程无法启动。",
        "script_process_failed": "脚本进程状态无法确认。",
        "script_process_cleanup_failed": "脚本进程组未能安全收敛，结果与文件不确定。",
        "script_exit_nonzero": "脚本以非零状态退出，未交付产物。",
        "script_manifest_missing": "脚本未生成要求的输出清单。",
        "script_manifest_invalid": "脚本输出清单格式不合法。",
        "script_output_path_invalid": "输出文件路径不合法。",
        "script_output_duplicate": "输出清单包含重复文件。",
        "script_output_missing": "清单声明的文件不存在。",
        "script_output_link_not_allowed": "输出文件不能是链接。",
        "script_output_not_regular": "输出项不是普通文件。",
    }
    return mapping.get(error_code or "", "脚本执行失败。")


__all__ = ["SkillExecutionService"]
