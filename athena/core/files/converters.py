"""文件领域模型与公开 API 数据之间的转换。"""

from __future__ import annotations

from typing import Any

from athena.models.file import Attachment


def _attachment_to_public(
    attachment: Attachment, include_metadata: bool = False
) -> dict[str, Any]:
    """将附件领域模型转换为公开 API 字典。"""
    data = {
        "id": attachment.id,
        "session_id": attachment.session_id,
        "knowledge_base_id": attachment.knowledge_base_id,
        "message_id": attachment.message_id,
        "filename": attachment.filename,
        "mime_type": attachment.mime_type,
        "size_bytes": attachment.size_bytes,
        "sha256": attachment.sha256,
        "status": attachment.status.value,
        "adapter_name": attachment.adapter_name,
        "adapter_version": attachment.adapter_version,
        "capabilities": attachment.capabilities,
        "error_message": attachment.error_message,
        "created_at": attachment.created_at.isoformat(),
        "updated_at": attachment.updated_at.isoformat(),
    }
    if include_metadata:
        data["metadata"] = attachment.metadata.model_dump(
            mode="json", exclude_none=True
        )
    return data
