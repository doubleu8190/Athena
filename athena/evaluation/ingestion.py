"""通过生产记忆服务和文件服务准备快照。"""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Any, AsyncIterator

from athena.core.files.runtime import FileIntelligenceRuntime
from athena.core.memory.memory import MemoryManager
from athena.evaluation.bindings import BindingIndex
from athena.evaluation.models import DatasetManifest
from athena.evaluation.snapshot import EvaluationWorkspace
from athena.infrastructure.sqlite.database import Database
from athena.models.file import AttachmentStatus


async def _stream(path: Path, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
    """以固定块大小产生文件字节流。

    参数：
        path: 要读取的文件路径。
        chunk_size: 每块最大字节数。

    生成值：
        文件内容块。

    异常：
        OSError: 文件无法打开或读取。
    """
    with path.open("rb") as source:
        while data := source.read(chunk_size):
            yield data


async def ingest_dataset(
    *,
    dataset_root: Path,
    manifest: DatasetManifest,
    workspace: EvaluationWorkspace,
    database: Database,
    memory_manager: MemoryManager,
    file_runtime: FileIntelligenceRuntime,
) -> BindingIndex:
    """通过生产服务将记忆 JSONL 和语料文件导入隔离评估工作区。

    参数：
        dataset_root: 数据集根目录，支持 ``memories.jsonl`` 和 ``corpus/``。
        manifest: 用于构造隔离会话名的数据集清单。
        workspace: 当前评估工作区。
        database: 提供附件持久化接口的数据库门面。
        memory_manager: 提供 ``add_memory`` 的记忆服务。
        file_runtime: 提供存储、解析和索引能力的文件运行时。

    返回值：
        保存到工作区的别名绑定索引。

    异常：
        ValueError: 记忆行缺少别名或内容。
        OSError: 数据集或绑定文件无法读写。
        Exception: 底层导入、解析或索引服务失败。
    """
    bindings = BindingIndex()
    memories_path = dataset_root / "memories.jsonl"
    if memories_path.exists():
        for line_number, line in enumerate(
            memories_path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if not line.strip():
                continue
            value = json.loads(line)
            alias = value.get("memory_alias") or value.get("alias")
            if not alias or not isinstance(value.get("content"), str):
                raise ValueError(f"invalid memory at {memories_path}:{line_number}")
            memory_id = await memory_manager.add_memory(
                value["content"],
                value.get("metadata", {}),
                bool(value.get("pinned", False)),
            )
            bindings.bind_memory(alias, memory_id)

    corpus = dataset_root / "corpus"
    if corpus.exists():
        for source in sorted(path for path in corpus.rglob("*") if path.is_file()):
            alias = source.relative_to(corpus).as_posix()
            stored = await file_runtime.storage.save_stream(_stream(source))
            attachment = await database.files.create_attachment(
                session_id=f"evaluation-{manifest.version}",
                filename=alias,
                mime_type=mimetypes.guess_type(source.name)[0]
                or "application/octet-stream",
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
                storage_key=stored.storage_key,
            )
            bindings.bind_attachment(alias, attachment.id)
            try:
                await file_runtime.parse_attachment(attachment.id)
                index_result = await file_runtime.index_attachment(
                    attachment.id, mark_ready=False
                )
                if not index_result.get("vector_indexed"):
                    raise RuntimeError(f"file vector index is not ready for {alias}")
                ready = await database.files.update_attachment(
                    attachment.id,
                    status=AttachmentStatus.READY.value,
                    error_message=None,
                )
                if ready is None or ready.status != AttachmentStatus.READY:
                    raise RuntimeError(f"attachment did not become ready: {alias}")
            except Exception as exc:
                await database.files.update_attachment(
                    attachment.id,
                    status=AttachmentStatus.FAILED.value,
                    error_message=str(exc),
                )
                raise
            for chunk in await database.files.get_chunks(attachment.id, limit=100_000):
                locator_key = "&".join(
                    f"{key}={value}"
                    for key, value in sorted(chunk.locator.items())
                    if key not in {"char_start", "char_end"}
                )
                bindings.bind_locator(
                    alias,
                    locator_key,
                    [
                        *bindings.locator_to_chunk_ids.get(
                            f"{alias}#{locator_key}", []
                        ),
                        chunk.id,
                    ],
                )
    bindings.write(workspace.root / "bindings.json")
    return bindings
