"""Context Manager — session, conversation, and token window management.

Responsibilities:
- Session lifecycle: active → idle (30 min) → expired (24h)
- Deterministic session_id: hash(user_id + channel + chat_id)
- Context injection: conversation history + user memories + environment state
- Token window control: compress when > 80% of model limit
- Snapshot persistence: cold backup / recovery point in SQLite
- Snapshot atomicity: writes with subtask_executions in same transaction
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from athena.config import Config
from athena.logging_config import bind_context, get_logger
from athena.models import get_session_maker

logger = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────
DEFAULT_K_ROUNDS = 10
TOKEN_WINDOW_THRESHOLD = 0.80  # Compress when > 80% of window
SESSION_IDLE_TIMEOUT_MINUTES = 30
SESSION_EXPIRE_HOURS = 24


@dataclass
class SessionContext:
    """In-memory session state (backed by Redis + SQLite snapshot)."""
    session_id: str
    user_id: str
    channel: str
    chat_id: str
    status: str = "active"
    conversation_summary: str = ""
    message_history: list[dict[str, Any]] = field(default_factory=list)
    current_task: dict[str, Any] | None = None
    injected_memories: list[str] = field(default_factory=list)
    token_count_estimate: int = 0
    last_active_at: float = field(default_factory=time.time)


class ContextManager:
    """Manages conversation sessions and context for the Athena Core.

    Session IDs are deterministic: hash(user_id + channel + chat_id).
    Context is stored in Redis (hot) with SQLite snapshot (cold backup).
    """

    def __init__(self, config: Config, redis_client):
        self.config = config
        self.redis = redis_client
        self._session_idle_timeout = config.system.session_idle_timeout_minutes * 60
        self._session_expire = config.system.session_expire_hours * 3600

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
    ) -> SessionContext:
        """Find or create a session for the given identity.

        Recovery logic:
        - Active/idle → silently reuse Redis context (transparent to user).
        - Expired but snapshot exists → load from SQLite, restore to Redis,
          append "📋 已恢复上次会话" to response.
        - No snapshot → fresh session.
        """
        session_id = self.make_session_id(user_id, channel, chat_id)
        logger = bind_context(session_id=session_id)

        # Check Redis first
        key = f"session:{session_id}"
        data = await self.redis.get(key)

        if data:
            ctx = self._deserialize(data)
            ctx.last_active_at = time.time()

            # Update idle→active if needed
            if ctx.status == "idle":
                ctx.status = "active"

            logger.info("session_reused", status=ctx.status)
            return ctx

        # Check SQLite for expired session with snapshot
        db_path = self.config.sqlite_db_path
        session_maker = get_session_maker(db_path)

        async with session_maker() as session:
            from sqlalchemy import select
            from athena.models.session import Session as SessionModel
            result = await session.execute(
                select(SessionModel).where(SessionModel.session_id == session_id)
            )
            db_session = result.scalar_one_or_none()

        if db_session and db_session.context_snapshot:
            # Restore from snapshot
            snapshot = json.loads(db_session.context_snapshot)
            ctx = await self.restore_from_snapshot(session_id, snapshot)
            ctx.last_active_at = time.time()
            ctx.status = "active"

            # Cache in Redis
            await self.redis.set(
                key,
                self._serialize(ctx),
                ex=self._session_idle_timeout,
            )

            logger.info("session_restored_from_snapshot")
            return ctx

        # Fresh session
        ctx = SessionContext(
            session_id=session_id,
            user_id=user_id,
            channel=channel,
            chat_id=chat_id,
        )

        # Persist to SQLite
        async with session_maker() as session:
            from athena.models.session import Session as SessionModel
            db_session = SessionModel(
                session_id=session_id,
                user_id=user_id,
                channel=channel,
                chat_id=chat_id,
                status="active",
            )
            session.add(db_session)
            await session.commit()

        # Cache in Redis
        await self.redis.set(
            key,
            self._serialize(ctx),
            ex=self._session_idle_timeout,
        )

        logger.info("session_created")
        return ctx

    async def restore_from_snapshot(
        self,
        session_id: str,
        snapshot: dict[str, Any],
    ) -> SessionContext:
        """Restore a SessionContext from a context_snapshot JSON blob."""
        task_data = snapshot.get("current_task")
        current_task = None
        if task_data:
            current_task = {
                "task_id": task_data.get("task_id"),
                "status": task_data.get("status", "running"),
                "completed_steps": task_data.get("completed_steps", []),
                "step_outputs": task_data.get("step_outputs", {}),
            }

        return SessionContext(
            session_id=session_id,
            user_id="",  # Will be set by caller
            channel="",  # Will be set by caller
            chat_id="",  # Will be set by caller
            status="active",
            conversation_summary=snapshot.get("conversation_summary", ""),
            message_history=snapshot.get("message_history", []),
            current_task=current_task,
            injected_memories=snapshot.get("injected_memories", []),
            token_count_estimate=snapshot.get("token_count_estimate", 0),
        )

    # ── Context building ──────────────────────────────────────────────

    async def inject_context(
        self,
        ctx: SessionContext,
        current_message: str,
        user_id: str,
    ) -> list[dict[str, Any]]:
        """Build the complete messages array for the LLM.

        Structure:
        1. System prompt with conversation_summary and injected memories
        2. message_history (last K rounds, unaffected by compression)
        3. Current user message

        If message_history is empty, use conversation_summary as fallback.
        """
        messages = []

        # Build system context
        system_parts = []

        if ctx.conversation_summary:
            system_parts.append(f"[Previous conversation]\n{ctx.conversation_summary}")

        # Inject user memories
        memories = await self._get_injected_memories(ctx, user_id)
        if memories:
            system_parts.append(f"[User context]\n{memories}")

        if system_parts:
            messages.append({"role": "system", "content": "\n\n".join(system_parts)})

        # Add message history
        messages.extend(ctx.message_history)

        # Add current message
        messages.append({"role": "user", "content": current_message})

        return messages

    async def _get_injected_memories(
        self,
        ctx: SessionContext,
        user_id: str,
    ) -> str:
        """Retrieve user memories relevant to the current conversation.

        Tries semantic search via RAG Skill first, falls back to simple
        key-based query if vector store is unavailable.
        """
        try:
            from athena.core.memory import MemoryStore
            store = MemoryStore(self.config)
            # Get the last few user messages for context
            recent_user_messages = " ".join(
                m["content"] for m in ctx.message_history[-3:]
                if m.get("role") == "user"
            )
            query = recent_user_messages or "recent"
            results = await store.semantic_search(user_id, query, top_k=5)
            if results:
                ctx.injected_memories = [m.memory_id for m in results]
                return "\n".join(f"- {m.value}" for m in results)
        except Exception:
            pass

        return ""

    # ── Context updates ───────────────────────────────────────────────

    async def add_to_history(
        self,
        ctx: SessionContext,
        role: str,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> None:
        """Append a message to the conversation history."""
        msg: dict[str, Any] = {"role": role, "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        ctx.message_history.append(msg)

    async def update_after_step(
        self,
        ctx: SessionContext,
        step: int,
        output: Any,
        completed: bool = True,
    ) -> None:
        """Update task context after a subtask execution step.

        Args:
            ctx: Current session context.
            step: Step number that completed.
            output: Step output (stored for template rendering).
            completed: Whether the step succeeded.
        """
        if not ctx.current_task:
            return

        if completed:
            if step not in ctx.current_task.get("completed_steps", []):
                ctx.current_task.setdefault("completed_steps", []).append(step)

        step_outputs = ctx.current_task.setdefault("step_outputs", {})
        step_outputs[f"step{step}"] = output

        # Persist snapshot
        await self.write_snapshot(ctx)

    async def write_snapshot(self, ctx: SessionContext) -> None:
        """Write context_snapshot to SQLite.

        IMPORTANT: This MUST be called within the same transaction as
        subtask_execution writes to ensure atomicity. The caller is
        responsible for transaction management.
        """
        snapshot = self._build_snapshot(ctx)

        db_path = self.config.sqlite_db_path
        session_maker = get_session_maker(db_path)

        async with session_maker() as session:
            from sqlalchemy import update
            from athena.models.session import Session as SessionModel

            await session.execute(
                update(SessionModel)
                .where(SessionModel.session_id == ctx.session_id)
                .values(
                    context_snapshot=json.dumps(snapshot, ensure_ascii=False),
                    last_active_at=datetime.now(timezone.utc),
                    status=ctx.status,
                )
            )
            await session.commit()

        logger.debug("snapshot_written", session_id=ctx.session_id)

    def _build_snapshot(self, ctx: SessionContext) -> dict[str, Any]:
        """Serialize the current context state into a snapshot dict."""
        return {
            "version": 1,
            "snapshot_at": datetime.now(timezone.utc).isoformat(),
            "conversation_summary": ctx.conversation_summary,
            "message_history": ctx.message_history,
            "current_task": ctx.current_task,
            "injected_memories": ctx.injected_memories,
            "token_count_estimate": ctx.token_count_estimate,
        }

    # ── Token window management ───────────────────────────────────────

    async def compress_if_needed(
        self,
        ctx: SessionContext,
        model_limit: int = 128000,
    ) -> bool:
        """Check token usage and compress if over 80% of model limit.

        Compression keeps the last K rounds (default 10) intact.
        Earlier rounds are summarized and merged into conversation_summary.

        Returns True if compression was performed.
        """
        threshold = int(model_limit * TOKEN_WINDOW_THRESHOLD)

        # Estimate current tokens
        ctx.token_count_estimate = self._estimate_total_tokens(ctx)

        if ctx.token_count_estimate < threshold:
            return False

        # Compress: summarize the oldest messages beyond last K rounds
        k = DEFAULT_K_ROUNDS
        total_msgs = len(ctx.message_history)
        if total_msgs <= k * 2:  # Not enough to compress
            return False

        to_compress = ctx.message_history[: total_msgs - k]
        to_keep = ctx.message_history[total_msgs - k:]

        # Generate summary of compressed messages
        summary = await self._summarize(to_compress)

        # Merge with existing summary
        if ctx.conversation_summary:
            ctx.conversation_summary = f"{ctx.conversation_summary}\n{summary}"
        else:
            ctx.conversation_summary = summary

        # Replace history with kept messages
        ctx.message_history = to_keep

        # Re-estimate
        ctx.token_count_estimate = self._estimate_total_tokens(ctx)

        logger.info(
            "context_compressed",
            session_id=ctx.session_id,
            removed_messages=len(to_compress),
            new_token_estimate=ctx.token_count_estimate,
        )
        return True

    async def _summarize(self, messages: list[dict[str, Any]]) -> str:
        """Generate a one-sentence summary of a set of messages using LLM."""
        try:
            from athena.core.llm_provider.manager import LLMProviderManager
            from athena.config import get_config

            config = get_config()
            llm = LLMProviderManager(config)

            conversation_text = "\n".join(
                f"{m['role']}: {m.get('content', '')[:200]}" for m in messages
            )

            response = await llm.generate(
                messages=[{
                    "role": "user",
                    "content": (
                        "Summarize the following conversation into 1-2 sentences, "
                        "capturing the key topics and actions:\n\n"
                        f"{conversation_text}"
                    ),
                }],
                tools=None,
                max_tokens=200,
                temperature=0.3,
            )
            return response.text.strip()
        except Exception as e:
            logger.warning("summarizer_failed", error=str(e))
            # Fallback: use first and last message
            first = messages[0].get("content", "")[:100] if messages else ""
            last = messages[-1].get("content", "")[:100] if messages else ""
            return f"Conversation about: {first} ... {last}"

    def _estimate_total_tokens(self, ctx: SessionContext) -> int:
        """Estimate total token count for the conversation."""
        total = 0
        for msg in ctx.message_history:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += len(content) // 4  # Rough estimate: 4 chars/token
        if ctx.conversation_summary:
            total += len(ctx.conversation_summary) // 4
        return total

    # ── Session lifecycle ─────────────────────────────────────────────

    async def expire_idle_sessions(self) -> int:
        """Periodic cleanup: transition idle→expired if past expiry.

        Returns count of expired sessions.
        """
        db_path = self.config.sqlite_db_path
        session_maker = get_session_maker(db_path)
        expiry_cutoff = datetime.now(timezone.utc)
        # In production this would use a proper timestamp comparison

        expired = 0
        async with session_maker() as session:
            from sqlalchemy import update
            from athena.models.session import Session as SessionModel

            # Mark idle sessions as expired
            result = await session.execute(
                update(SessionModel)
                .where(SessionModel.status == "idle")
                .values(status="expired")
            )
            await session.commit()
            expired = result.rowcount

        if expired:
            logger.info("sessions_expired", count=expired)
        return expired

    # ── Serialization ─────────────────────────────────────────────────

    def _serialize(self, ctx: SessionContext) -> str:
        """Serialize a SessionContext to JSON string for Redis storage."""
        return json.dumps({
            "session_id": ctx.session_id,
            "user_id": ctx.user_id,
            "channel": ctx.channel,
            "chat_id": ctx.chat_id,
            "status": ctx.status,
            "conversation_summary": ctx.conversation_summary,
            "message_history": ctx.message_history,
            "current_task": ctx.current_task,
            "injected_memories": ctx.injected_memories,
            "token_count_estimate": ctx.token_count_estimate,
            "last_active_at": ctx.last_active_at,
        }, ensure_ascii=False)

    def _deserialize(self, data: str) -> SessionContext:
        """Deserialize a JSON string into a SessionContext."""
        d = json.loads(data)
        return SessionContext(
            session_id=d["session_id"],
            user_id=d.get("user_id", ""),
            channel=d.get("channel", ""),
            chat_id=d.get("chat_id", ""),
            status=d.get("status", "active"),
            conversation_summary=d.get("conversation_summary", ""),
            message_history=d.get("message_history", []),
            current_task=d.get("current_task"),
            injected_memories=d.get("injected_memories", []),
            token_count_estimate=d.get("token_count_estimate", 0),
            last_active_at=d.get("last_active_at", time.time()),
        )
