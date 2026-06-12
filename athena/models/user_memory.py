"""UserMemory model — user_memories table.

Stores memory metadata and original text. Vector embeddings are managed
independently by the RAG Skill (Chroma/Qdrant). The vector_id column
links the two sides.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from athena.models.base import Base


class UserMemory(Base):
    __tablename__ = "user_memories"

    memory_id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    key: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    vector_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
    )  # Global unique ref in vector store
    sync_status: Mapped[str] = mapped_column(
        String, default="pending", index=True
    )  # 'pending', 'synced', 'failed', 'deleted'
    meta_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Extra metadata (synced_at, tags, source, etc.)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
