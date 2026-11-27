"""会话文件的发布、类型交叉校验、文本分页和流式下载用例。"""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import logging
import re
import shutil
import tempfile
import threading
import unicodedata
import zipfile
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal, NoReturn

from common.async_utils import run_thread_shielded
from common.date import utc_now
from common.ids import new_id
from domain.file import (
    EXTRACTABLE_DOCUMENT_EXTENSIONS,
    MAX_SESSION_DOCUMENT_EXTRACT_BYTES,
    MAX_SESSION_EXTRACTED_TEXT_BYTES,
    MAX_SESSION_FILE_BYTES,
    MAX_VISION_ENCODED_IMAGE_BYTES,
    MAX_VISION_RAW_IMAGE_BYTES,
    VISION_IMAGE_MIME_TYPES,
    NormalizedVisionImage,
    PendingSessionFile,
    SessionDocumentExtractor,
    SessionFile,
    SessionFileExtractionStatus,
    SessionFilePage,
    SessionFileRepository,
    SessionFileTextSlice,
    TextFileReadError,
    VisionImageError,
    VisionImageNormalizer,
)
from domain.file.ports import FileObjectStore
from domain.session import SessionNotFoundError

logger = logging.getLogger(__name__)

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_IDEMPOTENCY_KEY = re.compile(r"^[\x21-\x7e]{1,128}$")
_TEXT_TYPES = {
    ".txt": ("text/plain", {"text/plain"}),
    ".csv": ("text/csv", {"text/csv", "text/plain", "application/csv"}),
    ".json": ("application/json", {"application/json", "text/json", "text/plain"}),
    ".md": ("text/markdown", {"text/markdown", "text/plain"}),
    ".log": ("text/plain", {"text/plain"}),
}
_BINARY_TYPES = {
    ".xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/zip",
        },
    ),
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/zip",
        },
    ),
    ".pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        {
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/zip",
        },
    ),
    ".pdf": ("application/pdf", {"application/pdf"}),
    ".png": ("image/png", {"image/png"}),
    ".jpg": ("image/jpeg", {"image/jpeg", "image/jpg"}),
    ".jpeg": ("image/jpeg", {"image/jpeg", "image/jpg"}),
    ".webp": ("image/webp", {"image/webp"}),
}
_GENERIC_MIME = {"", "application/octet-stream", "binary/octet-stream"}
_SAFE_PUBLISH_REJECTIONS = {
    "idempotency_conflict",
    "session_file_quota_exceeded",
}


@dataclass(frozen=True, slots=True)
class _FileInspection:
    """一次线程内完成的文件大小、类型和摘要结果。"""

    size_bytes: int
    mime_type: str
    sha256: str


class FileInputError(ValueError):
    """文件内容或输入不符合会话白名单。"""

    def __init__(self, code: str) -> None:
        """保存稳定错误码。"""
        super().__init__(code)
        self.code = code


class _FileInspectionCancelled(RuntimeError):
    """调用方取消后在文件检查线程内使用的统一退出信号。"""


def sanitize_filename(value: str) -> str:
    """去除路径与控制字符，只保留纯文本安全文件名。"""
    if not isinstance(value, str):
        raise FileInputError("file_name_invalid")
    normalized = unicodedata.normalize("NFC", value.replace("\\", "/"))
    filename = PurePosixPath(normalized).name.strip()
    if not filename or filename in {".", ".."} or len(filename) > 255 or _CONTROL.search(filename):
        raise FileInputError("file_name_invalid")
    return filename


def validate_file_type(
    path: Path,
    filename: str,
    declared_mime: str | None,
    *,
    cancellation: threading.Event | None = None,
) -> str:
    """按扩展名、实际内容特征和可选 MIME 交叉校验并返回规范 MIME。"""
    extension = Path(filename).suffix.lower()
    if extension in _TEXT_TYPES:
        expected, aliases = _TEXT_TYPES[extension]
        _validate_declared_mime(declared_mime, aliases)
        if extension == ".json":
            _validate_json_document(path, cancellation=cancellation)
        else:
            _strict_utf8(path, cancellation=cancellation)
        return expected
    if extension not in _BINARY_TYPES:
        raise FileInputError("file_type_unsupported")
    expected, aliases = _BINARY_TYPES[extension]
    _validate_declared_mime(declared_mime, aliases)
    if extension in {".xlsx", ".docx", ".pptx"}:
        required = {
            ".xlsx": "xl/workbook.xml",
            ".docx": "word/document.xml",
            ".pptx": "ppt/presentation.xml",
        }[extension]
        _validate_office_archive(
            path,
            required_marker=required,
            cancellation=cancellation,
        )
        return expected
    with path.open("rb") as stream:
        header = stream.read(16)
    signatures = {
        ".pdf": header.startswith(b"%PDF-"),
        ".png": header.startswith(b"\x89PNG\r\n\x1a\n"),
        ".jpg": header.startswith(b"\xff\xd8\xff"),
        ".jpeg": header.startswith(b"\xff\xd8\xff"),
        ".webp": (len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP"),
    }
    if not signatures[extension]:
        raise FileInputError("file_content_mismatch")
    return expected


def _validate_office_archive(
    path: Path,
    *,
    required_marker: str,
    cancellation: threading.Event | None,
) -> None:
    """用标准库读取中央目录，先校验成员路径再核对 Office marker。

    参数:
        path: 待检查的 Office ZIP 文件。
        required_marker: xlsx、docx 或 pptx 对应格式的根 marker。
        cancellation: 调用方取消时设置的线程停止事件。

    返回:
        无返回值。

    异常:
        文件不是可读取 ZIP、成员路径不安全或缺少 marker 时抛出
        ``FileInputError``；检查线程被取消时抛出内部取消异常。
    """
    try:
        with zipfile.ZipFile(path, "r") as archive:
            members = archive.infolist()
            _raise_if_cancelled(cancellation)
            names: list[str] = []
            # 标准库会一次载入中央目录；路径检查完成前不读取任何成员正文，
            # 也不把 marker 命中提前为跳过危险成员的捷径。
            for member in members:
                _raise_if_cancelled(cancellation)
                name = member.orig_filename
                _validate_zip_member_path(name)
                names.append(name)
                _raise_if_cancelled(cancellation)
    except _FileInspectionCancelled:
        raise
    except (
        EOFError,
        OSError,
        RuntimeError,
        ValueError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as error:
        raise FileInputError("file_content_mismatch") from error
    if "[Content_Types].xml" not in names or required_marker not in names:
        raise FileInputError("file_content_mismatch")


def _validate_zip_member_path(name: str) -> None:
    """拒绝控制字符、绝对路径、反斜杠、空段和目录穿越。

    参数:
        name: ``ZipInfo.orig_filename`` 返回的原始成员名。

    返回:
        无返回值；路径安全时直接结束。

    异常:
        成员名不安全时抛出 ``FileInputError("file_content_mismatch")``。
    """
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
        raise FileInputError("file_content_mismatch")


def sha256_file(
    path: Path,
    *,
    cancellation: threading.Event | None = None,
) -> str:
    """流式计算摘要，并在每个读取块边界响应取消。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            _raise_if_cancelled(cancellation)
            chunk = stream.read(128 * 1024)
            if not chunk:
                break
            _raise_if_cancelled(cancellation)
            digest.update(chunk)
    _raise_if_cancelled(cancellation)
    return digest.hexdigest()


class SessionFileService:
    """统一发布用户上传和脚本清单声明的产物。"""

    def __init__(
        self,
        repository: SessionFileRepository,
        object_store: FileObjectStore,
        *,
        document_extractor: SessionDocumentExtractor | None = None,
        image_normalizer: VisionImageNormalizer | None = None,
        id_factory: Callable[[str], str] = new_id,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """保存文件元数据仓储、对象存储、可选文档提取器、图片归一化器和业务标识生成器。"""
        self._repository = repository
        self._object_store = object_store
        self._document_extractor = document_extractor
        self._image_normalizer = image_normalizer
        self._id_factory = id_factory
        self._clock = clock

    async def upload(
        self,
        session_id: str,
        owner_id: str,
        *,
        idempotency_key: str,
        filename: str,
        declared_mime: str | None,
        path: Path,
    ) -> SessionFile:
        """先写私有对象，再按数量配额原子发布上传元数据。"""
        idempotency_key = _validate_idempotency_key(idempotency_key)
        safe_filename = sanitize_filename(filename)
        # 用户上传上限必须在格式检测和完整摘要前生效，避免超大文件白白消耗 I/O。
        size = await asyncio.to_thread(lambda: path.stat().st_size)
        if size < 0 or size > MAX_SESSION_FILE_BYTES:
            raise FileInputError("document_file_too_large")
        inspection = await _inspect_file_cancellable(
            path,
            safe_filename,
            declared_mime,
        )
        mime_type = inspection.mime_type
        digest = inspection.sha256
        fingerprint = _upload_fingerprint(
            session_id=session_id,
            owner_id=owner_id,
            filename=safe_filename,
            mime_type=mime_type,
            size_bytes=size,
            sha256=digest,
        )
        existing = await self._repository.find_upload_request(
            session_id,
            owner_id,
            idempotency_key,
        )
        if existing is not None:
            if existing.sha256 != digest or existing.filename != safe_filename:
                raise FileInputError("idempotency_conflict")
            return existing
        return await self._write_and_publish_upload(
            session_id,
            owner_id,
            idempotency_key=idempotency_key,
            filename=safe_filename,
            mime_type=mime_type,
            size_bytes=size,
            digest=digest,
            fingerprint=fingerprint,
            path=path,
        )

    async def _write_and_publish_upload(
        self,
        session_id: str,
        owner_id: str,
        *,
        idempotency_key: str,
        filename: str,
        mime_type: str,
        size_bytes: int,
        digest: str,
        fingerprint: str,
        path: Path,
    ) -> SessionFile:
        """先写私有原件及可选提取缓存，再在数据库事务中原子提交文件引用。"""
        file_id = self._id_factory("file")
        key = self._upload_key(digest, file_id)
        extraction_status = SessionFileExtractionStatus.NONE
        extracted_bytes: bytes | None = None
        extracted_key: str | None = None
        extracted_text_bytes: int | None = None
        extraction_error_code: str | None = None

        # 仅对 PDF / Office 附件且注入了提取器时执行有界自动解析；任何解析失败都不阻断原件上传。
        if (
            Path(filename).suffix.lower() in EXTRACTABLE_DOCUMENT_EXTENSIONS
            and self._document_extractor is not None
        ):
            (
                extraction_status,
                extracted_bytes,
                extraction_error_code,
            ) = await self._extract_document_from_path(filename, path)

        try:
            await self._object_store.put_file(key, path, content_type=mime_type)
        except BaseException:
            await self._delete_orphan(key)
            raise

        if extraction_status is SessionFileExtractionStatus.READY and extracted_bytes is not None:
            candidate_extracted_key = self._extracted_text_key(digest, file_id)
            try:
                await self._object_store.put_bytes(
                    candidate_extracted_key,
                    extracted_bytes,
                    content_type="text/plain; charset=utf-8",
                )
            except asyncio.CancelledError:
                await self._delete_orphan(candidate_extracted_key)
                await self._delete_orphan(key)
                raise
            except Exception:
                # 提取缓存写入遇对象存储瞬态故障时不阻断原件发布，也不过早写死为 failed；
                # 清理残留缓存对象并回落为 none，允许首次读取时由惰性补解析自动恢复。
                await self._delete_orphan(candidate_extracted_key)
                extraction_status = SessionFileExtractionStatus.NONE
                extracted_key = None
                extracted_text_bytes = None
                extraction_error_code = None
            else:
                extracted_key = candidate_extracted_key
                extracted_text_bytes = len(extracted_bytes)

        pending = PendingSessionFile(
            id=file_id,
            filename=filename,
            label=None,
            mime_type=mime_type,
            size_bytes=size_bytes,
            sha256=digest,
            object_key=key,
            output_index=-1,
            created_at=self._clock(),
            extraction_status=extraction_status,
            extracted_text_key=extracted_key,
            extracted_text_bytes=extracted_text_bytes,
            extraction_error_code=extraction_error_code,
        )
        try:
            result = await self._repository.publish_upload(
                session_id,
                owner_id,
                pending,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                published_at=self._clock(),
            )
            if result.id != file_id:
                # 并发同键请求可能都在写入前查询为空；败者必须同时删除自己的原件与提取缓存对象。
                await self._abandon_unused_upload_objects(
                    result,
                    object_key=key,
                    extracted_text_key=extracted_key,
                )
            return result
        except SessionNotFoundError:
            # 会话不存在或已删除属于确定性拒绝，数据库未写入任何引用，立即清理原件与缓存孤儿对象。
            await self._abandon_upload_objects(key, extracted_key)
            raise
        except ValueError as error:
            replay_known, replay = await self._replay_upload_after_failure(
                session_id,
                owner_id,
                idempotency_key,
            )
            if replay is not None and replay.sha256 == digest and replay.filename == filename:
                await self._abandon_unused_upload_objects(
                    replay,
                    object_key=key,
                    extracted_text_key=extracted_key,
                )
                return replay
            if replay_known and str(error) in _SAFE_PUBLISH_REJECTIONS:
                await self._abandon_upload_objects(key, extracted_key)
            if str(error) == "idempotency_conflict":
                raise FileInputError("idempotency_conflict") from error
            raise
        except asyncio.CancelledError:
            # 取消可能发生在数据库提交后的响应阶段；保留对象并依靠幂等重查确认。
            raise
        except Exception:
            _, replay = await self._replay_upload_after_failure(
                session_id,
                owner_id,
                idempotency_key,
            )
            if replay is not None and replay.sha256 == digest and replay.filename == filename:
                await self._abandon_unused_upload_objects(
                    replay,
                    object_key=key,
                    extracted_text_key=extracted_key,
                )
                return replay
            # 未知异常无法区分回滚与提交后回包失败，禁止立即删除潜在引用对象。
            raise

    async def prepare_artifact(
        self,
        *,
        source_call_id: str,
        output_index: int,
        path: Path,
        filename: str,
        label: str,
    ) -> PendingSessionFile:
        """校验并私有上传脚本产物，等待与调用结果同事务发布。"""
        if output_index < 0:
            raise FileInputError("session_file_output_index_invalid")
        safe_filename = sanitize_filename(filename)
        # 脚本产物与用户上传统一执行单文件 50 MiB 上限，先于内容检查与对象上传拦截超大文件。
        size = await asyncio.to_thread(lambda: path.stat().st_size)
        if size < 0 or size > MAX_SESSION_FILE_BYTES:
            raise FileInputError("session_file_too_large")
        inspection = await _inspect_file_cancellable(
            path,
            safe_filename,
            None,
        )
        mime_type = inspection.mime_type
        size = inspection.size_bytes
        if size < 0 or size > MAX_SESSION_FILE_BYTES:
            raise FileInputError("session_file_too_large")
        digest = inspection.sha256
        file_id = self._id_factory("file")
        call_digest = hashlib.sha256(source_call_id.encode("utf-8")).hexdigest()
        key = f"session-artifacts/{call_digest[:2]}/{call_digest}/{file_id}"
        try:
            await self._object_store.put_file(key, path, content_type=mime_type)
        except BaseException:
            await self._delete_orphan(key)
            raise
        return PendingSessionFile(
            id=file_id,
            filename=safe_filename,
            label=label,
            mime_type=mime_type,
            size_bytes=size,
            sha256=digest,
            object_key=key,
            output_index=output_index,
            created_at=self._clock(),
        )

    async def abandon_artifacts(
        self,
        artifacts: tuple[PendingSessionFile, ...],
    ) -> None:
        """明确放弃尚未进入 Run 完成事务的已知产物（含原件及可选提取缓存）。"""
        for artifact in artifacts:
            await self._delete_orphan(artifact.object_key)
            if artifact.extracted_text_key is not None:
                await self._delete_orphan(artifact.extracted_text_key)

    async def list(
        self,
        session_id: str,
        owner_id: str,
        *,
        after_file_id: str | None,
        limit: int,
    ) -> SessionFilePage:
        """读取会话文件分页。"""
        page = await self._repository.list_visible(
            session_id,
            owner_id,
            after_file_id=after_file_id,
            limit=limit,
        )
        if page is None:
            raise SessionNotFoundError("session_not_found")
        return page

    async def get(
        self,
        session_id: str,
        owner_id: str,
        file_id: str,
    ) -> SessionFile:
        """读取已授权文件元数据。"""
        result = await self._repository.get_visible(session_id, owner_id, file_id)
        if result is None:
            raise FileInputError("session_file_not_found")
        return result

    async def get_many(
        self,
        session_id: str,
        owner_id: str,
        file_ids: tuple[str, ...],
    ) -> tuple[SessionFile, ...]:
        """批量关联脚本输入文件并拒绝跨会话或已失效 ID。"""
        try:
            return await self._repository.get_visible_many(
                session_id,
                owner_id,
                file_ids,
            )
        except ValueError as error:
            if str(error) == "session_file_not_found":
                raise FileInputError("session_file_not_found") from error
            if str(error) == "session_file_ids_duplicate":
                raise FileInputError("session_file_ids_duplicate") from error
            raise

    async def iter_download(self, file: SessionFile) -> AsyncGenerator[bytes, None]:
        """按鉴权后的对象键分块读取，不暴露存储 URL。"""
        async for chunk in self._object_store.iter_range(file.object_key):
            yield chunk

    async def load_vision_image(self, file: SessionFile) -> NormalizedVisionImage:
        """按 20 MiB 上限从私有对象存储读取白名单图片并完成隐私剥离与安全归一化。"""
        if file.mime_type not in VISION_IMAGE_MIME_TYPES:
            raise VisionImageError("vision_image_unsupported")
        if self._image_normalizer is None:
            raise VisionImageError("vision_image_unsupported")
        # 元数据层面先按 20 MiB 快速拦截，避免超限大文件产生无效对象存储读取。
        if file.size_bytes > MAX_VISION_RAW_IMAGE_BYTES:
            raise VisionImageError("vision_image_too_large")
        try:
            raw_bytes = await self._object_store.get_bytes(
                file.object_key,
                maximum_bytes=MAX_VISION_RAW_IMAGE_BYTES,
            )
        except asyncio.CancelledError:
            raise
        except ValueError as error:
            if str(error) == "object_too_large":
                raise VisionImageError("vision_image_too_large") from None
            logger.warning(
                "session_vision_image_read_failed file_id=%s exception_type=%s",
                file.id,
                type(error).__name__,
            )
            raise VisionImageError("vision_image_invalid") from None
        except Exception as error:
            if getattr(error, "code", None) == "file_storage_object_too_large":
                raise VisionImageError("vision_image_too_large") from None
            logger.warning(
                "session_vision_image_read_failed file_id=%s exception_type=%s",
                file.id,
                type(error).__name__,
            )
            raise VisionImageError("vision_image_invalid") from None

        normalized = await self._image_normalizer.normalize(
            file_id=file.id,
            filename=file.filename,
            declared_mime_type=file.mime_type,
            raw_bytes=raw_bytes,
        )
        # 二次防线：确保自定义或底层归一化实现输出严格不超过 4 MiB（base64 编码前字节）。
        if normalized.size_bytes > MAX_VISION_ENCODED_IMAGE_BYTES:
            raise VisionImageError("vision_image_too_large")
        return normalized

    async def read_text(
        self,
        session_id: str,
        owner_id: str,
        file_id: str,
        *,
        offset: int,
        limit_bytes: int,
    ) -> SessionFileTextSlice:
        """按严格 UTF-8 字符边界分段读取白名单纯文本或 PDF / Office 自动提取缓存。"""
        if offset < 0 or not 1 <= limit_bytes <= 64 * 1024:
            raise FileInputError("session_file_range_invalid")
        file = await self.get(session_id, owner_id, file_id)
        extension = Path(file.filename).suffix.lower()
        if extension in _TEXT_TYPES:
            return await self._read_utf8_slice(
                file=file,
                object_key=file.object_key,
                total_bytes=file.size_bytes,
                offset=offset,
                limit_bytes=limit_bytes,
                source_kind="raw_text",
            )
        if extension in EXTRACTABLE_DOCUMENT_EXTENSIONS:
            if file.extraction_status is SessionFileExtractionStatus.FAILED:
                raise TextFileReadError(
                    file.extraction_error_code or "document_parse_failed",
                    file,
                )
            if file.extraction_status is SessionFileExtractionStatus.NONE:
                if self._document_extractor is None:
                    raise TextFileReadError("file_not_text", file)
                # 历史未解析文件或工具/Skill 产出的 PDF/Office 文档，
                # 在首次读取时惰性补解析并回填缓存。
                file = await self._lazy_extract_document(file)
            if file.extracted_text_key is None or file.extracted_text_bytes is None:
                raise TextFileReadError("document_parse_failed", file)
            return await self._read_utf8_slice(
                file=file,
                object_key=file.extracted_text_key,
                total_bytes=file.extracted_text_bytes,
                offset=offset,
                limit_bytes=limit_bytes,
                source_kind="extracted_document",
            )
        raise TextFileReadError("file_not_text", file)

    async def _read_utf8_slice(
        self,
        *,
        file: SessionFile,
        object_key: str,
        total_bytes: int,
        offset: int,
        limit_bytes: int,
        source_kind: Literal["raw_text", "extracted_document"],
    ) -> SessionFileTextSlice:
        """从指定对象键按 UTF-8 字符边界精确读取字节分片并推进游标。"""
        if offset > total_bytes:
            raise TextFileReadError("file_offset_invalid", file)
        chunk = await self._object_store.get_range(
            object_key,
            offset=offset,
            length=limit_bytes,
        )
        if chunk and chunk[0] & 0xC0 == 0x80:
            raise TextFileReadError("file_offset_invalid", file)
        requested_nonempty = bool(chunk)
        while chunk:
            try:
                content = chunk.decode("utf-8")
                break
            except UnicodeDecodeError as error:
                if error.reason == "unexpected end of data" and error.end == len(chunk):
                    chunk = chunk[: error.start]
                    continue
                raise TextFileReadError("file_content_not_text", file) from error
        else:
            content = ""
        if requested_nonempty and not chunk:
            # 窗口小于当前多字节字符时不能返回零进度，否则模型或前端会永久停滞。
            raise TextFileReadError("file_range_too_small", file)
        next_offset = offset + len(chunk)
        return SessionFileTextSlice(
            file=file,
            offset=offset,
            next_offset=next_offset,
            content=content,
            eof=next_offset >= total_bytes,
            truncated=next_offset < total_bytes,
            source_kind=source_kind,
        )

    async def _extract_document_from_path(
        self,
        filename: str,
        path: Path,
    ) -> tuple[SessionFileExtractionStatus, bytes | None, str | None]:
        """调用文档提取器并对返回文本执行空值与 10 MiB UTF-8 字节上限二次防线。"""
        assert self._document_extractor is not None
        try:
            result = await self._document_extractor.extract(filename, path)
        except asyncio.CancelledError:
            raise
        except Exception:
            return SessionFileExtractionStatus.FAILED, None, "document_parse_failed"
        if result.status is SessionFileExtractionStatus.READY and result.text is not None:
            encoded = result.text.encode("utf-8")
            if not encoded or not result.text.strip():
                return (
                    SessionFileExtractionStatus.FAILED,
                    None,
                    "document_no_extractable_text",
                )
            if len(encoded) > MAX_SESSION_EXTRACTED_TEXT_BYTES:
                return (
                    SessionFileExtractionStatus.FAILED,
                    None,
                    "document_extracted_text_too_large",
                )
            return SessionFileExtractionStatus.READY, encoded, None
        return (
            SessionFileExtractionStatus.FAILED,
            None,
            result.error_code or "document_parse_failed",
        )

    async def _lazy_extract_document(self, file: SessionFile) -> SessionFile:
        """惰性读取原件（上限 20 MiB）、提取结构化文本、写入缓存并并发安全地回填元数据。"""
        if file.size_bytes > MAX_SESSION_DOCUMENT_EXTRACT_BYTES:
            return await self._record_lazy_extraction_failure(
                file,
                "document_extract_too_large",
            )
        try:
            raw_content = await self._object_store.get_bytes(
                file.object_key,
                maximum_bytes=MAX_SESSION_DOCUMENT_EXTRACT_BYTES,
            )
        except ValueError as error:
            if str(error) == "object_too_large":
                return await self._record_lazy_extraction_failure(
                    file,
                    "document_extract_too_large",
                )
            raise
        except Exception as error:
            if getattr(error, "code", None) == "file_storage_object_too_large":
                return await self._record_lazy_extraction_failure(
                    file,
                    "document_extract_too_large",
                )
            # 存储临时不可用不将文件永久写死为 failed，直接向上抛出 503/瞬态错误。
            raise

        temp_dir = await asyncio.to_thread(
            tempfile.mkdtemp,
            prefix="athena-session-extract-",
        )
        temp_path = Path(temp_dir) / sanitize_filename(file.filename)
        try:
            await asyncio.to_thread(temp_path.write_bytes, raw_content)
            (
                extraction_status,
                extracted_bytes,
                extraction_error_code,
            ) = await self._extract_document_from_path(file.filename, temp_path)
        finally:
            try:
                await asyncio.shield(asyncio.to_thread(shutil.rmtree, temp_dir, True))
            except asyncio.CancelledError:
                shutil.rmtree(temp_dir, ignore_errors=True)
                raise

        if extraction_status is not SessionFileExtractionStatus.READY or extracted_bytes is None:
            return await self._record_lazy_extraction_failure(
                file,
                extraction_error_code or "document_parse_failed",
            )

        # 为每次惰性提取尝试生成唯一不可变对象键，确保并发请求在写缓存失败、取消
        # 或竞态落败时仅清理自身尝试键，绝不误删并发胜者已提交落库的提取缓存。
        attempt_id = self._id_factory("extract")
        candidate_key = self._extracted_text_key(file.sha256, attempt_id)
        try:
            await self._object_store.put_bytes(
                candidate_key,
                extracted_bytes,
                content_type="text/plain; charset=utf-8",
            )
        except BaseException:
            await self._delete_orphan(candidate_key)
            raise

        try:
            updated = await self._repository.update_extraction(
                file.id,
                extraction_status=SessionFileExtractionStatus.READY,
                extracted_text_key=candidate_key,
                extracted_text_bytes=len(extracted_bytes),
                extraction_error_code=None,
            )
        except asyncio.CancelledError:
            # 取消可能发生在数据库提交之后，保留对象交由离线维护判断。
            raise
        except Exception:
            # 写库异常时仅在明确确认数据库未引用该缓存键的情况下清理孤儿对象。
            latest = await self._safe_get_visible(file.session_id, file.owner_id, file.id)
            if latest is not None and latest.extracted_text_key != candidate_key:
                await self._delete_orphan(candidate_key)
            raise

        if updated is None:
            await self._delete_orphan(candidate_key)
            raise FileInputError("session_file_not_found")
        if updated.extracted_text_key != candidate_key:
            # 并发调用已先一步提交另一缓存键：清理本次多余对象，改用已提交键继续读取。
            await self._delete_orphan(candidate_key)
        if updated.extraction_status is SessionFileExtractionStatus.FAILED:
            raise TextFileReadError(
                updated.extraction_error_code or "document_parse_failed",
                updated,
            )
        return updated

    async def _record_lazy_extraction_failure(
        self,
        file: SessionFile,
        error_code: str,
    ) -> SessionFile:
        """回填惰性提取失败态；若并发已有胜者提交 ready，则返回胜者记录继续读取。"""
        updated = await self._repository.update_extraction(
            file.id,
            extraction_status=SessionFileExtractionStatus.FAILED,
            extracted_text_key=None,
            extracted_text_bytes=None,
            extraction_error_code=error_code,
        )
        if updated is None:
            raise FileInputError("session_file_not_found")
        if (
            updated.extraction_status is SessionFileExtractionStatus.READY
            and updated.extracted_text_key is not None
        ):
            return updated
        raise TextFileReadError(updated.extraction_error_code or error_code, updated)

    async def _safe_get_visible(
        self,
        session_id: str,
        owner_id: str,
        file_id: str,
    ) -> SessionFile | None:
        """异常后只读复核文件状态，复核本身失败时返回 None 避免误删可能已提交对象。"""
        try:
            return await self._repository.get_visible(session_id, owner_id, file_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None

    @staticmethod
    def _upload_key(sha256: str, file_id: str) -> str:
        return f"session-files/{sha256[:2]}/{file_id}"

    @staticmethod
    def _extracted_text_key(sha256: str, object_id: str) -> str:
        """构造提取文本缓存对象键（上传使用 file_id，惰性补解析使用独立 attempt_id）。"""
        return f"session-files-extracted/{sha256[:2]}/{object_id}.txt"

    async def _delete_orphan(self, key: str) -> None:
        """清理未发布的并发重试/事务失败对象，不覆盖主业务错误。"""
        try:
            await self._object_store.delete(key)
        except Exception:
            return

    async def _abandon_upload_object(self, key: str) -> None:
        """删除已经确认没有数据库引用的对象。"""
        await self._delete_orphan(key)

    async def _abandon_upload_objects(
        self,
        object_key: str,
        extracted_text_key: str | None,
    ) -> None:
        """同步清理已确认无数据库引用的原件与提取缓存孤儿对象。"""
        await self._abandon_upload_object(object_key)
        if extracted_text_key is not None:
            await self._abandon_upload_object(extracted_text_key)

    async def _abandon_unused_upload_objects(
        self,
        committed: SessionFile,
        *,
        object_key: str,
        extracted_text_key: str | None,
    ) -> None:
        """在幂等复放或并发落败时，仅删除未被已提交记录引用的原件与缓存对象。"""
        if committed.object_key != object_key:
            await self._abandon_upload_object(object_key)
        if extracted_text_key is not None and committed.extracted_text_key != extracted_text_key:
            await self._abandon_upload_object(extracted_text_key)

    async def _replay_upload_after_failure(
        self,
        session_id: str,
        owner_id: str,
        idempotency_key: str,
    ) -> tuple[bool, SessionFile | None]:
        """复核异常事务是否已提交；查询本身失败时返回未知状态。"""
        try:
            return (
                True,
                await self._repository.find_upload_request(
                    session_id,
                    owner_id,
                    idempotency_key,
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False, None


def _validate_declared_mime(value: str | None, aliases: set[str]) -> None:
    normalized = "" if value is None else value.split(";", 1)[0].strip().lower()
    if normalized not in _GENERIC_MIME and normalized not in aliases:
        raise FileInputError("file_mime_mismatch")


def _strict_utf8(
    path: Path,
    *,
    cancellation: threading.Event | None = None,
) -> str:
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        with path.open("rb") as stream:
            while True:
                _raise_if_cancelled(cancellation)
                chunk = stream.read(128 * 1024)
                if not chunk:
                    break
                _raise_if_cancelled(cancellation)
                decoder.decode(chunk)
            _raise_if_cancelled(cancellation)
            decoder.decode(b"", final=True)
    except UnicodeDecodeError as error:
        raise FileInputError("file_content_not_text") from error
    return ""


def _reject_json_constant(_value: str) -> NoReturn:
    """拒绝 Python json 扩展默认接受的非 RFC 8259 常量（NaN / Infinity）。"""
    raise ValueError("file_content_mismatch")


def _validate_json_document(
    path: Path,
    *,
    cancellation: threading.Event | None = None,
) -> None:
    """前置已受 50 MiB 文件大小上限保护，直接严格 UTF-8 解码并用标准 json.loads 校验。"""
    _raise_if_cancelled(cancellation)
    try:
        with path.open("rb") as stream:
            raw = stream.read()
        _raise_if_cancelled(cancellation)
        text = raw.decode("utf-8", errors="strict")
        json.loads(text, parse_constant=_reject_json_constant)
    except _FileInspectionCancelled:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise FileInputError("file_content_mismatch") from error


def _inspect_file(
    path: Path,
    filename: str,
    declared_mime: str | None,
    stop_event: threading.Event,
) -> _FileInspection:
    """在阻塞工作线程内完成大小、格式和摘要检查。"""
    size = path.stat().st_size
    mime_type = validate_file_type(
        path,
        filename,
        declared_mime,
        cancellation=stop_event,
    )
    return _FileInspection(
        size_bytes=size,
        mime_type=mime_type,
        sha256=sha256_file(path, cancellation=stop_event),
    )


async def _inspect_file_cancellable(
    path: Path,
    filename: str,
    declared_mime: str | None,
) -> _FileInspection:
    """在线程检查被取消时设置停止事件，并复用抗二次取消护盾等待线程退出。"""
    stop_event = threading.Event()
    return await run_thread_shielded(
        _inspect_file,
        path,
        filename,
        declared_mime,
        stop_event,
        name="athena-session-file-inspection",
        on_cancel=stop_event.set,
    )


def _validate_idempotency_key(value: str) -> str:
    """上传键只接受可见 ASCII，并参与会话内唯一约束。"""
    if not isinstance(value, str) or _IDEMPOTENCY_KEY.fullmatch(value) is None:
        raise FileInputError("idempotency_key_invalid")
    return value


def _raise_if_cancelled(cancellation: threading.Event | None) -> None:
    """在阻塞文件检查的每个块边界统一退出。"""
    if cancellation is not None and cancellation.is_set():
        raise _FileInspectionCancelled("file_inspection_cancelled")


def _upload_fingerprint(
    *,
    session_id: str,
    owner_id: str,
    filename: str,
    mime_type: str,
    size_bytes: int,
    sha256: str,
) -> str:
    """摘要覆盖调用方可见上传身份，防止同键复用到不同内容。"""
    payload = json.dumps(
        {
            "operation": "session_file_upload",
            "session_id": session_id,
            "owner_id": owner_id,
            "filename": filename,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "sha256": sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "FileInputError",
    "SessionFileService",
    "sanitize_filename",
    "sha256_file",
    "validate_file_type",
]
