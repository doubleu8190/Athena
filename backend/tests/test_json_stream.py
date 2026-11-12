"""流式 JSON 语法验证的有界内存与取消边界测试。"""

from __future__ import annotations

import threading
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO, cast

import pytest

from common.json_stream import (
    JsonStreamValidationCancelled,
    JsonStreamValidationError,
    validate_json_stream,
)

_LARGE_SCALAR_BYTES = 51 * 1024 * 1024
_DEEP_NESTING = 150_000


class _GatedBinaryStream:
    """在首次读取后暂停，供取消测试确认排序。"""

    def __init__(
        self,
        stream: BinaryIO,
        read_finished: threading.Event,
        release: threading.Event,
    ) -> None:
        """保存底层流和两个线程同步事件。"""
        self._stream = stream
        self._read_finished = read_finished
        self._release = release
        self._gated = False

    def read(self, size: int = -1) -> bytes:
        """执行一次底层读取，并在首次返回前等待测试放行。"""
        data = self._stream.read(size)
        if not self._gated:
            self._gated = True
            self._read_finished.set()
            assert self._release.wait(timeout=2)
        return data


def _write_repeated(
    path: Path,
    *,
    prefix: bytes,
    repeated_byte: bytes,
    repeat_count: int,
    suffix: bytes,
) -> None:
    """以固定内存块把大型单标量 JSON 写入磁盘。"""
    chunk = repeated_byte * (1024 * 1024)
    remaining = repeat_count
    with path.open("wb") as stream:
        stream.write(prefix)
        while remaining:
            write_size = min(remaining, len(chunk))
            stream.write(chunk[:write_size])
            remaining -= write_size
        stream.write(suffix)


def _write_deep_array(path: Path) -> None:
    """把指定深度的合法嵌套数组写入磁盘。"""
    chunk = b"[" * 8192
    remaining = _DEEP_NESTING
    with path.open("wb") as stream:
        while remaining:
            write_size = min(remaining, 8192)
            stream.write(chunk[:write_size])
            remaining -= write_size
        stream.write(b"null")
        remaining = _DEEP_NESTING
        while remaining:
            write_size = min(remaining, 8192)
            stream.write(b"]" * write_size)
            remaining -= write_size


def _peak_traced_bytes(action: Callable[[], None]) -> int:
    """执行验证动作并返回 tracemalloc 记录的增量峰值。"""
    tracemalloc.start()
    try:
        baseline = tracemalloc.get_traced_memory()[0]
        tracemalloc.reset_peak()
        action()
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    return peak - baseline


def _validate_path(
    path: Path,
    cancellation: threading.Event | None = None,
) -> None:
    """以二进制只读方式验证指定文件。"""
    with path.open("rb") as stream:
        validate_json_stream(stream, cancellation=cancellation)


@pytest.mark.parametrize(
    "payload",
    (
        b"null",
        b'{"text":"hello","items":[1,-2.5e+3,true,false,null]}',
        b'[{"nested":[]},{ },0,-0,0.0,1e-2]',
        b'"\\u20ac\\n\\t\\/"',
        b" \r\n [ ] \t",
    ),
)
def test_valid_json_is_accepted(
    tmp_path: Path,
    payload: bytes,
) -> None:
    """验证合法根标量、容器、转义和空白。"""
    path = tmp_path / "valid.json"
    path.write_bytes(payload)

    with path.open("rb") as stream:
        validate_json_stream(stream)


@pytest.mark.parametrize(
    ("payload", "code"),
    (
        (b"", "json_stream_invalid_json"),
        (b"01", "json_stream_invalid_json"),
        (b"1.", "json_stream_invalid_json"),
        (b"1e", "json_stream_invalid_json"),
        (b'{"a":1,}', "json_stream_invalid_json"),
        (b"[1,]", "json_stream_invalid_json"),
        (b'{"a" 1}', "json_stream_invalid_json"),
        (b'"\\u12"', "json_stream_invalid_json"),
        (b'"raw\nnewline"', "json_stream_invalid_json"),
        (b"true false", "json_stream_invalid_json"),
        (b'"\xe2\x82', "json_stream_invalid_utf8"),
        (b"\xff", "json_stream_invalid_utf8"),
    ),
)
def test_invalid_json_has_stable_error_code(
    tmp_path: Path,
    payload: bytes,
    code: str,
) -> None:
    """验证非法 JSON 和非法 UTF-8 明确失败。"""
    path = tmp_path / "invalid.json"
    path.write_bytes(payload)

    with path.open("rb") as stream, pytest.raises(
        JsonStreamValidationError,
        match=code,
    ):
        validate_json_stream(stream)


@pytest.mark.parametrize(
    ("case_id", "prefix", "suffix"),
    (
        ("string", b'"', b'"'),
        ("key", b'{"', b'":0}'),
    ),
)
def test_large_string_and_key_use_bounded_memory(
    tmp_path: Path,
    case_id: str,
    prefix: bytes,
    suffix: bytes,
) -> None:
    """验证超过 50 MiB 的单字符串和单对象键不构造成完整 scalar。"""
    path = tmp_path / f"{case_id}.json"
    _write_repeated(
        path,
        prefix=prefix,
        repeated_byte=b"x",
        repeat_count=_LARGE_SCALAR_BYTES,
        suffix=suffix,
    )

    peak = _peak_traced_bytes(
        lambda: _validate_path(path),
    )

    assert peak < 8 * 1024 * 1024


def test_huge_number_uses_bounded_memory(tmp_path: Path) -> None:
    """验证跨多个读取块且超过一百万位的数字不构造完整数字对象。"""
    path = tmp_path / "huge-number.json"
    _write_repeated(
        path,
        prefix=b"1",
        repeated_byte=b"0",
        repeat_count=2 * 1024 * 1024,
        suffix=b"",
    )

    peak = _peak_traced_bytes(lambda: _validate_path(path))

    assert peak < 8 * 1024 * 1024


def test_deep_nesting_uses_bounded_memory(tmp_path: Path) -> None:
    """验证深层容器使用文件后备栈，内存不随深度线性增长。"""
    path = tmp_path / "deep.json"
    _write_deep_array(path)

    peak = _peak_traced_bytes(lambda: _validate_path(path))

    assert peak < 8 * 1024 * 1024


def test_utf8_multibyte_character_split_across_read_boundary(
    tmp_path: Path,
) -> None:
    """验证多字节 UTF-8 字符跨 64 KiB 读取边界时正确续接。"""
    path = tmp_path / "utf8-boundary.json"
    path.write_bytes(b'"' + b"a" * (64 * 1024 - 2) + "€".encode() + b'"')

    with path.open("rb") as stream:
        validate_json_stream(stream)


def test_cancellation_stops_active_validation(tmp_path: Path) -> None:
    """验证读取边界发生后取消事件使活动验证尽快停止。"""
    path = tmp_path / "cancel.json"
    _write_repeated(
        path,
        prefix=b'"',
        repeated_byte=b"x",
        repeat_count=2 * 1024 * 1024,
        suffix=b'"',
    )
    cancellation = threading.Event()
    read_finished = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def run_validation() -> None:
        """在线程中运行可取消的验证。"""
        with path.open("rb") as stream:
            gated_stream = _GatedBinaryStream(stream, read_finished, release)
            try:
                validate_json_stream(
                    cast(BinaryIO, gated_stream),
                    cancellation=cancellation,
                )
            except BaseException as error:
                errors.append(error)

    thread = threading.Thread(target=run_validation)
    thread.start()
    assert read_finished.wait(timeout=1)
    cancellation.set()
    release.set()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], JsonStreamValidationCancelled)
