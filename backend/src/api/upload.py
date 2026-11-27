"""HTTP multipart 文件的有界临时落盘。"""

from __future__ import annotations

import asyncio
import tempfile
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import IO

from fastapi import UploadFile


class UploadTooLargeError(ValueError):
    """上传实际读取字节超过入口上限。"""


class _StagedUploadState:
    """用线程锁串行化临时文件的创建登记、写入、关闭与清理，防止取消时多线程竞争。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.handle: IO[bytes] | None = None
        self.path: Path | None = None
        self.closed = False
        self.cancelled = False


def _unlink_path_quietly(path: Path) -> None:
    """尽力删除临时文件，忽略文件系统清理异常。"""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _create_staged_file(state: _StagedUploadState) -> Path | None:
    """在工作线程创建临时落盘文件；若创建前后已被取消则当场同步关闭并删除。"""
    with state.lock:
        if state.cancelled:
            return None
    staged = tempfile.NamedTemporaryFile(
        mode="w+b",
        prefix="athena-upload-",
        suffix=".bin",
        delete=False,
    )
    path = Path(staged.name)
    with state.lock:
        if state.cancelled:
            try:
                staged.close()
            except OSError:
                pass
            finally:
                _unlink_path_quietly(path)
            return None
        state.handle = staged
        state.path = path
        return path


def _write_chunk(state: _StagedUploadState, chunk: bytes) -> None:
    """在锁内校验句柄状态后写入单个分块，与关闭和清理操作严格互斥。"""
    with state.lock:
        if state.closed or state.cancelled or state.handle is None:
            return
        state.handle.write(chunk)


def _close_staged_file(state: _StagedUploadState) -> None:
    """写入完成后在锁内刷盘并关闭句柄，保留路径供下游读取。"""
    with state.lock:
        handle = state.handle
        state.handle = None
        state.closed = True
        if handle is not None:
            handle.close()


def _cleanup_staged_file(state: _StagedUploadState) -> None:
    """在锁内标记取消并幂等关闭句柄、删除临时文件，确保任意退出路径零泄漏。"""
    with state.lock:
        state.cancelled = True
        state.closed = True
        handle = state.handle
        state.handle = None
        path = state.path
        state.path = None
        try:
            if handle is not None:
                handle.close()
        except OSError:
            pass
        finally:
            if path is not None:
                _unlink_path_quietly(path)


@asynccontextmanager
async def staged_upload(
    upload: UploadFile,
    *,
    maximum_bytes: int,
    too_large_code: str = "upload_too_large",
) -> AsyncIterator[Path]:
    """边读边计数并通过工作线程落盘到临时文件，在所有退出与取消路径安全删除。"""
    state = _StagedUploadState()
    try:
        staged_path = await asyncio.to_thread(_create_staged_file, state)
        if staged_path is None:
            raise asyncio.CancelledError
        size = 0
        while chunk := await upload.read(128 * 1024):
            size += len(chunk)
            if size > maximum_bytes:
                raise UploadTooLargeError(too_large_code)
            await asyncio.to_thread(_write_chunk, state, chunk)
        if size == 0:
            raise ValueError("upload_empty")
        # 写入结束后先刷盘并关闭文件句柄，保证下游只读取已完整落盘的文件。
        await asyncio.to_thread(_close_staged_file, state)
        yield staged_path
    finally:
        # 优先在工作线程执行并屏蔽单次取消；若等待清理期间再遇取消/异常，
        # 立即在当前线程同步加锁幂等清理后再重新抛出，绝不泄漏句柄或临时文件。
        try:
            await asyncio.shield(asyncio.to_thread(_cleanup_staged_file, state))
        except BaseException:
            _cleanup_staged_file(state)
            raise


__all__ = ["UploadTooLargeError", "staged_upload"]
