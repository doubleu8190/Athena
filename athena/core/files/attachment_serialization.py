"""附件领域模型到稳定传输内容的序列化。"""

from __future__ import annotations

from typing import Any

from athena.models.file import Attachment


def attachment_to_payload(
    attachment: Attachment, include_metadata: bool = False
) -> dict[str, Any]:
    """将附件转换为路由和工具可复用的传输内容。

    参数：
        attachment: 需要序列化的附件领域模型。
        include_metadata: 是否包含结构化附件元数据。

    返回：
        包含稳定附件字段的字典。

    异常：
        不主动抛出业务异常。
    """
    payload = {
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
        payload["metadata"] = attachment.metadata.model_dump(
            mode="json", exclude_none=True
        )
    return payload
