"""内容寻址本地存储层 — 管理原始文件和派生数据的持久化。

采用 SHA-256 内容寻址策略：相同内容的文件只存储一份，通过 hash 前缀
分桶避免单目录文件数过多。目录结构::

    {root}/
    ├── blobs/           # 原始文件（内容寻址，不可变）
    │   └── ab/          # 按 hash 前两字符分桶
    │       └── ab1234...  # 完整 SHA-256 作为文件名
    ├── artifacts/       # 派生产物（摘要、分析结果等）
    └── work/            # 临时工作目录（上传暂存、解压中间文件）
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator


class FileTooLargeError(ValueError):
    """上传文件超过大小限制时抛出。"""


@dataclass(frozen=True)
class StoredBlob:
    """已存储 blob 的元数据。

    Attributes:
        sha256: 文件内容的 SHA-256 哈希值（十六进制字符串）。
        size_bytes: 文件字节数。
        storage_key: 相对于存储根目录的路径键（如 ``blobs/ab/ab1234...``），
            用于后续 ``resolve()`` 获取绝对路径。
    """

    sha256: str
    size_bytes: int
    storage_key: str


class StorageLayer:
    """内容寻址本地存储层。

    管理文件 blob 的上传、路径解析、工作区创建和清理。
    blob 不可变：相同内容的文件只存储一份，通过 SHA-256 哈希去重。

    Args:
        root: 存储根目录的绝对路径。
        max_upload_bytes: 单文件上传大小上限（字节），
            超出时抛出 ``FileTooLargeError``。
    """

    def __init__(self, root: Path, max_upload_bytes: int) -> None:
        self.root = root
        self.max_upload_bytes = max_upload_bytes
        self.blob_root = root / "blobs"
        self.artifact_root = root / "artifacts"
        self.work_root = root / "work"
        for path in (self.blob_root, self.artifact_root, self.work_root):
            path.mkdir(parents=True, exist_ok=True)

    async def save_stream(self, chunks: AsyncIterator[bytes]) -> StoredBlob:
        """从异步字节流保存文件 blob。

        流式计算 SHA-256，写入临时文件后原子移动到最终位置。
        如果目标 blob 已存在（内容相同），直接丢弃临时文件。

        Args:
            chunks: 异步字节迭代器，通常来自 UploadFile 的分块读取。

        Returns:
            ``StoredBlob``：包含哈希值、字节数和存储键。

        Raises:
            FileTooLargeError: 累计字节数超过 ``max_upload_bytes``。
        """
        digest = hashlib.sha256()
        size = 0
        fd, temp_name = tempfile.mkstemp(prefix="upload-", dir=self.work_root)
        try:
            with os.fdopen(fd, "wb") as output:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > self.max_upload_bytes:
                        raise FileTooLargeError(
                            f"文件超过 {self.max_upload_bytes} bytes 上传上限"
                        )
                    digest.update(chunk)
                    output.write(chunk)
            sha256 = digest.hexdigest()
            key = f"blobs/{sha256[:2]}/{sha256}"
            destination = self.root / key
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                # 内容相同的 blob 已存在，丢弃临时文件即可
                os.unlink(temp_name)
            else:
                os.replace(temp_name, destination)
            return StoredBlob(sha256=sha256, size_bytes=size, storage_key=key)
        except Exception:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
            raise

    def resolve(self, storage_key: str) -> Path:
        """将存储键解析为绝对路径，并验证路径安全性。

        Args:
            storage_key: 相对存储路径（如 ``blobs/ab/ab1234...``）。

        Returns:
            解析后的绝对路径。

        Raises:
            PermissionError: 路径穿越存储根目录（安全校验失败）。
        """
        path = (self.root / storage_key).resolve()
        root = self.root.resolve()
        if path != root and root not in path.parents:
            raise PermissionError("非法存储路径")
        return path

    def create_workspace(self, attachment_id: str) -> Path:
        """为附件创建临时工作目录。

        工作目录位于 ``{root}/work/`` 下，用于适配器解压或生成中间文件。
        使用完毕后应调用 ``cleanup_workspace()`` 清理。

        Args:
            attachment_id: 附件 ID，用作目录名前缀便于识别。

        Returns:
            创建的临时目录绝对路径。
        """
        path = Path(tempfile.mkdtemp(prefix=f"{attachment_id}-", dir=self.work_root))
        return path

    def cleanup_workspace(self, path: Path) -> None:
        """清理临时工作目录。

        仅删除位于 ``work_root`` 下的目录，防止误删其他路径。

        Args:
            path: ``create_workspace()`` 返回的目录路径。
        """
        if path.exists() and self.work_root.resolve() in path.resolve().parents:
            shutil.rmtree(path, ignore_errors=True)

    def delete_blob(self, storage_key: str) -> bool:
        """删除指定 blob 文件。

        调用方需确保该 blob 已无活跃引用（即所有关联附件均已软删除）。

        Args:
            storage_key: blob 的存储键。

        Returns:
            是否成功删除（文件不存在或路径无效时返回 ``False``）。
        """
        path = self.resolve(storage_key)
        if not path.is_file() or path.parent == self.blob_root:
            return False
        path.unlink()
        return True

    def blob_keys(self) -> list[str]:
        """列出所有已存储 blob 的存储键。

        Returns:
            存储键列表（相对路径），按文件系统顺序排列。
        """
        if not self.blob_root.exists():
            return []
        return [str(path.relative_to(self.root)) for path in self.blob_root.glob("*/*") if path.is_file()]
