"""记忆事实的新增持久化表。"""

from __future__ import annotations

from sqlalchemy import Computed, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .project_models import ProjectModel
from .runtime_models import MemoryModel


class MemoryExtractionSnapshotModel(Base):
    """一轮提取后冻结的候选清单；空清单也会持久化。"""

    __tablename__ = "memory_extraction_snapshots"

    turn_id: Mapped[str] = mapped_column(String, primary_key=True)
    snapshot_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String)


class MemoryRunEvidenceSnapshotModel(Base):
    """一轮记忆作业首次领取时冻结的 Run/Worker 来源集合。"""

    __tablename__ = "memory_run_evidence_snapshots"

    turn_id: Mapped[str] = mapped_column(String, primary_key=True)
    source_fingerprint: Mapped[str] = mapped_column(String)
    snapshot_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String)


class MemoryEpisodeModel(Base):
    """来源轮次唯一的任务回顾及其结构化验证边界。"""

    __tablename__ = "memory_episodes"
    __table_args__ = (
        UniqueConstraint("turn_id", name="uq_memory_episodes_turn"),
        Index("idx_memory_episodes_session_created", "session_id", "created_at"),
        Index(
            "idx_memory_episodes_content_fts",
            "content_fts",
            postgresql_using="gin",
        ),
    )

    episode_id: Mapped[str] = mapped_column(String, primary_key=True)
    turn_id: Mapped[str] = mapped_column(String, nullable=False)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_fts: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('chinese'::regconfig, content)", persisted=True),
    )
    summary_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_refs_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class MemorySourceSuppressionModel(Base):
    """用户遗忘的来源及条目指纹；不保存已删除的正文。"""

    __tablename__ = "memory_source_suppressions"

    source_type: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[str] = mapped_column(String, primary_key=True)
    item_fingerprint: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class MemoryRelationModel(Base):
    """两条事实版本之间的替代或矛盾关系。"""

    __tablename__ = "memory_relations"
    __table_args__ = (
        UniqueConstraint("source_memory_id", "target_memory_id", "kind"),
        Index("idx_memory_relations_target", "target_memory_id", "kind"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_memory_id: Mapped[str] = mapped_column(
        ForeignKey("memories.id", ondelete="CASCADE")
    )
    target_memory_id: Mapped[str] = mapped_column(
        ForeignKey("memories.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String)
    operation_id: Mapped[str] = mapped_column(String, unique=True)
    created_at: Mapped[str] = mapped_column(String)


__all__ = [
    "MemoryExtractionSnapshotModel",
    "MemoryEpisodeModel",
    "MemoryModel",
    "MemoryRelationModel",
    "MemoryRunEvidenceSnapshotModel",
    "MemorySourceSuppressionModel",
]
