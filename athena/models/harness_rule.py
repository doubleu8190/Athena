"""HarnessRule model — harness_rules table.

Stores security rule definitions for the Harness Engine.
Rules are hot-reloaded: in-memory cache polls MAX(revision) every 30s.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from athena.models.base import Base


class RuleType(StrEnum):
    """Valid rule types for the Harness Engine."""

    BLACKLIST = "blacklist"
    PATH_PERMISSION = "path_permission"
    QUOTA = "quota"
    COOLING_OFF = "cooling_off"


class HarnessRule(Base):
    __tablename__ = "harness_rules"

    rule_id: Mapped[str] = mapped_column(String, primary_key=True)
    rule_type: Mapped[str] = mapped_column(
        String, nullable=False, index=True
    )  # 'blacklist', 'path_boundary', 'path_permission', 'quota', 'cooling_off'
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_json: Mapped[str] = mapped_column(Text, nullable=False)  # JSON
    priority: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
    revision: Mapped[int] = mapped_column(Integer, default=1)  # Monotonic counter
