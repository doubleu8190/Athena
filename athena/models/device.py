"""Device model — device_registry table.

Tracks registered device agents (host machines, Android devices).
Connection info stored as JSON (PSK public key, serial, etc.).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from athena.models.base import Base


class Device(Base):
    __tablename__ = "device_registry"

    device_id: Mapped[str] = mapped_column(String, primary_key=True)
    type: Mapped[str] = mapped_column(
        String, nullable=False
    )  # 'host' or 'android'
    connection_info: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    status: Mapped[str] = mapped_column(
        String, default="offline"
    )  # 'online', 'offline'
    last_heartbeat: Mapped[datetime | None] = mapped_column(nullable=True)
