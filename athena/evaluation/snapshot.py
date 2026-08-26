"""隔离且可复现的评估工作区和快照清单。"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from athena.config.settings import Settings


@dataclass(frozen=True)
class SnapshotManifest:
    """描述一次可复现评估运行的输入、索引和模型版本。"""

    run_id: str
    dataset_id: str
    dataset_version: str
    settings_hash: str
    sqlite_sha256: str
    chroma_sha256: str
    embedding_model: str
    llm_model: str
    parser_version: str
    application_commit: str
    index_version: str
    created_at: str
    dataset_content_manifest_sha256: str = ""
    schema_version: str = "1"

    def write(self, path: Path) -> None:
        """将快照清单写入 JSON 文件。

        异常：
            OSError: 输出文件无法创建或写入。
        """
        path.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def read(cls, path: Path) -> "SnapshotManifest":
        """从 JSON 文件读取并校验快照清单。"""
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("snapshot manifest must be a JSON object")
        return cls(**value)


class WorkspaceLock:
    """由原子创建操作支持的排他锁，在关闭时释放。"""

    def __init__(self, path: Path) -> None:
        """创建基于原子文件创建的工作区锁。

        参数：
            path: 锁文件路径。
        """
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        """独占创建锁文件。

        异常：
            RuntimeError: 锁文件已存在。
            OSError: 锁文件无法创建。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self._fd, f"pid={os.getpid()}\n".encode())
        except FileExistsError as exc:
            raise RuntimeError(f"evaluation workspace is locked: {self.path}") from exc

    def release(self) -> None:
        """关闭锁文件描述符并删除锁文件；重复调用安全。"""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
            self.path.unlink(missing_ok=True)

    def __enter__(self) -> "WorkspaceLock":
        """获取锁并返回自身。"""
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        """退出上下文时释放锁。"""
        self.release()


class EvaluationWorkspace:
    """单次评估运行的目录布局和设置保护器。"""

    def __init__(self, root: Path, settings: Settings) -> None:
        """创建评估工作区对象。

        参数：
            root: 工作区根目录。
            settings: 用于隔离校验的配置。
        """
        self.root = root.resolve()
        self.settings = settings
        self.lock = WorkspaceLock(self.root / "lock")
        self._closed = False

    @property
    def db_path(self) -> Path:
        """返回工作区 SQLite 文件路径。"""
        return self.root / "athena.db"

    @property
    def chroma_path(self) -> Path:
        """返回工作区 ChromaDB 目录路径。"""
        return self.root / "chromadb"

    @property
    def files_path(self) -> Path:
        """返回工作区附件目录路径。"""
        return self.root / "files"

    def create(self) -> None:
        """创建目录、获取锁并验证路径隔离。

        异常：
            RuntimeError: 工作区与生产数据目录存在危险重叠。
            OSError: 目录或锁无法创建。
        """
        configured_paths = {
            Path(self.settings.sqlite_db_path).resolve(),
            self.settings.chroma_path.resolve(),
            self.settings.files_path.resolve(),
        }
        if configured_paths != {
            self.db_path.resolve(),
            self.chroma_path.resolve(),
            self.files_path.resolve(),
        }:
            self.assert_root_isolated(self.settings)
        self.root.mkdir(parents=True, exist_ok=True)
        for path in (self.chroma_path, self.files_path, self.root / "logs"):
            path.mkdir(parents=True, exist_ok=True)
        self.lock.acquire()
        try:
            self.assert_isolated()
        except Exception:
            self.lock.release()
            raise

    def assert_isolated(self) -> None:
        """拒绝使用默认生产数据目录或部分重叠路径。

        异常：
            RuntimeError: 配置路径与工作区或生产目录冲突。
        """
        configured = {
            Path(self.settings.sqlite_db_path).resolve(),
            self.settings.chroma_path.resolve(),
            self.settings.files_path.resolve(),
        }
        workspace_paths = {
            self.db_path.resolve(),
            self.chroma_path.resolve(),
            self.files_path.resolve(),
        }
        if configured & workspace_paths and configured != workspace_paths:
            raise RuntimeError(
                "evaluation settings partially overlap the evaluation workspace"
            )
        production_roots = {
            Path("./data/athena.db").resolve(),
            Path("./data/chromadb").resolve(),
            Path("./data/files").resolve(),
        }
        if configured & production_roots:
            raise RuntimeError(
                "evaluation runtime refuses to use the default production data directory"
            )
        for production_path in production_roots:
            if production_path == self.root or production_path in self.root.parents:
                raise RuntimeError(
                    "evaluation workspace overlaps a configured production path"
                )

    def assert_root_isolated(self, settings: Settings) -> None:
        """在替换配置前检查 workspace 不在任何生产数据树中。"""
        production_roots = {
            Path(settings.sqlite_db_path).resolve().parent,
            settings.chroma_path.resolve(),
            settings.files_path.resolve(),
            Path("./data").resolve(),
        }
        for root in production_roots:
            if self.root == root or root in self.root.parents or self.root in root.parents:
                raise RuntimeError(
                    "evaluation workspace overlaps a configured production path"
                )

    def isolated_settings(self) -> Settings:
        """返回将数据库、向量库和文件目录替换为工作区路径的配置。"""
        return self.settings.model_copy(
            update={
                "sqlite_db_path": str(self.db_path),
                "chromadb_path": str(self.chroma_path),
                "file_storage_path": str(self.files_path),
            }
        )

    def close(self) -> None:
        """释放工作区锁；重复调用安全。"""
        if not self._closed:
            self.lock.release()
            self._closed = True


def settings_hash(settings: Settings) -> str:
    """计算排除工作区物理路径后的配置 SHA-256 摘要。"""
    value = settings.model_dump(mode="json")
    value.update(
        {
            "sqlite_db_path": "<workspace>",
            "chromadb_path": "<workspace>",
            "file_storage_path": "<workspace>",
        }
    )
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def tree_hash(path: Path) -> str:
    """按相对路径和文件字节计算目录树 SHA-256 摘要。"""
    if not path.exists():
        return hashlib.sha256(b"").hexdigest()
    digest = hashlib.sha256()
    for item in sorted(p for p in path.rglob("*") if p.is_file()):
        digest.update(item.relative_to(path).as_posix().encode())
        digest.update(item.read_bytes())
    return digest.hexdigest()


def build_snapshot_manifest(
    *,
    run_id: str,
    dataset_id: str,
    dataset_version: str,
    dataset_content_manifest_sha256: str = "",
    settings: Settings,
    workspace: EvaluationWorkspace,
    embedding_model: str,
    llm_model: str,
    parser_version: str,
    application_commit: str = "unknown",
) -> SnapshotManifest:
    """根据当前工作区状态构造可复现快照清单。

    参数：
        run_id: 本次评估运行标识。
        dataset_id: 数据集标识。
        dataset_version: 数据集版本。
        dataset_content_manifest_sha256: 数据集内容摘要。
        settings: 实际使用的隔离配置。
        workspace: 已初始化的评估工作区。
        embedding_model: 向量模型名称。
        llm_model: LLM 模型名称。
        parser_version: 解析器版本。
        application_commit: 应用代码版本。

    返回值：
        包含数据库、向量库和配置摘要的快照清单。
    """
    index_version = hashlib.sha256(
        f"{tree_hash(workspace.db_path)}:{tree_hash(workspace.chroma_path)}".encode()
    ).hexdigest()
    return SnapshotManifest(
        run_id=run_id,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        dataset_content_manifest_sha256=dataset_content_manifest_sha256,
        settings_hash=settings_hash(settings),
        sqlite_sha256=tree_hash(workspace.db_path),
        chroma_sha256=tree_hash(workspace.chroma_path),
        embedding_model=embedding_model,
        llm_model=llm_model,
        parser_version=parser_version,
        application_commit=application_commit,
        index_version=index_version,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
