"""Context Manager — session lifecycle management.

Responsibilities:
- Deterministic session_id: hash(user_id + channel + chat_id)
- Session lookup: Redis (hot) → SQLite (cold) → create new
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from athena.config import get_config
from athena.logging_config import bind_context, get_logger
from athena.models.base import get_redis, get_session_maker

if TYPE_CHECKING:
    from athena.models.session import Session

logger = get_logger(__name__)


@dataclass
class SessionData:
    """Serializable session data for Redis caching."""
    session_id: str = ""
    user_id: str = ""
    channel: str = ""
    chat_id: str = ""
    summary: str | None = None
    summary_offset: int | None = None

    @classmethod
    def from_dict(cls, data: dict) -> SessionData:
        return cls(
            session_id=data.get("session_id", ""),
            user_id=data.get("user_id", ""),
            channel=data.get("channel", ""),
            chat_id=data.get("chat_id", ""),
            summary=data.get("summary"),
            summary_offset=data.get("summary_offset"),
        )

    @classmethod
    def from_session(cls, session: Session) -> SessionData:
        """Create from a Session ORM model."""
        return cls(
            session_id=session.session_id,
            user_id=session.user_id,
            channel=session.channel,
            chat_id=session.chat_id,
            summary=session.summary,
            summary_offset=session.summary_offset,
        )

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "channel": self.channel,
            "chat_id": self.chat_id,
            "summary": self.summary,
            "summary_offset": self.summary_offset,
        }


class ContextManager:
    """Manages conversation sessions for the Athena Core.

    Session IDs are deterministic: hash(user_id + channel + chat_id).
    Session records are cached in Redis (hot) with SQLite as cold storage.
    """

    def __init__(self) -> None:
        config = get_config()
        self._session_idle_timeout = config.system.session_idle_timeout_minutes * 60

    # ── Session management ────────────────────────────────────────────

    @staticmethod
    def make_session_id(user_id: str, channel: str, chat_id: str) -> str:
        """Generate a deterministic session_id from user identity."""
        raw = f"{user_id}:{channel}:{chat_id}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    async def get_or_create_session(
        self,
        user_id: str,
        channel: str,
        chat_id: str,
    ) -> Session:
        """Find or create a session for the given identity.

        Lookup order: Redis → SQLite → create new.
        Returns a ``Session`` model instance (detached from SQLAlchemy session).
        """
        from athena.models.session import Session as SessionModel

        session_id = self.make_session_id(user_id, channel, chat_id)
        log = bind_context(session_id=session_id)

        # 1. Check Redis
        redis = await get_redis()
        key = f"session:{session_id}"
        data = await redis.get(key)
        if data:
            d = json.loads(data)
            return SessionModel(**d)

        # 2. Check SQLite
        async with get_session_maker()() as db_session:
            from sqlalchemy import select
            result = await db_session.execute(
                select(SessionModel).where(SessionModel.session_id == session_id)
            )
            db_row = result.scalar_one_or_none()
            if db_row is not None:
                # Cache in Redis
                session_data = SessionData.from_session(db_row).to_dict()
                await redis.set(
                    key,
                    json.dumps(session_data, ensure_ascii=False, default=str),
                    ex=self._session_idle_timeout,
                )
                log.info("session_loaded_from_sqlite")
                return db_row

            # 3. Create new
            new_session = SessionModel(
                session_id=session_id,
                user_id=user_id,
                channel=channel,
                chat_id=chat_id,
            )

            db_session.add(new_session)
            await db_session.commit()

            # Cache in Redis
            session_data = SessionData.from_session(new_session).to_dict()
            await redis.set(
                key,
                json.dumps(session_data, ensure_ascii=False, default=str),
                ex=self._session_idle_timeout,
            )

            log.info("session_created")
            return new_session
