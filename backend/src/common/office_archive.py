"""Office Open XML（DOCX/XLSX/PPTX）ZIP 容器的统一安全校验工具。"""

from __future__ import annotations

import re
import threading
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Literal

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

OfficeArchiveErrorCode = Literal[
    "bad_zip",
    "unsafe_member_path",
    "member_limit",
    "expanded_size_limit",
    "encrypted",
    "macro_unsupported",
    "signature_invalid",
]


class OfficeArchiveError(ValueError):
    """Office ZIP 容器校验失败，携带结构化错误码供上层精确映射。"""

    def __init__(self, code: OfficeArchiveErrorCode) -> None:
        super().__init__(code)
        self.code: OfficeArchiveErrorCode = code


class _ArchiveCancelled(BaseException):
    """隔离取消回调抛出的异常，防止被 ZIP 异常捕获块误吞。"""

    def __init__(self, inner: BaseException) -> None:
        self.inner = inner


def validate_zip_member_path(name: str) -> None:
    """拒绝控制字符、绝对路径、反斜杠、空段和目录穿越。"""
    trimmed = name[:-1] if name.endswith("/") else name
    parts = trimmed.split("/")
    if (
        not trimmed
        or "\x00" in name
        or _CONTROL.search(name)
        or name.startswith("/")
        or "\\" in name
        or (len(name) >= 2 and name[1] == ":")
        or "//" in name
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise OfficeArchiveError("unsafe_member_path")


def _check_cancellation(
    cancellation: threading.Event | None,
    on_cancelled: Callable[[], None] | None,
) -> None:
    if cancellation is not None and cancellation.is_set():
        if on_cancelled is not None:
            try:
                on_cancelled()
            except BaseException as exc:
                raise _ArchiveCancelled(exc) from None
        raise OfficeArchiveError("bad_zip")


def validate_office_archive(
    path: Path,
    *,
    required_marker: str,
    check_limits_and_macros: bool = False,
    case_insensitive_markers: bool = False,
    max_members: int = 10_000,
    max_expanded_bytes: int = 200 * 1024 * 1024,
    max_content_types_bytes: int = 10 * 1024 * 1024,
    cancellation: threading.Event | None = None,
    on_cancelled: Callable[[], None] | None = None,
    member_path_validator: Callable[[str], None] = validate_zip_member_path,
) -> None:
    """校验 Office ZIP 容器的路径安全、结构标记及可选的资源/宏上限。"""
    names: list[str] = []
    try:
        try:
            with zipfile.ZipFile(path, "r") as archive:
                members = archive.infolist()
                _check_cancellation(cancellation, on_cancelled)
                if check_limits_and_macros:
                    if len(members) > max_members:
                        raise OfficeArchiveError("member_limit")
                    if sum(member.file_size for member in members) > max_expanded_bytes:
                        raise OfficeArchiveError("expanded_size_limit")
                    if any(member.flag_bits & 0x1 for member in members):
                        raise OfficeArchiveError("encrypted")
                # 路径安全校验完成前不读取任何成员正文，也不因提前命中 marker 而跳过危险成员。
                for member in members:
                    _check_cancellation(cancellation, on_cancelled)
                    name = member.orig_filename
                    member_path_validator(name)
                    names.append(name)
                    _check_cancellation(cancellation, on_cancelled)
                if check_limits_and_macros:
                    lower_names = {name.lower() for name in names}
                    if any(
                        "vbaproject" in name or name.endswith((".xlsm", ".docm", ".pptm"))
                        for name in lower_names
                    ):
                        raise OfficeArchiveError("macro_unsupported")
                    for member in members:
                        if member.orig_filename.lower() != "[content_types].xml":
                            continue
                        # 对 [Content_Types].xml 实施声明大小与实际解压流双边界检查，
                        # 防范伪造 ZIP 头炸弹。
                        if member.file_size > max_content_types_bytes:
                            raise OfficeArchiveError("expanded_size_limit")
                        with archive.open(member) as stream:
                            raw = stream.read(max_content_types_bytes + 1)
                        if len(raw) > max_content_types_bytes:
                            raise OfficeArchiveError("expanded_size_limit")
                        if "macroenabled" in raw.decode("utf-8", "ignore").lower():
                            raise OfficeArchiveError("macro_unsupported")
        except _ArchiveCancelled as cancelled:
            raise cancelled.inner from None
    except OfficeArchiveError:
        raise
    except (
        EOFError,
        OSError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as error:
        if isinstance(error, _ArchiveCancelled):  # pragma: no cover - defensive
            raise error.inner from None
        # 允许调用方自定义路径校验器抛出自定义异常（如 FileInputError）。
        if error.__class__.__module__ != "builtins" and not isinstance(
            error, (zipfile.BadZipFile, zipfile.LargeZipFile, OSError)
        ):
            raise
        raise OfficeArchiveError("bad_zip") from error

    if case_insensitive_markers:
        candidate_names = {name.lower() for name in names}
        has_content_types = "[content_types].xml" in candidate_names
        has_marker = required_marker.lower() in candidate_names
    else:
        has_content_types = "[Content_Types].xml" in names
        has_marker = required_marker in names
    if not has_content_types or not has_marker:
        raise OfficeArchiveError("signature_invalid")


__all__ = [
    "OfficeArchiveError",
    "OfficeArchiveErrorCode",
    "validate_office_archive",
    "validate_zip_member_path",
]
