"""会话文件 ORM 到领域对象的纯转换。"""

from __future__ import annotations

from adapter.file.model import SessionFileModel
from common.date import as_utc
from domain.file import SessionFile, SessionFileExtractionStatus, SessionFileSource


def session_file_to_domain(row: SessionFileModel) -> SessionFile:
    """把文件行转换为领域对象并补齐测试数据库丢失的 UTC 时区。"""
    created_at = as_utc(row.created_at)
    return SessionFile(
        id=row.id,
        session_id=row.session_id,
        owner_id=row.owner_id,
        source=SessionFileSource(row.source),
        filename=row.filename,
        label=row.label,
        mime_type=row.mime_type,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        object_key=row.object_key,
        source_call_id=row.source_call_id,
        output_index=row.output_index,
        created_at=created_at,
        extraction_status=SessionFileExtractionStatus(row.extraction_status),
        extracted_text_key=row.extracted_text_key,
        extracted_text_bytes=row.extracted_text_bytes,
        extraction_error_code=row.extraction_error_code,
    )


__all__ = ["session_file_to_domain"]
