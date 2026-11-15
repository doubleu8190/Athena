"""知识库 ingestion job 消费者。"""

from __future__ import annotations

import asyncio
import logging
import math
import multiprocessing
import sys
import time
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import asdict
from queue import Empty
from typing import Protocol, cast

from domain.knowledge.model import (
    ChunkLocator,
    OriginalStorageAccessDeniedError,
    OriginalStorageConfigurationError,
    OriginalStorageError,
    OriginalStorageNotFoundError,
    OriginalStorageUnavailableError,
    TextBlock,
)
from domain.knowledge.ports import (
    ChineseTokenizer,
    ClaimedKnowledgeJob,
    DocumentParser,
    DocumentParserFactory,
    FileUploadReservation,
    KnowledgeJobRepository,
    OriginalStore,
    ParsedDocument,
)
from domain.llm.ports import EmbeddingPort, TokenCounterPort

CHUNK_TOKEN_LIMIT = 512
CHUNK_TOKEN_OVERLAP = 64
LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 20

logger = logging.getLogger(__name__)


class _ChildResultQueue(Protocol):
    """multiprocessing.Queue 的窄结果边界。"""

    def put(self, value: tuple[bool, ParsedDocument | str]) -> None: ...


class _ChildProcess(Protocol):
    """Narrow lifecycle methods shared by multiprocessing context processes."""

    def is_alive(self) -> bool: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def join(self, timeout: float | None = None) -> None: ...


class KnowledgeWorker:
    """在租约保护下解析、向量化并发布一个知识版本。"""

    def __init__(
        self,
        repository: KnowledgeJobRepository,
        parser_factory: DocumentParserFactory,
        originals: OriginalStore,
        embedding: EmbeddingPort,
        tokenizer: ChineseTokenizer,
        counter: TokenCounterPort,
        *,
        isolate_parsing: bool = True,
        parse_timeout_seconds: int = 600,
        parse_memory_mb: int = 4096,
        profile_id: str | None = None,
        owner_concurrency: int = 2,
    ) -> None:
        """保存任务仓储及解析工厂、磁盘、模型和文本处理端口。"""
        self._repository = repository
        self._parser_factory = parser_factory
        # 非隔离模式下按需创建并缓存唯一解析器实例，避免重复初始化模型。
        self._parser: DocumentParser | None = None
        self._originals = originals
        self._embedding = embedding
        self._tokenizer = tokenizer
        self._counter = counter
        self._isolate_parsing = isolate_parsing
        self._parse_timeout_seconds = parse_timeout_seconds
        if parse_memory_mb < 512 or parse_memory_mb > 16384:
            raise ValueError("knowledge_parse_memory_limit_invalid")
        self._parse_memory_mb = parse_memory_mb
        self._profile_id = profile_id
        self._owner_concurrency = owner_concurrency
        self._sweep_counter = 0

    async def run_once(self) -> bool:
        """消费一条任务；返回是否认领到工作。"""
        self._sweep_counter += 1
        if self._sweep_counter >= 100:
            self._sweep_counter = 0
            await self._sweep_expired_uploads()
        job = await self._repository.claim_job(
            lease_seconds=LEASE_SECONDS,
            owner_concurrency=self._owner_concurrency,
        )
        if job is None:
            return False
        lost_lease = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(job, lost_lease))
        try:
            if job.kind == "cleanup":
                for key in job.source_keys:
                    try:
                        await self._originals.delete(key)
                    except OriginalStorageNotFoundError:
                        continue
                if lost_lease.is_set():
                    return True
                await self._repository.finish_cleanup_job(job)
                return True
            if job.profile_id != self._profile_id:
                raise ValueError("embedding_profile_mismatch")
            blocks, warnings = await self._read_blocks(job)
            chunks = _make_chunks(blocks, self._counter)
            if not chunks:
                raise ValueError("document_text_empty")
            vectors = await self._embedding.embed([chunk[0] for chunk in chunks])
            if len(vectors) != len(chunks) or any(
                len(vector) != 1024
                or not all(
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                    for value in vector
                )
                or not any(value != 0 for value in vector)
                for vector in vectors
            ):
                raise ValueError("embedding_profile_mismatch")
            if lost_lease.is_set():
                return True
            ready_chunks = [
                (content, locator, count, " ".join(self._tokenizer.tokenize(content)), vector)
                for (content, locator, count), vector in zip(chunks, vectors, strict=True)
            ]
            if not await self._repository.publish_job(job, chunks=ready_chunks, warnings=warnings):
                return True
        except asyncio.CancelledError:
            raise
        except Exception as error:
            code = _error_code(error)
            await self._repository.fail_job(
                job,
                error_code=code,
                retryable=_is_retryable_error(error, job.kind),
            )
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        return True

    async def _sweep_expired_uploads(self) -> None:
        """先删过期预留的原件并条件删除预留行，再清理老孤儿。"""
        try:
            expired = await self._repository.list_expired_upload_reservations(limit=20)
            for reservation in expired:
                try:
                    await self._cleanup_expired_reservation(reservation)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    code = (
                        error.code
                        if isinstance(error, OriginalStorageError)
                        else "knowledge_upload_cleanup_failed"
                    )
                    logger.warning(
                        "knowledge_upload_cleanup_failed code=%s exception_type=%s reservation=%s",
                        code,
                        type(error).__name__,
                        reservation.reservation_token,
                    )
                    continue
            candidates = self._originals.iter_orphan_candidates(older_than_seconds=86400)
            async with aclosing(cast(AsyncGenerator[str, None], candidates)) as orphan_candidates:
                async for key in orphan_candidates:
                    if await self._repository.is_storage_key_referenced(key):
                        continue
                    try:
                        await self._originals.delete(key)
                    except OriginalStorageNotFoundError:
                        continue
                    except OriginalStorageError as error:
                        logger.warning(
                            "knowledge_orphan_delete_failed code=%s",
                            error.code,
                        )
                        continue
        except Exception as error:
            # 回收失败不影响 ingestion；reservation 和 quota 保持可恢复状态。
            logger.warning(
                "knowledge_maintenance_failed exception_type=%s",
                type(error).__name__,
            )
            return

    async def _cleanup_expired_reservation(self, reservation: FileUploadReservation) -> None:
        """先原子删除过期预留行，确认无来源与有效预留引用后再删除对象（失败由孤儿扫描兜底）。"""
        if reservation.reservation_token is None or reservation.storage_key is None:
            return
        deleted = await self._repository.delete_expired_upload_reservation(
            reservation.reservation_token
        )
        if not deleted:
            return
        try:
            await self._originals.delete(reservation.storage_key)
        except OriginalStorageNotFoundError:
            pass
        except OriginalStorageError as error:
            logger.warning(
                "knowledge_upload_cleanup_failed code=%s exception_type=%s reservation=%s",
                error.code,
                type(error).__name__,
                reservation.reservation_token,
            )

    async def _read_blocks(self, job: ClaimedKnowledgeJob) -> tuple[list[TextBlock], list[str]]:
        if job.source_text is not None:
            return [TextBlock(job.source_text, ChunkLocator(kind="text"))], []
        if job.source_key is None or job.source_filename is None:
            raise ValueError("document_source_missing")
        content = await self._originals.get(job.source_key)
        if self._isolate_parsing:
            parsed = await _parse_in_child(
                job.source_filename,
                content,
                self._parser_factory,
                self._parse_timeout_seconds,
                self._parse_memory_mb,
            )
        else:
            if self._parser is None:
                self._parser = self._parser_factory.create()
            parsed = await self._parser.parse(job.source_filename, content)
        return list(parsed.blocks), list(parsed.warnings)

    async def _heartbeat(self, job: ClaimedKnowledgeJob, lost_lease: asyncio.Event) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            renewed = await self._repository.heartbeat_job(
                job.job_id,
                job.fencing_token,
                lease_seconds=LEASE_SECONDS,
            )
            if not renewed:
                lost_lease.set()
                return


def _make_chunks(
    blocks: list[TextBlock],
    counter: TokenCounterPort,
) -> list[tuple[str, dict[str, object], int]]:
    """保留块定位并按 tokenizer 限制长文本，片段间使用字符重叠近似 token 重叠。"""
    output: list[tuple[str, dict[str, object], int]] = []
    for block in blocks:
        pending = block.text.strip()
        while pending:
            if counter.count(pending) <= CHUNK_TOKEN_LIMIT:
                piece = pending
                pending = ""
            else:
                previous_length = len(pending)
                split_at = _largest_prefix(pending, counter, CHUNK_TOKEN_LIMIT)
                piece = pending[:split_at].rstrip()
                overlap_at = _largest_suffix_start(piece, counter, CHUNK_TOKEN_OVERLAP)
                pending = piece[overlap_at:].lstrip() + pending[split_at:]
                if not piece or len(pending) >= previous_length:
                    raise ValueError("chunker_no_progress")
            if piece:
                output.append((piece, asdict(block.locator), counter.count(piece)))
    return output


def _largest_prefix(text: str, counter: TokenCounterPort, limit: int) -> int:
    low, high = 1, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if counter.count(text[:middle]) <= limit:
            low = middle
        else:
            high = middle - 1
    return low


def _largest_suffix_start(text: str, counter: TokenCounterPort, limit: int) -> int:
    # 使用二分查找定位满足 token 上限的最长后缀起点（最小 start），将 O(N) 线性探测降为 O(log N)。
    if not text or counter.count(text) <= limit:
        return 0
    low, high = 1, len(text)
    while low < high:
        middle = (low + high) // 2
        if counter.count(text[middle:]) <= limit:
            high = middle
        else:
            low = middle + 1
    return low


def _error_code(error: Exception) -> str:
    """把异常映射为不含正文、物理路径或驱动细节的短错误码。"""
    if isinstance(error, OriginalStorageError):
        return error.code
    if isinstance(error, TimeoutError):
        return "knowledge_parse_timeout"
    if isinstance(error, (ValueError, TypeError)):
        code = str(error)
        if code.startswith("knowledge_") and len(code) <= 64:
            return code
        return "knowledge_document_invalid"
    if isinstance(error, RuntimeError):
        code = str(error)
        if code in {
            "knowledge_ocr_engine_unavailable",
            "knowledge_parse_memory_monitor_unavailable",
            "knowledge_parser_process_failed",
            "knowledge_parser_transient",
        }:
            return code
    if isinstance(error, OSError):
        return "knowledge_storage_unavailable"
    return "knowledge_processing_failed"


def _is_retryable_error(error: Exception, job_kind: str) -> bool:
    """Apply distinct ingestion and cleanup retry policies."""
    if isinstance(error, OriginalStorageUnavailableError):
        return True
    if isinstance(
        error,
        (OriginalStorageAccessDeniedError, OriginalStorageConfigurationError),
    ):
        return job_kind == "cleanup"
    if isinstance(error, OriginalStorageError):
        return False
    return not isinstance(error, (ValueError, TypeError))


def _process_rss_mb(process_id: int) -> int | None:
    """Read a child process RSS in MiB for the macOS parser guard."""
    try:
        import psutil  # type: ignore[import-untyped]

        return cast(int, psutil.Process(process_id).memory_info().rss // (1024 * 1024))
    except Exception:
        return None


async def _parse_in_child(
    filename: str,
    content: bytes,
    factory: DocumentParserFactory,
    timeout_seconds: int,
    memory_limit_mb: int = 4096,
) -> ParsedDocument:
    """在独立进程运行 OCR/解析，取消或超时后终止并回收进程。"""
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue(maxsize=1)
    # 工厂实例随 spawn 参数在父进程序列化；子进程反序列化参数时
    # （进入目标函数前）即导入解析器模块，create() 时才实例化解析器。
    process = context.Process(
        target=_parse_child_entry,
        args=(filename, content, factory, result_queue, memory_limit_mb),
    )
    # 进程可能启动失败（例如工厂无法序列化）：只在真正启动后终止回收，
    # 但结果队列无论启动成败都要在 finally 中关闭，避免句柄泄漏。
    started = False
    try:
        process.start()
        started = True
        deadline = time.monotonic() + timeout_seconds
        received: tuple[bool, ParsedDocument | str] | None = None
        while received is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                received = await asyncio.to_thread(result_queue.get, True, min(0.25, remaining))
                break
            except Empty:
                pass
            if sys.platform == "darwin" and process.pid is not None:
                rss_mb = await asyncio.to_thread(_process_rss_mb, process.pid)
                if rss_mb is None and process.is_alive():
                    await _terminate_and_reap(process)
                    raise RuntimeError("knowledge_parse_memory_monitor_unavailable")
                if rss_mb is not None and rss_mb > memory_limit_mb:
                    await _terminate_and_reap(process)
                    raise ValueError("knowledge_parse_memory_limit")
        # A child may publish its result just before exiting. Receiving from the
        # queue is not proof that the process has exited; wait for it within the
        # same deadline before treating it as a hung parser.
        remaining = max(0.0, deadline - time.monotonic())
        await asyncio.to_thread(process.join, remaining)
        if process.is_alive():
            await _terminate_and_reap(process)
            raise TimeoutError("knowledge_parse_timeout")
        if process.exitcode != 0:
            raise RuntimeError("knowledge_parser_process_failed")
        if received is None:
            raise RuntimeError("knowledge_parser_result_missing")
        succeeded, result = received
        if not succeeded:
            category = str(result)
            if category.startswith("invalid:"):
                raise ValueError(category.removeprefix("invalid:"))
            if category.startswith("transient:"):
                raise RuntimeError(category.removeprefix("transient:"))
            if category.startswith("unsupported:"):
                raise ValueError(category.removeprefix("unsupported:"))
            raise RuntimeError("knowledge_parser_failed")
        if not isinstance(result, ParsedDocument):
            raise RuntimeError("knowledge_parser_result_invalid")
        return result
    except asyncio.CancelledError:
        if started:
            await _terminate_and_reap(process)
        raise
    except BaseException:
        if started:
            await _terminate_and_reap(process)
        raise
    finally:
        result_queue.close()
        result_queue.join_thread()


async def _terminate_and_reap(process: _ChildProcess) -> None:
    """Best-effort termination and mandatory join on every exceptional path."""
    try:
        if process.is_alive():
            process.terminate()
        await asyncio.shield(asyncio.to_thread(process.join, 2))
        if process.is_alive():
            process.kill()
    except Exception:
        try:
            if process.is_alive():
                process.kill()
        except Exception:
            pass
    finally:
        try:
            await asyncio.shield(asyncio.to_thread(process.join))
        except Exception:
            pass


def _parse_child_entry(
    filename: str,
    content: bytes,
    factory: DocumentParserFactory,
    result_queue: _ChildResultQueue,
    memory_limit_mb: int,
) -> None:
    """子进程解析入口，确保 parser 和 OCR runtime 在子进程创建。"""
    try:
        import asyncio

        if sys.platform != "darwin":
            try:
                import resource
            except ImportError as error:
                raise RuntimeError("resource_limits_unsupported") from error
            memory_limit = memory_limit_mb * 1024 * 1024
            try:
                resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
            except (OSError, ValueError) as error:
                raise RuntimeError("resource_limits_unsupported") from error

        # 工厂参数在子进程反序列化时（进入本函数前）导入 infrastructure 实现；
        # create() 在本函数内实例化解析器。父进程无需加载 OCR runtime，
        # 应用层也不直连 infrastructure。
        parsed = asyncio.run(factory.create().parse(filename, content))
        result_queue.put((True, parsed))
    except Exception as error:
        # Do not serialize exception text: it can contain source content or
        # physical paths. Preserve only a safe retry category across IPC.
        message = str(error)
        safe_code = (
            message
            if message.startswith("knowledge_") and message.replace("_", "").isalnum()
            else None
        )
        if message == "resource_limits_unsupported":
            category = "unsupported:knowledge_parse_resource_limits_unsupported"
        elif isinstance(error, ValueError):
            category = f"invalid:{safe_code or 'knowledge_document_invalid'}"
        else:
            category = f"transient:{safe_code or 'knowledge_parser_transient'}"
        result_queue.put((False, category))
