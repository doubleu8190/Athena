"""知识库 ingestion job 消费者。"""

from __future__ import annotations

import asyncio
import logging
import math
import multiprocessing
import re
import threading
import time
import weakref
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from dataclasses import asdict
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import cast

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
    KnowledgeJobRepository,
    OriginalStore,
    ParsedDocument,
)
from domain.llm.ports import EmbeddingPort, TokenCounterPort

CHUNK_TOKEN_LIMIT = 512
CHUNK_TOKEN_OVERLAP = 64
EMBED_BATCH_SIZE = 32
EMBED_CONCURRENCY_FACTOR = 4
PARSER_CHILD_MAX_TASKS = 50
ORPHAN_REFERENCE_BATCH_SIZE = 100
DEFAULT_ORPHAN_SWEEP_INTERVAL_SECONDS = 3600.0
LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 20

_SENTENCE_END_PUNCTUATION = frozenset({"。", "！", "？", "；", ".", "!", "?", ";"})
_CLAUSE_AND_SPACE_SEPARATORS = frozenset({"，", ",", " ", "\t"})
_IGNORED_CONTEXT_HEADINGS = frozenset({"normal", "body text", "default paragraph font", "正文"})
_TABLE_PLACEHOLDER_HEADING_RE = re.compile(r"^table \d+$", re.IGNORECASE)

logger = logging.getLogger(__name__)

# spawn 解析子进程内缓存的解析器与事件循环，消除每文档重复初始化 OCR 运行时的冷启动开销。
_CHILD_PARSER: DocumentParser | None = None
_CHILD_LOOP: asyncio.AbstractEventLoop | None = None


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
        parse_pool_size: int | None = None,
        profile_id: str | None = None,
        owner_concurrency: int = 2,
        orphan_sweep_interval_seconds: float = DEFAULT_ORPHAN_SWEEP_INTERVAL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """保存任务仓储及解析工厂、存储、模型和文本处理端口。"""
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
        if orphan_sweep_interval_seconds < 0.0:
            raise ValueError("knowledge_orphan_sweep_interval_invalid")
        resolved_pool_size = (
            max(1, min(owner_concurrency, 4)) if parse_pool_size is None else parse_pool_size
        )
        if resolved_pool_size < 1 or resolved_pool_size > 16:
            raise ValueError("knowledge_parse_pool_size_invalid")
        self._profile_id = profile_id
        self._owner_concurrency = owner_concurrency
        self._orphan_sweep_interval_seconds = orphan_sweep_interval_seconds
        self._monotonic = monotonic
        self._sweep_counter = 0
        self._last_orphan_sweep_at: float | None = None
        # 隔离模式下基于 spawn + 双工 Pipe 复用受控解析子进程，避免重复 OCR 冷启动与私有属性探测。
        self._parser_pool: _IsolatedParserPool | None = (
            _IsolatedParserPool(
                parser_factory,
                max_workers=resolved_pool_size,
                timeout_seconds=parse_timeout_seconds,
            )
            if isolate_parsing
            else None
        )

    async def aclose(self) -> None:
        """优雅关闭并回收解析子进程池；可安全重复调用。"""
        if self._parser_pool is not None:
            await self._parser_pool.aclose()

    async def run_once(self) -> bool:
        """消费一条任务；返回是否认领到工作。"""
        self._sweep_counter += 1
        if self._sweep_counter >= 100:
            self._sweep_counter = 0
            # 按单调时钟降频执行 24 小时宽限期全桶孤儿扫描，避免每次轮询重复枚举对象存储。
            now = self._monotonic()
            if (
                self._last_orphan_sweep_at is None
                or (now - self._last_orphan_sweep_at) >= self._orphan_sweep_interval_seconds
            ):
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
                raise ValueError("knowledge_embedding_profile_mismatch")
            blocks, warnings = await self._read_blocks(job)
            # 切片与 token 计数属于 CPU 密集型计算，卸载到工作线程避免阻塞 120s 租约心跳协程。
            chunks = await asyncio.to_thread(_make_chunks, blocks, self._counter)
            if not chunks:
                raise ValueError("document_text_empty")
            if lost_lease.is_set():
                return True
            # 为向量化与全文检索构造带文档/章节/表定位头的索引文本；
            # 入库正文与 token_count 仍保留干净原文。
            index_texts = [
                _build_index_text(content, locator, job.title) for content, locator, _ in chunks
            ]
            # 按嵌入客户端配置动态决定单轮向量化窗口大小（如 batch_size * 4），
            # 既让远程客户端 4 路并发充分生效，又在窗口间定期复核租约状态及时中止失租任务。
            embed_window_size = _resolve_embed_window_size(self._embedding)
            vectors: list[list[float]] = []
            for start in range(0, len(chunks), embed_window_size):
                if lost_lease.is_set():
                    return True
                batch_texts = index_texts[start : start + embed_window_size]
                batch_vectors = await self._embedding.embed(batch_texts)
                if len(batch_vectors) != len(batch_texts) or any(
                    not _is_valid_embedding_vector(vector) for vector in batch_vectors
                ):
                    raise ValueError("knowledge_embedding_profile_mismatch")
                vectors.extend(batch_vectors)
                if lost_lease.is_set():
                    return True
            # 中文分词同样移出事件循环，避免大文档全文分词阻塞心跳与并发消费者。
            ready_chunks = await asyncio.to_thread(
                _build_ready_chunks,
                chunks,
                index_texts,
                vectors,
                self._tokenizer,
            )
            if lost_lease.is_set():
                return True
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
        """批量扫描超过 24 小时宽限期且未被来源表引用的孤儿对象并回收。"""
        try:
            candidates = self._originals.iter_orphan_candidates(older_than_seconds=86400)
            async with aclosing(cast(AsyncGenerator[str, None], candidates)) as orphan_candidates:
                batch: list[str] = []
                async for key in orphan_candidates:
                    batch.append(key)
                    if len(batch) >= ORPHAN_REFERENCE_BATCH_SIZE:
                        await self._delete_unreferenced_orphans(batch)
                        batch.clear()
                if batch:
                    await self._delete_unreferenced_orphans(batch)
            self._last_orphan_sweep_at = self._monotonic()
        except Exception as error:
            # 孤儿回收失败不影响 ingestion 主循环，等待下个周期重试。
            logger.warning(
                "knowledge_maintenance_failed exception_type=%s",
                type(error).__name__,
            )
            return

    async def _delete_unreferenced_orphans(self, keys: list[str]) -> None:
        """按页批量核验对象引用后再删除未引用孤儿对象，避免 N+1 查库。"""
        referenced = await self._repository.find_referenced_storage_keys(keys)
        unreferenced = [key for key in keys if key not in referenced]
        for key in unreferenced:
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

    async def _read_blocks(self, job: ClaimedKnowledgeJob) -> tuple[list[TextBlock], list[str]]:
        if job.source_text is not None:
            return [TextBlock(job.source_text, ChunkLocator(kind="text"))], []
        if job.source_key is None or job.source_filename is None:
            raise ValueError("document_source_missing")
        content = await self._originals.get(job.source_key)
        if self._isolate_parsing:
            assert self._parser_pool is not None
            parsed = await self._parser_pool.parse(job.source_filename, content)
        else:
            if self._parser is None:
                self._parser = self._parser_factory.create()
            parsed = await self._parser.parse(job.source_filename, content)
        return list(parsed.blocks), list(parsed.warnings)

    async def _heartbeat(self, job: ClaimedKnowledgeJob, lost_lease: asyncio.Event) -> None:
        # 每 20 秒续租一次，一旦失租即置位 lost_lease 中止后续步骤
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


def _resolve_embed_window_size(embedding: EmbeddingPort) -> int:
    """根据嵌入客户端配置计算单轮送检窗口大小；无 .config 的测试替身回退默认 EMBED_BATCH_SIZE。"""
    config = getattr(embedding, "config", None)
    configured_batch = getattr(config, "batch_size", None)
    if (
        isinstance(configured_batch, int)
        and not isinstance(configured_batch, bool)
        and configured_batch > 0
    ):
        return configured_batch * EMBED_CONCURRENCY_FACTOR
    return EMBED_BATCH_SIZE


def _is_valid_embedding_vector(vector: list[float]) -> bool:
    """校验单条知识库向量满足 1024 维、全有限数值且非全零。"""
    return (
        len(vector) == 1024
        and all(
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            for value in vector
        )
        and any(value != 0 for value in vector)
    )


def _build_index_text(content: str, locator: dict[str, object], title: str) -> str:
    """按规则拼装上下文检索头，仅用于向量化与分词索引，不污染原始展示正文。"""
    parts: list[str] = []
    clean_title = title.strip()
    if clean_title:
        parts.append(f"文档: {clean_title}")

    heading = locator.get("heading")
    if isinstance(heading, str):
        clean_heading = heading.strip()
        if (
            clean_heading
            and clean_heading.casefold() not in _IGNORED_CONTEXT_HEADINGS
            and _TABLE_PLACEHOLDER_HEADING_RE.match(clean_heading) is None
        ):
            parts.append(f"章节: {clean_heading}")

    sheet = locator.get("sheet")
    if isinstance(sheet, str) and sheet.strip():
        parts.append(f"工作表: {sheet.strip()}")

    slide = locator.get("slide")
    if slide is not None and not isinstance(slide, bool):
        slide_text = str(slide).strip()
        if slide_text:
            parts.append(f"第{slide_text}页")

    if not parts:
        return content
    return f"[{' | '.join(parts)}]\n{content}"


def _build_ready_chunks(
    chunks: list[tuple[str, dict[str, object], int]],
    index_texts: list[str],
    vectors: list[list[float]],
    tokenizer: ChineseTokenizer,
) -> list[tuple[str, dict[str, object], int, str, list[float]]]:
    """在工作线程中为增强索引文本生成中文分词，并与干净原文组装发布负载。"""
    return [
        (content, locator, count, " ".join(tokenizer.tokenize(index_text)), vector)
        for (content, locator, count), index_text, vector in zip(
            chunks, index_texts, vectors, strict=True
        )
    ]


def _table_group_key(block: TextBlock) -> tuple[str, str, str | None] | None:
    """返回参与按表聚合与拆片重复表头的表格分组键；无明确表标识时返回 None。"""
    locator = block.locator
    if locator.kind == "xlsx" and locator.sheet is not None:
        return ("xlsx", locator.sheet, None)
    if block.kind != "table":
        return None
    if locator.kind == "docx_table" and locator.heading:
        return ("docx_table", locator.heading, None)
    if locator.kind == "pptx_table" and locator.slide is not None and locator.shape is not None:
        return ("pptx_table", str(locator.slide), locator.shape)
    return None


def _merge_region_key(
    block: TextBlock,
) -> tuple[str, int | None, str | None, int | None, str | None, str | None] | None:
    """返回可合并短文本块的区域键；代码块与表格块不参与普通文本合并。"""
    if block.kind != "text":
        return None
    locator = block.locator
    return (
        locator.kind,
        locator.page,
        locator.sheet,
        locator.slide,
        locator.media_id,
        locator.heading,
    )


def _merge_locators(first: ChunkLocator, last: ChunkLocator) -> ChunkLocator:
    """合并相邻同区域文本块的起止行号、表格行范围与幻灯片形状定位。"""
    end_line = last.end_line if last.end_line is not None else last.start_line
    cell_range = first.cell_range
    if first.cell_range is not None and last.cell_range is not None:
        first_start = first.cell_range.split(":", 1)[0]
        last_end = last.cell_range.rsplit(":", 1)[-1]
        cell_range = f"{first_start}:{last_end}"
    elif last.cell_range is not None:
        cell_range = last.cell_range
    if first.shape == last.shape:
        shape = first.shape
    elif first.shape is not None and last.shape is not None:
        shape = f"{first.shape}-{last.shape}"
    else:
        shape = first.shape or last.shape
    return ChunkLocator(
        kind=first.kind,
        page=first.page,
        start_line=first.start_line,
        end_line=end_line,
        heading=first.heading,
        sheet=first.sheet,
        cell_range=cell_range,
        slide=first.slide,
        shape=shape,
        media_id=first.media_id,
    )


def _find_natural_split_end(text: str, pos: int, max_end: int) -> int:
    """在最大右端点末尾约 20% 窗口内按段落/换行/句末标点/子句空白寻找自然切分边界。"""
    window = max(1, (max_end - pos) // 5)
    min_end = max(pos + 1, max_end - window)

    # 1. 优先在空行（段落边界）后切分。
    para_idx = text.rfind("\n\n", min_end, max_end)
    if para_idx != -1:
        return para_idx + 2

    # 2. 次优在单个换行符后切分。
    line_idx = text.rfind("\n", min_end, max_end)
    if line_idx != -1:
        return line_idx + 1

    # 3. 再次优在句末标点后切分；跳过数字间小数点/版本号点（如 3.14、v2.1）。
    text_len = len(text)
    for idx in range(max_end - 1, min_end - 1, -1):
        ch = text[idx]
        if ch not in _SENTENCE_END_PUNCTUATION:
            continue
        if (
            ch == "."
            and idx > 0
            and idx + 1 < text_len
            and text[idx - 1].isdigit()
            and text[idx + 1].isdigit()
        ):
            continue
        return idx + 1

    # 4. 兜底在子句标点或空白字符后切分，避免截断英文单词。
    for idx in range(max_end - 1, min_end - 1, -1):
        if text[idx] in _CLAUSE_AND_SPACE_SEPARATORS:
            return idx + 1

    return max_end


def _split_long_text_with_cursor(
    text: str,
    locator: ChunkLocator,
    counter: TokenCounterPort,
    output: list[tuple[str, dict[str, object], int]],
    *,
    prefix: str = "",
) -> None:
    """按 Python 字符下标执行严格 O(N) 游标分块（倍增探测 + 自然边界回退），天然不切碎 UTF-8。"""
    total = len(text)
    pos = 0
    # 带固定前缀（如表格重复表头）切分时，限制重叠预算不超过可用正文空间的 1/4，确保稳步前移。
    overlap_budget = (
        min(CHUNK_TOKEN_OVERLAP, max(1, (CHUNK_TOKEN_LIMIT - counter.count(prefix)) // 4))
        if prefix
        else CHUNK_TOKEN_OVERLAP
    )
    while pos < total:
        max_end = _largest_prefix_from(text, pos, counter, CHUNK_TOKEN_LIMIT, prefix=prefix)
        if max_end >= total:
            piece = text[pos:].rstrip()
            if piece:
                chunk_text = f"{prefix}{piece}" if prefix else piece
                output.append((chunk_text, asdict(locator), counter.count(chunk_text)))
            break
        split_end = _find_natural_split_end(text, pos, max_end)
        piece = text[pos:split_end].rstrip()
        if not piece:
            # 窗口内全为空白时直接跳过并单调前移游标，杜绝死循环。
            pos = split_end
            while pos < total and text[pos].isspace():
                pos += 1
            continue
        chunk_text = f"{prefix}{piece}" if prefix else piece
        output.append((chunk_text, asdict(locator), counter.count(chunk_text)))
        overlap_offset = _largest_suffix_start(piece, counter, overlap_budget)
        next_pos = pos + overlap_offset
        if (
            pos < next_pos < split_end
            and text[next_pos - 1].isascii()
            and text[next_pos - 1].isalnum()
            and text[next_pos].isascii()
            and text[next_pos].isalnum()
        ):
            # overlap 落在英文单词或数字中间时，向后跳过当前词残片到最近空白/换行。
            adjusted = next_pos
            while adjusted < split_end and not text[adjusted].isspace():
                adjusted += 1
            while adjusted < split_end and text[adjusted].isspace():
                adjusted += 1
            if adjusted < split_end:
                next_pos = adjusted
        while next_pos < split_end and text[next_pos].isspace():
            next_pos += 1
        if next_pos <= pos:
            next_pos = split_end
            while next_pos < total and text[next_pos].isspace():
                next_pos += 1
        pos = next_pos


def _make_chunks(
    blocks: list[TextBlock],
    counter: TokenCounterPort,
) -> list[tuple[str, dict[str, object], int]]:
    """合并相邻同区域短文本块与同表行（拆片自动重复表头），隔离代码块并按自然边界切分长文本。"""
    output: list[tuple[str, dict[str, object], int]] = []
    buffer_text = ""
    buffer_tokens = 0
    buffer_count = 0
    buffer_first_locator: ChunkLocator | None = None
    buffer_last_locator: ChunkLocator | None = None
    buffer_region: tuple[str, int | None, str | None, int | None, str | None, str | None] | None = (
        None
    )
    buffer_table_key: tuple[str, str, str | None] | None = None
    current_table_key: tuple[str, str, str | None] | None = None
    current_table_header: str | None = None
    current_table_header_tokens = 0

    def flush_buffer() -> None:
        nonlocal buffer_text, buffer_tokens, buffer_count
        nonlocal buffer_first_locator, buffer_last_locator, buffer_region, buffer_table_key
        if not buffer_text or buffer_first_locator is None or buffer_last_locator is None:
            return
        locator = (
            buffer_first_locator
            if buffer_count == 1
            else _merge_locators(buffer_first_locator, buffer_last_locator)
        )
        output.append((buffer_text, asdict(locator), buffer_tokens))
        buffer_text = ""
        buffer_tokens = 0
        buffer_count = 0
        buffer_first_locator = None
        buffer_last_locator = None
        buffer_region = None
        buffer_table_key = None

    def reset_table_state() -> None:
        nonlocal current_table_key, current_table_header, current_table_header_tokens
        current_table_key = None
        current_table_header = None
        current_table_header_tokens = 0

    def start_table_buffer(
        pending: str,
        pending_tokens: int,
        locator: ChunkLocator,
        table_key: tuple[str, str, str | None],
        *,
        prepend_header: bool,
    ) -> None:
        nonlocal buffer_text, buffer_tokens, buffer_count
        nonlocal buffer_first_locator, buffer_last_locator, buffer_region, buffer_table_key
        # 表格拆片开启新分片时，若表头不超过单块预算一半，始终携带表头：
        # 拼接后不超上限则入缓冲继续合并后续短行；超上限则以表头为前缀切分当前长行。
        if (
            prepend_header
            and current_table_header is not None
            and current_table_header_tokens <= CHUNK_TOKEN_LIMIT // 2
        ):
            with_header = f"{current_table_header}\n{pending}"
            with_header_tokens = counter.count(with_header)
            if with_header_tokens <= CHUNK_TOKEN_LIMIT:
                buffer_text = with_header
                buffer_tokens = with_header_tokens
                buffer_count = 1
                buffer_first_locator = locator
                buffer_last_locator = locator
                buffer_region = None
                buffer_table_key = table_key
                return
            _split_long_text_with_cursor(
                pending,
                locator,
                counter,
                output,
                prefix=f"{current_table_header}\n",
            )
            return
        if pending_tokens <= CHUNK_TOKEN_LIMIT:
            buffer_text = pending
            buffer_tokens = pending_tokens
            buffer_count = 1
            buffer_first_locator = locator
            buffer_last_locator = locator
            buffer_region = None
            buffer_table_key = table_key
            return
        _split_long_text_with_cursor(pending, locator, counter, output)

    for block in blocks:
        pending = block.text.strip()
        if not pending:
            continue
        pending_tokens = counter.count(pending)
        table_key = _table_group_key(block)
        if table_key is not None:
            is_new_table = table_key != current_table_key
            if is_new_table:
                flush_buffer()
                current_table_key = table_key
                current_table_header = pending
                current_table_header_tokens = pending_tokens
                start_table_buffer(
                    pending, pending_tokens, block.locator, table_key, prepend_header=False
                )
                continue
            if buffer_count > 0 and buffer_table_key == table_key:
                candidate_text = f"{buffer_text}\n{pending}"
                candidate_tokens = counter.count(candidate_text)
                if candidate_tokens <= CHUNK_TOKEN_LIMIT:
                    buffer_text = candidate_text
                    buffer_tokens = candidate_tokens
                    buffer_count += 1
                    buffer_last_locator = block.locator
                    continue
                flush_buffer()
            start_table_buffer(
                pending, pending_tokens, block.locator, table_key, prepend_header=True
            )
            continue

        if current_table_key is not None:
            flush_buffer()
            reset_table_state()

        region = _merge_region_key(block)
        # 同区域短文本块按换行合并，减少碎片切片数量并改善语义检索上下文完整度。
        if region is not None and pending_tokens <= CHUNK_TOKEN_LIMIT:
            if buffer_count > 0 and buffer_region == region:
                candidate_text = f"{buffer_text}\n{pending}"
                candidate_tokens = counter.count(candidate_text)
                if candidate_tokens <= CHUNK_TOKEN_LIMIT:
                    buffer_text = candidate_text
                    buffer_tokens = candidate_tokens
                    buffer_count += 1
                    buffer_last_locator = block.locator
                    continue
            flush_buffer()
            buffer_text = pending
            buffer_tokens = pending_tokens
            buffer_count = 1
            buffer_first_locator = block.locator
            buffer_last_locator = block.locator
            buffer_region = region
            continue

        # 代码块、无表标识的表格块或超长单块先结算已有缓冲，再独立输出或执行游标切分。
        flush_buffer()
        if pending_tokens <= CHUNK_TOKEN_LIMIT:
            output.append((pending, asdict(block.locator), pending_tokens))
            continue
        _split_long_text_with_cursor(pending, block.locator, counter, output)
    flush_buffer()
    return output


def _largest_prefix_from(
    text: str,
    start: int,
    counter: TokenCounterPort,
    limit: int,
    *,
    prefix: str = "",
) -> int:
    """从游标 start 起以指数倍增探测再局部二分，返回带 prefix 满足 token 上限的最大右端点。"""
    remaining = len(text) - start
    if remaining <= 1:
        return len(text)
    low = 0
    probe = min(remaining, 64)
    while True:
        segment = text[start : start + probe]
        candidate = f"{prefix}{segment}" if prefix else segment
        if counter.count(candidate) <= limit:
            low = probe
            if probe == remaining:
                return len(text)
            probe = min(remaining, probe * 2)
        else:
            high = probe - 1
            break
    low = max(1, low)
    while low < high:
        middle = (low + high + 1) // 2
        segment = text[start : start + middle]
        candidate = f"{prefix}{segment}" if prefix else segment
        if counter.count(candidate) <= limit:
            low = middle
        else:
            high = middle - 1
    return start + low


def _largest_suffix_start(text: str, counter: TokenCounterPort, limit: int) -> int:
    """二分查找满足 token 上限的最长后缀起点（最小 start），复杂度 O(log N)。"""
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
            "knowledge_parser_process_failed",
            "knowledge_parser_transient",
        }:
            return code
    if isinstance(error, OSError):
        return "knowledge_storage_unavailable"
    return "knowledge_processing_failed"


def _is_retryable_error(error: Exception, job_kind: str) -> bool:
    """按入库与清理两类任务区分是否允许后台重试。"""
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


def _classify_parser_error(error: BaseException) -> tuple[str, bool]:
    """将解析子进程中的异常脱敏为安全错误码，并返回是否需要回收重建子进程池。"""
    if isinstance(error, MemoryError):
        return "invalid:knowledge_parse_memory_limit", True
    message = str(error)
    safe_code = (
        message if message.startswith("knowledge_") and message.replace("_", "").isalnum() else None
    )
    if isinstance(error, ValueError):
        # 合法文档格式/加密/页数等业务校验错误不破坏解析器运行时，允许复用子进程。
        return f"invalid:{safe_code or 'knowledge_document_invalid'}", False
    # 其余运行期异常（OCR 引擎故障、C++ 异常等）可能遗留脏状态，建议回收重建子进程池。
    return f"transient:{safe_code or 'knowledge_parser_transient'}", True


def _decode_child_error(category: str) -> Exception:
    """将脱敏后的子进程错误类别还原为父进程统一的领域异常类型。"""
    if category.startswith("invalid:"):
        return ValueError(category.removeprefix("invalid:"))
    if category.startswith("transient:"):
        return RuntimeError(category.removeprefix("transient:"))
    return RuntimeError("knowledge_parser_failed")


def _close_child_loop() -> None:
    """子进程退出时关闭按需创建的事件循环。"""
    global _CHILD_LOOP
    loop = _CHILD_LOOP
    _CHILD_LOOP = None
    if loop is not None and not loop.is_closed():
        loop.close()


def _parser_worker_entry(factory: DocumentParserFactory, conn: Connection) -> None:
    """spawn 解析子进程主循环：初始化一次解析器并按双工 Pipe 逐条处理请求。"""
    global _CHILD_PARSER, _CHILD_LOOP
    try:
        _CHILD_PARSER = factory.create()
        _CHILD_LOOP = None
        while True:
            try:
                payload = conn.recv()
            except EOFError:
                break
            if payload is None:
                break
            filename, content = cast(tuple[str, bytes], payload)
            try:
                # 若解析器实现提供 parse_sync，直接同步执行以省去子进程内额外事件循环开销。
                parse_sync = getattr(_CHILD_PARSER, "parse_sync", None)
                if callable(parse_sync):
                    parsed = cast(ParsedDocument, parse_sync(filename, content))
                else:
                    if _CHILD_LOOP is None or _CHILD_LOOP.is_closed():
                        _CHILD_LOOP = asyncio.new_event_loop()
                        asyncio.set_event_loop(_CHILD_LOOP)
                    parsed = _CHILD_LOOP.run_until_complete(_CHILD_PARSER.parse(filename, content))
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise
                category, should_recycle = _classify_parser_error(error)
                response: tuple[bool, ParsedDocument | str, bool] = (
                    False,
                    category,
                    should_recycle,
                )
            else:
                response = (True, parsed, False)
            try:
                conn.send(response)
            except (BrokenPipeError, EOFError, OSError):
                break
    finally:
        _CHILD_PARSER = None
        _close_child_loop()
        try:
            conn.close()
        except Exception:
            pass


class _ParserWorkerProcess:
    """受控解析子进程槽位：封装独立 spawn 进程、双工 Pipe 与幂等回收逻辑。"""

    def __init__(self, process: BaseProcess, conn: Connection) -> None:
        self.process = process
        self.conn = conn
        self.tasks_completed = 0
        self._closed = False
        self._close_lock = threading.Lock()

    def is_alive(self) -> bool:
        """判断子进程是否尚未关闭且仍存活。"""
        return not self._closed and self.process.is_alive()

    def close_sync(self, *, force_kill: bool) -> None:
        """线程安全、幂等地关闭当前单个子进程并释放 Pipe 句柄。"""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            if not force_kill:
                try:
                    # 投递哨兵 None 通知子进程优雅退出事件循环。
                    self.conn.send(None)
                except (BrokenPipeError, EOFError, OSError):
                    pass
                try:
                    self.process.join(timeout=1.0)
                except Exception:
                    pass
            try:
                if self.process.is_alive():
                    self.process.terminate()
                    self.process.join(timeout=1.0)
                if self.process.is_alive():
                    self.process.kill()
                    self.process.join(timeout=1.0)
                else:
                    # 已退出进程短 join 收割退出码，避免僵尸进程残留。
                    self.process.join(timeout=0.1)
            except Exception:
                pass
            try:
                self.conn.close()
            except Exception:
                pass


def _exchange_task(
    worker: _ParserWorkerProcess,
    filename: str,
    content: bytes,
) -> tuple[bool, ParsedDocument | str, bool]:
    """在工作线程内通过双工 Pipe 向单个子进程发送文档并等待解析结果。"""
    worker.conn.send((filename, content))
    return cast(tuple[bool, ParsedDocument | str, bool], worker.conn.recv())


def _cleanup_workers_sync(workers: set[_ParserWorkerProcess]) -> None:
    """weakref.finalize 回调：无父对象强引用，强杀回收全部残留解析子进程。"""
    for worker in list(workers):
        workers.discard(worker)
        worker.close_sync(force_kill=True)


class _IsolatedParserPool:
    """基于 spawn + 双工 Pipe 的隔离解析子进程池（支持单进程超时强杀与不误杀兄弟轮换）。"""

    def __init__(
        self,
        factory: DocumentParserFactory,
        *,
        max_workers: int,
        timeout_seconds: int,
        max_tasks_per_child: int = PARSER_CHILD_MAX_TASKS,
    ) -> None:
        if max_workers < 1:
            raise ValueError("knowledge_parse_pool_size_invalid")
        if timeout_seconds < 1 or max_tasks_per_child < 1:
            raise ValueError("knowledge_parse_pool_config_invalid")
        self._factory = factory
        self._max_workers = max_workers
        self._timeout_seconds = timeout_seconds
        self._max_tasks_per_child = max_tasks_per_child
        self._semaphore = asyncio.Semaphore(max_workers)
        self._idle: list[_ParserWorkerProcess] = []
        self._workers: set[_ParserWorkerProcess] = set()
        self._closed = False
        # 仅捕获 _workers 集合，不持有 self 强引用，保证测试未调用 aclose() 退出时不挂起。
        self._finalizer = weakref.finalize(self, _cleanup_workers_sync, self._workers)

    def active_processes(self) -> tuple[BaseProcess, ...]:
        """只读返回当前进程池持有的存活子进程句柄。"""
        return tuple(worker.process for worker in self._workers if worker.is_alive())

    def _acquire_or_spawn_worker(self) -> _ParserWorkerProcess:
        """优先复用存活空闲子进程，无可用空闲进程时按 spawn 方式懒拉起新子进程。"""
        if self._closed:
            raise RuntimeError("knowledge_parser_pool_closed")
        while self._idle:
            candidate = self._idle.pop()
            if candidate.is_alive():
                return candidate
            # 已意外退出的空闲子进程立即淘汰并关闭 Pipe，不留死句柄。
            self._workers.discard(candidate)
            candidate.close_sync(force_kill=True)
        ctx = multiprocessing.get_context("spawn")
        parent_conn, child_conn = ctx.Pipe(duplex=True)
        process = ctx.Process(
            target=_parser_worker_entry,
            args=(self._factory, child_conn),
            daemon=True,
        )
        try:
            process.start()
        except BaseException:
            parent_conn.close()
            child_conn.close()
            raise
        # 父进程立即关闭子端 Pipe 句柄，确保子进程崩溃时 parent_conn.recv() 立即收到 EOFError。
        child_conn.close()
        worker = _ParserWorkerProcess(process, parent_conn)
        self._workers.add(worker)
        return worker

    async def _retire_worker(self, worker: _ParserWorkerProcess, *, force_kill: bool) -> None:
        """仅淘汰并关闭指定子进程，绝不波及同池其他正常在途的兄弟子进程。"""
        self._workers.discard(worker)
        try:
            await asyncio.shield(asyncio.to_thread(worker.close_sync, force_kill=force_kill))
        except BaseException:
            worker.close_sync(force_kill=True)
            raise

    async def parse(self, filename: str, content: bytes) -> ParsedDocument:
        """在独立 spawn 子进程中执行解析，覆盖单进程超时强杀、崩溃自愈与单槽位轮换。"""
        if self._closed:
            raise RuntimeError("knowledge_parser_pool_closed")
        await self._semaphore.acquire()
        worker: _ParserWorkerProcess | None = None
        force_kill = False
        should_recycle = False
        succeeded = False
        result: ParsedDocument | str = ""
        try:
            if self._closed:
                raise RuntimeError("knowledge_parser_pool_closed")
            worker = self._acquire_or_spawn_worker()
            try:
                succeeded, result, child_recycle = await asyncio.wait_for(
                    asyncio.to_thread(_exchange_task, worker, filename, content),
                    timeout=self._timeout_seconds,
                )
                should_recycle = child_recycle
            except TimeoutError:
                force_kill = True
                raise TimeoutError("knowledge_parse_timeout") from None
            except (EOFError, BrokenPipeError, OSError):
                force_kill = True
                raise RuntimeError("knowledge_parser_process_failed") from None
            except asyncio.CancelledError:
                force_kill = True
                raise
        finally:
            try:
                if worker is not None:
                    worker.tasks_completed += 1
                    if force_kill or self._closed or not worker.is_alive():
                        await self._retire_worker(worker, force_kill=True)
                    elif should_recycle or worker.tasks_completed >= self._max_tasks_per_child:
                        await self._retire_worker(worker, force_kill=False)
                    else:
                        self._idle.append(worker)
            finally:
                self._semaphore.release()

        if not succeeded:
            raise _decode_child_error(str(result))
        if not isinstance(result, ParsedDocument):
            raise RuntimeError("knowledge_parser_result_invalid")
        return result

    async def aclose(self) -> None:
        """幂等、抗取消地关闭全部解析子进程并解绑 finalizer。"""
        if self._closed and not self._workers:
            return
        self._closed = True
        self._idle.clear()
        # 逐个关闭后再移出 _workers，防止中途取消导致剩余进程脱离 weakref.finalize 兜底。
        for worker in list(self._workers):
            await asyncio.shield(asyncio.to_thread(worker.close_sync, force_kill=False))
            self._workers.discard(worker)
        if not self._workers:
            self._finalizer.detach()
