"""Small in-process adapters used by the bootstrap smoke application.

These adapters implement the same domain ports as the PostgreSQL adapters.  They
make the default ASGI app deterministic for local development and tests; a
production deployment can replace the providers with the PostgreSQL graph
without changing controllers or application services.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from domain.approval import ApprovalDecision, ApprovalRepository, ApprovalRequest
from domain.configuration import ProviderQueryPort, SettingsQueryPort
from domain.memory.query_ports import MemoryQueryPort
from domain.retrieval.query_ports import RetrievalPort
from domain.tools.query_ports import ToolUsageQueryPort
from domain.files import (
    AdapterInfo,
    Attachment,
    AttachmentRepository,
    AttachmentStatus,
    DocumentJobPort,
    FileAdapterRegistryPort,
    FileStoragePort,
    StoredBlob,
)
from domain.knowledge import KnowledgeBase, KnowledgeBaseRepository, KnowledgeDocumentRepository
from domain.events import (
    ApplicationEvent,
    EventPort,
)
import asyncio
import hashlib
import json
from domain.memory import (
    MemoryEpisode,
    MemoryEpisodeRepositoryPort,
    MemoryEpisodeSearchRequest,
    MemoryFact,
    MemoryFactIdentity,
    MemoryListRequest,
    MemoryPage,
    MemoryRepositoryPort,
    MemoryRecord,
    MemoryRelationRecord,
    MemorySourceReference,
    MemorySearchRequest,
    MemorySuppressedError,
    MemoryStatus,
    MemoryWriteCommand,
)
from domain.runs import (
    CommandEnqueueResult,
    CommandStatus,
    CommandStatusRecord,
    RunCommand,
    RunCommandPayload,
    RunStartPayload,
    CommandQueuePort,
    CommandStorePort,
    RunLifecyclePort,
    RunQueryPort,
    RunStatus,
    RunSummary,
)
from domain.sessions import Message, MessageRepository, Session, SessionRepository
from domain.tools import (
    JsonSchema,
    MCPServerConfig,
    MCPPort,
    RiskLevel,
    ToolConfig,
    ToolConfigRepository,
    ToolExecutionPort,
    ToolExecutionRequest,
    ToolExecutionResult,
    ToolExecutionMode,
)


class InMemorySessionRepository(SessionRepository):
    def __init__(self) -> None:
        self.items: dict[str, Session] = {}
        self.creation_requests: dict[str, str] = {}

    async def create(self, session: Session) -> Session:
        self.items[session.id] = session
        return session

    async def create_idempotent(self, session: Session, *, creation_request_id: str) -> Session:
        existing_id = self.creation_requests.get(creation_request_id)
        if existing_id is not None:
            return self.items[existing_id]
        self.items[session.id] = session
        self.creation_requests[creation_request_id] = session.id
        return session

    async def get(self, session_id: str) -> Session | None:
        return self.items.get(session_id)

    async def list_sessions(self) -> list[Session]:
        return sorted(self.items.values(), key=lambda item: item.updated_at, reverse=True)

    async def save(self, session: Session) -> Session:
        self.items[session.id] = session
        return session

    async def rename(self, session: Session) -> Session:
        self.items[session.id] = session
        return session

    async def delete(self, session_id: str) -> bool:
        return self.items.pop(session_id, None) is not None


class InMemoryMessageRepository(MessageRepository):
    def __init__(self) -> None:
        self.items: dict[str, Message] = {}

    async def save(self, message: Message) -> str:
        self.items[message.id] = message
        return message.id

    async def save_user_message_with_title(self, message: Message, *, fallback_title: str) -> bool:
        first = not any(
            item.session_id == message.session_id and item.role.value == "user"
            for item in self.items.values()
        )
        self.items[message.id] = message
        return first

    async def get(self, message_id: str) -> Message | None:
        return self.items.get(message_id)

    async def list_by_session(self, session_id: str, *, limit: int | None = None) -> list[Message]:
        values = [item for item in self.items.values() if item.session_id == session_id]
        values.sort(key=lambda item: item.timestamp)
        return values if limit is None else values[:limit]

    async def list_recent_conversation(
        self, session_id: str, *, limit: int
    ) -> list[Message]:
        values = [
            item
            for item in self.items.values()
            if item.session_id == session_id
            and item.role.value in {"user", "assistant"}
        ]
        values.sort(key=lambda item: (item.timestamp, item.id), reverse=True)
        values = values[:limit]
        values.reverse()
        return values

    async def list_after(self, session_id: str, after_id: str) -> list[Message]:
        values = await self.list_by_session(session_id)
        return [item for item in values if item.id > after_id]


def _command_message_fields(payload: RunCommandPayload) -> tuple[str | None, tuple[str, ...]]:
    if isinstance(payload, RunStartPayload):
        return payload.message_id, payload.attachment_ids
    return None, ()


class InMemoryRunStore(
    CommandQueuePort,
    CommandStorePort,
    RunLifecyclePort,
    RunQueryPort,
):
    writes_cancellation_events_to_outbox = False
    writes_terminal_artifacts_to_outbox = False

    def __init__(self) -> None:
        self.commands: dict[str, RunCommand] = {}
        self.command_status: dict[str, CommandStatusRecord] = {}
        self.runs: dict[str, RunSummary] = {}
        self.root_waits: dict[str, dict[str, object]] = {}

    async def enqueue_command(self, command: RunCommand) -> CommandEnqueueResult:
        message_id, attachment_ids = _command_message_fields(command.payload)
        existing = self.command_status.get(command.command_id)
        if existing is not None:
            return CommandEnqueueResult(command.command_id, command.run_id, message_id, attachment_ids, existing.status, True)
        self.commands[command.command_id] = command
        self.command_status[command.command_id] = CommandStatusRecord(command.command_id, command.session_id, command.run_id, command.command_type, CommandStatus.QUEUED)
        if command.run_id:
            now = datetime.now(timezone.utc)
            self.runs.setdefault(command.run_id, RunSummary(command.run_id, command.session_id, RunStatus.CREATED, f"thread-{command.run_id}", created_at=now, updated_at=now))
        return CommandEnqueueResult(command.command_id, command.run_id, message_id, attachment_ids, CommandStatus.QUEUED, False)

    async def get_command(self, command_id: str) -> CommandStatusRecord | None:
        return self.command_status.get(command_id)
    async def list_for_session(self, session_id: str) -> list[RunSummary]:
        return [value for value in self.runs.values() if value.session_id == session_id]

    async def get(self, run_id: str) -> RunSummary | None:
        return self.runs.get(run_id)

    async def get_active_for_session(self, session_id: str) -> RunSummary | None:
        return next((value for value in self.runs.values() if value.session_id == session_id and value.status in {RunStatus.CREATED, RunStatus.RUNNING, RunStatus.PAUSED}), None)

    async def claim_next_command(self) -> RunCommand | None:
        for command_id, command in self.commands.items():
            status = self.command_status[command_id]
            if status.status == CommandStatus.QUEUED:
                self.command_status[command_id] = replace(status, status=CommandStatus.RUNNING)
                return command
        return None

    async def complete_command(self, command_id: str, *, status: CommandStatus, result: object = None, error: object = None) -> None:
        current = self.command_status.get(command_id)
        if current:
            self.command_status[command_id] = replace(current, status=status, result=result, error=error)

    async def complete_active_command_for_run(
        self,
        run_id: str,
        *,
        status: CommandStatus,
        result: object = None,
        error: object = None,
    ) -> bool:
        active = next(
            (
                command_id
                for command_id, record in self.command_status.items()
                if record.run_id == run_id and record.status == CommandStatus.RUNNING
            ),
            None,
        )
        if active is None:
            return False
        await self.complete_command(active, status=status, result=result, error=error)
        return True

    async def update_status(self, run_id: str, status: RunStatus | str) -> bool:
        current = self.runs.get(run_id)
        if current is None:
            return False
        self.runs[run_id] = replace(current, status=status, updated_at=datetime.now(timezone.utc))
        return True

    async def complete(self, run_id: str, result: dict[str, object]) -> bool:
        return await self.update_status(run_id, RunStatus.COMPLETED)

    async def fail(self, run_id: str, error: dict[str, object]) -> bool:
        return await self.update_status(run_id, RunStatus.FAILED)

    async def arm_root_wait(
        self,
        run_id: str,
        *,
        plan_id: str,
        round_id: str,
        plan_version: int,
        interrupt_id: str,
        checkpoint_id: str,
    ) -> bool:
        run = self.runs.get(run_id)
        if (
            run is None
            or run.status != RunStatus.RUNNING
            or not all((plan_id, round_id, interrupt_id, checkpoint_id))
            or plan_version < 1
        ):
            return False
        self.root_waits[run_id] = {
            "plan_id": plan_id,
            "round_id": round_id,
            "plan_version": plan_version,
            "interrupt_id": interrupt_id,
            "checkpoint_id": checkpoint_id,
        }
        return True

    async def cancel(self, run_id: str) -> bool:
        return await self.update_status(run_id, RunStatus.CANCELLED)


class InMemoryToolRepository(ToolConfigRepository):
    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        self.items: dict[str, ToolConfig] = {
            "noop": ToolConfig("noop", ToolExecutionMode.NATIVE, None, None, "No-op tool", JsonSchema({}), RiskLevel.LOW, False, True, now, now)
        }

    async def upsert(self, config: ToolConfig) -> ToolConfig:
        self.items[config.tool_name] = config
        return config

    async def update(self, config: ToolConfig) -> ToolConfig:
        return await self.upsert(config)

    async def get(self, tool_name: str) -> ToolConfig | None:
        return self.items.get(tool_name)

    async def list_tools(self) -> list[ToolConfig]:
        return list(self.items.values())


class InMemoryToolExecution(ToolExecutionPort):
    async def execute(self, request: ToolExecutionRequest) -> ToolExecutionResult:
        return ToolExecutionResult(status="success", output="", duration_ms=0)


class InMemoryMCPPort(MCPPort):
    def __init__(self) -> None:
        self.items: dict[str, MCPServerConfig] = {}

    async def register(self, name: str, config: MCPServerConfig) -> dict[str, object]:
        self.items[name] = config
        return {"name": name, "status": "connected"}

    async def unregister(self, name: str) -> None:
        self.items.pop(name, None)

    async def list_servers(self) -> list[dict[str, object]]:
        return [{"name": name, "status": "connected", "enabled": config.enabled} for name, config in self.items.items()]

    async def shutdown(self) -> None:
        self.items.clear()


class InMemoryMemoryPort(MemoryRepositoryPort):
    def __init__(self) -> None:
        self.items: dict[str, MemoryRecord] = {}
        self.relations: list[tuple[str, str, str, str]] = []
        self.suppressed_sources: set[tuple[str, str]] = set()

    async def initialize(self) -> None: pass
    async def search(self, request: MemorySearchRequest) -> list[MemoryRecord]:
        query = request.query.lower()
        values = [
            value
            for value in self.items.values()
            if query in value.content.lower()
        ]
        scope = (request.filters or {}).get("memory_scope")
        if isinstance(scope, dict):
            session_id = scope.get("session_id")
            project_id = scope.get("project_id")
            scoped: list[MemoryRecord] = []
            for value in values:
                raw_fact = value.metadata.get("fact")
                fact = (
                    raw_fact
                    if isinstance(raw_fact, MemoryFact)
                    else MemoryFact.model_validate(raw_fact)
                    if raw_fact is not None
                    else None
                )
                if fact is not None:
                    allowed = (
                        fact.scope_kind == "global" and fact.scope_id == "global"
                    ) or (
                        fact.scope_kind == "session" and fact.scope_id == session_id
                    ) or (
                        project_id is not None
                        and fact.scope_kind == "project"
                        and fact.scope_id == project_id
                    )
                else:
                    category = value.metadata.get(
                        "category", value.metadata.get("type")
                    )
                    allowed = (
                        category in {"preference", "profile"}
                        or value.metadata.get("session_id") == session_id
                    )
                if allowed:
                    scoped.append(value)
            values = scoped
        return [replace(value, score=1.0) for value in values][:request.limit]

    async def list_scope_index(
        self,
        *,
        session_id: str,
        project_id: str | None,
        limit: int = 80,
    ) -> list[MemoryRecord]:
        """返回测试数据中当前作用域的目录来源，不增加访问计数。"""
        values: list[MemoryRecord] = []
        for record in self.items.values():
            if record.status is not MemoryStatus.ACTIVE or record.validity_status != "valid":
                continue
            raw_fact = record.metadata.get("fact")
            fact = (
                raw_fact
                if isinstance(raw_fact, MemoryFact)
                else MemoryFact.model_validate(raw_fact)
                if raw_fact is not None
                else None
            )
            if fact is not None:
                allowed = (
                    (fact.scope_kind == "global" and fact.scope_id == "global")
                    or (fact.scope_kind == "session" and fact.scope_id == session_id)
                    or (
                        project_id is not None
                        and fact.scope_kind == "project"
                        and fact.scope_id == project_id
                    )
                )
            else:
                category = record.metadata.get(
                    "category", record.metadata.get("type")
                )
                allowed = (
                    category in {"preference", "profile"}
                    or record.metadata.get("session_id") == session_id
                )
            if allowed:
                values.append(record)
        values.sort(key=lambda record: record.created_at, reverse=True)
        return values[: min(120, max(1, limit))]

    async def list_memories(self, request: MemoryListRequest) -> MemoryPage:
        values = list(self.items.values())[request.offset:request.offset + request.limit]
        return MemoryPage(tuple(values), len(self.items), {"total": len(self.items), "expired": 0, "recent": len(self.items)})
    async def list_fact_group(self, fact: MemoryFact, *, session_id: str) -> list[MemoryRecord]:
        identity = (fact.scope_kind, fact.scope_id, fact.subject_key, fact.fact_key)
        now = datetime.now(timezone.utc)
        matches = []
        for record in self.items.values():
            if record.status is not MemoryStatus.ACTIVE or record.validity_status != "valid":
                continue
            expired = False
            for value in (record.expires_at, record.valid_until):
                if value is None:
                    continue
                try:
                    deadline = datetime.fromisoformat(value)
                except (TypeError, ValueError):
                    expired = True
                    break
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
                if deadline <= now:
                    expired = True
                    break
            if expired:
                continue
            raw_fact = record.metadata.get("fact")
            if raw_fact is None:
                continue
            stored = raw_fact if isinstance(raw_fact, MemoryFact) else MemoryFact.model_validate(raw_fact)
            if stored.valid_from is not None:
                try:
                    valid_from = datetime.fromisoformat(stored.valid_from)
                except (TypeError, ValueError):
                    continue
                if valid_from.tzinfo is None:
                    valid_from = valid_from.replace(tzinfo=timezone.utc)
                if valid_from > now:
                    continue
            if (stored.scope_kind, stored.scope_id, stored.subject_key, stored.fact_key) != identity:
                continue
            if fact.claim_intent == "retract" and stored.fact_value != fact.retracts_value:
                continue
            matches.append(record)
        return sorted(matches, key=lambda item: item.id)
    async def list_fact_group_by_identity(self, identity: MemoryFactIdentity) -> list[MemoryRecord]:
        now = datetime.now(timezone.utc).isoformat()
        seed = MemoryFact(
            scope_kind=identity.scope_kind,
            scope_id=identity.scope_id,
            subject_key=identity.subject_key,
            fact_key=identity.fact_key,
            fact_value="validation-seed",
            confidence=1.0,
            asserted_at=now,
            source_refs=(MemorySourceReference(
                source_type="turn",
                source_id="validation-seed",
                excerpt="validation-seed",
                observed_at=now,
            ),),
        )
        return await self.list_fact_group(seed, session_id=identity.scope_id)
    async def get(self, memory_id: str) -> MemoryRecord | None: return self.items.get(memory_id)
    async def revisions(self, memory_id: str) -> list[MemoryRecord]:
        value = self.items.get(memory_id)
        return [] if value is None else [value]
    async def related_entries(self, memory_id: str) -> list[MemoryRelationRecord]:
        related: list[MemoryRelationRecord] = []
        for source_id, target_id, kind, operation_id in self.relations:
            if source_id == memory_id:
                other_id = target_id
            elif target_id == memory_id:
                other_id = source_id
            else:
                continue
            record = self.items.get(other_id)
            if record is not None:
                related.append(MemoryRelationRecord(operation_id, kind, other_id, record.content, record.status.value, record.created_at))
        return related
    async def create_memory(self, command: MemoryWriteCommand) -> str:
        key = command.operation_key or f"memory-{len(self.items) + 1}"
        metadata = dict(command.metadata)
        fingerprint = _fake_memory_suppression_fingerprint(metadata, command.content)
        source_ids = _fake_memory_source_ids(metadata)
        if any((source_id, fingerprint) in self.suppressed_sources for source_id in source_ids):
            raise MemorySuppressedError("memory_source_forgotten")
        existing = self.items.get(key)
        if existing is not None and existing.status is MemoryStatus.DELETED:
            raise MemorySuppressedError("memory_source_forgotten")
        self.items[key] = MemoryRecord(key, command.content, dict(command.metadata))
        return key
    async def revise(
        self,
        memory_id: str,
        content: str,
        *,
        metadata: dict[str, object] | None = None,
        operation_key: str | None = None,
        expected_memory_ids: tuple[str, ...] | None = None,
    ) -> str | None:
        if memory_id not in self.items: return None
        self.items[memory_id] = replace(self.items[memory_id], content=content, metadata=metadata or self.items[memory_id].metadata)
        return memory_id
    async def add_relation(self, source_memory_id: str, target_memory_id: str, kind: str, *, operation_id: str) -> None:
        relation = (source_memory_id, target_memory_id, kind, operation_id)
        if relation not in self.relations: self.relations.append(relation)
    async def update_validity(self, memory_id: str, validity_status: str, *, valid_until: str | None = None) -> bool: return memory_id in self.items
    async def delete(self, memory_id: str) -> bool:
        if memory_id not in self.items:
            return False
        record = self.items[memory_id]
        fingerprint = _fake_memory_suppression_fingerprint(record.metadata, record.content)
        self.suppressed_sources.update(
            (source_id, fingerprint)
            for source_id in _fake_memory_source_ids(record.metadata)
        )
        self.items[memory_id] = replace(record, status=MemoryStatus.DELETED)
        return True
    async def record_access(self, memory_ids: list[str]) -> int:
        """测试端口无需统计，返回 0，无业务异常。"""
        return 0
    async def flush_access_stats(self) -> int: return 0


def _fake_memory_source_ids(metadata: dict[str, Any]) -> tuple[str, ...]:
    source_ids = set()
    source_turn_id = metadata.get("source_turn_id")
    if isinstance(source_turn_id, str) and source_turn_id:
        source_ids.add(source_turn_id)
    raw_fact = metadata.get("fact")
    if raw_fact is not None:
        fact = raw_fact if isinstance(raw_fact, MemoryFact) else MemoryFact.model_validate(raw_fact)
        source_ids.update(
            item.source_id for item in fact.source_refs if item.source_type == "turn"
        )
    return tuple(sorted(source_ids))


def _fake_memory_suppression_fingerprint(
    metadata: dict[str, Any], content: str
) -> str:
    raw_fact = metadata.get("fact")
    fact = None if raw_fact is None else (
        raw_fact if isinstance(raw_fact, MemoryFact) else MemoryFact.model_validate(raw_fact)
    )
    identity = (
        ("fact", fact.scope_kind, fact.scope_id, fact.subject_key, fact.fact_key)
        if fact is not None
        else (
            "text",
            str(metadata.get("type", metadata.get("category", ""))),
            " ".join(content.casefold().split()),
        )
    )
    return hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class InMemoryMemoryEpisodeRepository(MemoryEpisodeRepositoryPort):
    """测试用的按来源轮次幂等 episode 仓储。"""

    def __init__(self) -> None:
        """初始化空的回顾记录表。"""
        self.items: dict[str, MemoryEpisode] = {}
        self.suppressed_turns: set[str] = set()

    async def get_by_turn(self, turn_id: str) -> MemoryEpisode | None:
        """按来源轮次读取回顾。

        参数：turn_id 是已完成 Run 的稳定 ID。
        返回：已有回顾；不存在时返回 None。
        异常：不主动抛出业务异常。
        """
        return self.items.get(turn_id)

    async def create_if_absent(self, episode: MemoryEpisode) -> MemoryEpisode | None:
        """保存首次回顾并返回同轮次胜出的记录。

        参数：episode 是已完成来源校验的任务回顾。
        返回：已保存或此前保存的同轮回顾。
        异常：不主动抛出业务异常。
        """
        if episode.turn_id in self.suppressed_turns:
            return None
        return self.items.setdefault(episode.turn_id, episode)

    async def list_recent(
        self, *, limit: int, offset: int, session_id: str | None = None
    ) -> tuple[list[MemoryEpisode], int]:
        visible = [
            episode
            for episode in self.items.values()
            if episode.turn_id not in self.suppressed_turns
            and (session_id is None or episode.session_id == session_id)
        ]
        visible.sort(key=lambda episode: episode.created_at, reverse=True)
        return visible[offset:offset + limit], len(visible)

    async def forget(self, episode_id: str) -> bool:
        episode = next(
            (item for item in self.items.values() if item.episode_id == episode_id),
            None,
        )
        if episode is None:
            return False
        self.suppressed_turns.add(episode.turn_id)
        self.items.pop(episode.turn_id, None)
        return True

    async def search(
        self, session_id: str, request: MemoryEpisodeSearchRequest
    ) -> list[MemoryEpisode]:
        """在测试记录中按会话和词项匹配并应用有界限制。

        参数：session_id 是唯一搜索范围；request 提供查询词和候选上限。
        返回：按创建时间倒序排列的匹配任务回顾。
        异常：不主动抛出业务异常。
        """
        terms = tuple(term.casefold() for term in request.terms)
        matches = [
            episode
            for episode in self.items.values()
            if episode.turn_id not in self.suppressed_turns
            and (
                (
                    episode.session_id == session_id
                    and episode.project_id is None
                )
                or (
                    request.project_id is not None
                    and episode.project_id == request.project_id
                )
            )
            and any(term in episode.content.casefold() for term in terms)
        ]
        matches.sort(key=lambda episode: episode.created_at, reverse=True)
        return matches[: request.limit]

    async def expand_sources(
        self,
        episode_id: str,
        *,
        session_id: str,
        project_id: str | None,
        limit: int,
    ) -> tuple[MemorySourceReference, ...]:
        """在测试仓储中按同一范围返回冻结来源片段。"""
        episode = next(
            (
                value
                for value in self.items.values()
                if value.turn_id not in self.suppressed_turns
                and value.episode_id == episode_id
                and (
                    (value.session_id == session_id and value.project_id is None)
                    or (project_id is not None and value.project_id == project_id)
                )
            ),
            None,
        )
        return () if episode is None else episode.source_refs[: min(4, max(0, limit))]


class InMemoryApprovalRepository(ApprovalRepository):
    def __init__(self) -> None: self.items: dict[str, ApprovalRequest] = {}
    async def get(self, approval_id: str) -> ApprovalRequest | None: return self.items.get(approval_id)
    async def list_pending(self, session_id: str | None = None) -> list[ApprovalRequest]:
        return [value for value in self.items.values() if value.decision is None and (session_id is None or value.session_id == session_id)]
    async def list_history(self, session_id: str | None = None, *, limit: int = 50, offset: int = 0) -> list[ApprovalRequest]:
        values = [value for value in self.items.values() if value.decision is not None and (session_id is None or value.session_id == session_id)]
        return values[offset:offset + limit]
    async def resolve(self, approval_id: str, *, decision: ApprovalDecision) -> bool:
        value = self.items.get(approval_id)
        if value is None or value.decision is not None: return False
        self.items[approval_id] = replace(value, decision=decision, status=decision.value)
        return True


class InMemoryProviderQueries(ProviderQueryPort, SettingsQueryPort, RetrievalPort, ToolUsageQueryPort, MemoryQueryPort):
    def __init__(self, settings: dict[str, Any], memory: InMemoryMemoryPort) -> None:
        self.settings = settings
        self.memory = memory
    async def list_providers(self) -> list[dict[str, Any]]: return self.settings.get("providers", [])
    async def get_settings(self) -> dict[str, Any]: return self.settings
    async def last_called_by_tool(self) -> dict[str, str]: return {}
    async def count_calls_since(self, since: Any) -> int: return 0
    @staticmethod
    def _record(value: MemoryRecord) -> dict[str, Any]:
        return {"id": value.id, "content": value.content, "metadata": dict(value.metadata), "score": value.score, "source": value.source, "status": value.status.value}
    async def search(self, *, query: str, n_results: int, where: dict[str, Any] | None) -> list[dict[str, Any]]: return [self._record(value) for value in await self.memory.search(MemorySearchRequest(query=query, limit=n_results))]
    async def list_memories(self, *, limit: int, offset: int, expired: bool, session_id: str | None) -> dict[str, Any]:
        page = await self.memory.list_memories(MemoryListRequest(limit=limit, offset=offset, expired_only=expired, session_id=session_id))
        return {"items": [self._record(value) for value in page.items], **page.stats}
    async def get(self, memory_id: str) -> dict[str, Any] | None:
        value = await self.memory.get(memory_id); return None if value is None else self._record(value)
    async def revisions(self, memory_id: str) -> list[dict[str, Any]]: return [self._record(value) for value in await self.memory.revisions(memory_id)]
    async def list_runs(self, **filters: Any) -> tuple[list[dict[str, Any]], int]: return ([], 0)
    async def get_run(self, run_id: str) -> dict[str, Any] | None: return None


class InMemoryKnowledgeRepository(KnowledgeBaseRepository):
    def __init__(self) -> None: self.items: dict[str, KnowledgeBase] = {}
    async def create(self, item: KnowledgeBase) -> KnowledgeBase: self.items[item.id] = item; return item
    async def get(self, item_id: str) -> KnowledgeBase | None: return self.items.get(item_id)
    async def list_knowledge_bases(self) -> list[KnowledgeBase]: return list(self.items.values())
    async def save(self, item: KnowledgeBase) -> KnowledgeBase: self.items[item.id] = item; return item
    async def delete(self, item_id: str) -> bool: return self.items.pop(item_id, None) is not None


class InMemoryFileStorage(FileStoragePort):
    def __init__(self) -> None: self.blobs: dict[str, bytes] = {}
    async def save_stream(self, chunks):
        data = bytearray()
        async for chunk in chunks: data.extend(chunk)
        digest = hashlib.sha256(data).hexdigest(); key = digest
        self.blobs[key] = bytes(data)
        return StoredBlob(digest, len(data), key)
    async def read(self, storage_key: str) -> bytes: return self.blobs[storage_key]
    async def cleanup_unreferenced(self, live_storage_keys: set[str]) -> int:
        removed = len([key for key in self.blobs if key not in live_storage_keys])
        for key in list(self.blobs):
            if key not in live_storage_keys: self.blobs.pop(key)
        return removed
    async def discard(self, storage_key: str) -> None: self.blobs.pop(storage_key, None)


class InMemoryFileAdapters(FileAdapterRegistryPort):
    def __init__(self) -> None:
        self.adapter = AdapterInfo("text", "1", ("text/plain", "application/octet-stream"), (".txt", ".md", ".json", ".csv"), ("text",))
    def select(self, filename: str, mime_type: str) -> AdapterInfo:
        if "." not in filename: raise ValueError("unsupported file type")
        return self.adapter
    def supported_extensions(self) -> list[str]: return list(self.adapter.extensions)


class InMemoryDocumentJobs(DocumentJobPort):
    def __init__(self) -> None: self.items: set[str] = set()
    async def enqueue(self, attachment_id: str) -> bool: self.items.add(attachment_id); return True
    async def cancel(self, attachment_id: str) -> None: self.items.discard(attachment_id)


class InMemoryAttachmentRepository(AttachmentRepository):
    def __init__(self) -> None: self.items: dict[str, Attachment] = {}
    async def create(self, attachment: Attachment) -> Attachment: self.items[attachment.id] = attachment; return attachment
    async def get(self, attachment_id: str, *, session_id: str | None = None) -> Attachment | None:
        value = self.items.get(attachment_id)
        return value if value and value.deleted_time is None and (session_id is None or value.session_id == session_id) else None
    async def list_by_session(self, session_id: str) -> list[Attachment]: return [v for v in self.items.values() if v.session_id == session_id and v.deleted_time is None]
    async def list_by_knowledge_base(self, knowledge_base_id: str) -> list[Attachment]: return [v for v in self.items.values() if v.knowledge_base_id == knowledge_base_id and v.deleted_time is None]
    async def list_all_attachments(self) -> list[Attachment]: return [v for v in self.items.values() if v.deleted_time is None]
    async def save(self, attachment: Attachment) -> Attachment: self.items[attachment.id] = attachment; return attachment
    async def soft_delete(self, attachment_id: str, *, session_id: str) -> bool:
        value = await self.get(attachment_id, session_id=session_id)
        if value is None: return False
        self.items[attachment_id] = replace(value, status=AttachmentStatus.DELETED, deleted_time=datetime.now(timezone.utc)); return True


class InMemoryKnowledgeDocuments(InMemoryAttachmentRepository, KnowledgeDocumentRepository):
    async def get(self, attachment_id: str, *, knowledge_base_id: str):
        value = self.items.get(attachment_id)
        return value if value and value.knowledge_base_id == knowledge_base_id and value.deleted_time is None else None
    async def list_documents(self, knowledge_base_id: str): return await self.list_by_knowledge_base(knowledge_base_id)
    async def list_versions(self, attachment_id: str, *, knowledge_base_id: str):
        value = self.items.get(attachment_id)
        return [] if value is None else [v for v in self.items.values() if v.logical_document_id == value.logical_document_id and v.knowledge_base_id == knowledge_base_id]
    async def latest_for_filename(self, filename: str, *, knowledge_base_id: str):
        values = [v for v in self.items.values() if v.filename == filename and v.knowledge_base_id == knowledge_base_id and v.deleted_time is None]
        return max(values, key=lambda v: v.document_version, default=None)
    async def soft_delete(self, attachment_id: str, *, knowledge_base_id: str) -> bool:
        value = await self.get(attachment_id, knowledge_base_id=knowledge_base_id)
        if value is None: return False
        self.items[attachment_id] = replace(value, status=AttachmentStatus.DELETED, deleted_time=datetime.now(timezone.utc)); return True


class InMemoryEventStore(EventPort):
    def __init__(self) -> None: self.items: dict[str, list[ApplicationEvent]] = {}; self.subscribers: dict[str, list[asyncio.Queue[ApplicationEvent]]] = {}
    async def publish(self, event: ApplicationEvent) -> ApplicationEvent:
        values = self.items.setdefault(event.session_id, []); event = replace(event, session_seq=len(values) + 1); values.append(event)
        for queue in self.subscribers.get(event.session_id, []): queue.put_nowait(event)
        return event
    async def publish_realtime(self, event: ApplicationEvent) -> ApplicationEvent:
        for queue in self.subscribers.get(event.session_id, []): queue.put_nowait(event)
        return event
    async def open_subscription(
        self, session_id: str
    ) -> tuple[asyncio.Queue[ApplicationEvent], int]:
        queue: asyncio.Queue[ApplicationEvent] = asyncio.Queue(); self.subscribers.setdefault(session_id, []).append(queue)
        return queue, len(self.items.get(session_id, []))
    async def list_events_between(
        self, session_id: str, after: int = 0, upto: int | None = None,
        limit: int = 256,
    ) -> list[ApplicationEvent]:
        values = [
            event for event in self.items.get(session_id, [])
            if (event.session_seq or 0) > after
            and (upto is None or (event.session_seq or 0) <= upto)
        ]
        return values[:limit]
    async def list_events_for_run(
        self,
        session_id: str,
        run_id: str,
        *,
        after: int = 0,
        limit: int = 200,
    ) -> tuple[list[ApplicationEvent], bool]:
        values = [event for event in self.items.get(session_id, []) if event.run_id == run_id and (event.session_seq or 0) > after]
        return values[:limit], len(values) > limit
    async def close_subscription(self, session_id: str, queue: asyncio.Queue[ApplicationEvent]) -> None:
        if queue in self.subscribers.get(session_id, []): self.subscribers[session_id].remove(queue)


class InMemoryEventPublisher(EventPort):
    def __init__(self, store: InMemoryEventStore) -> None: self.store = store
    async def publish(self, event: ApplicationEvent) -> ApplicationEvent: return await self.store.publish(event)
    async def publish_realtime(self, event: ApplicationEvent) -> ApplicationEvent: return await self.store.publish_realtime(event)
    async def list_events_between(self, session_id: str, after: int = 0, upto: int | None = None, limit: int = 256) -> list[ApplicationEvent]:
        """把历史查询转交给共享的内存事件库。"""
        return await self.store.list_events_between(session_id, after, upto, limit)

    async def list_events_for_run(
        self,
        session_id: str,
        run_id: str,
        *,
        after: int = 0,
        limit: int = 200,
    ) -> tuple[list[ApplicationEvent], bool]:
        """把运行事件查询转交给共享的内存事件库。"""
        return await self.store.list_events_for_run(
            session_id, run_id, after=after, limit=limit
        )

    async def open_subscription(
        self, session_id: str
    ) -> tuple[asyncio.Queue[ApplicationEvent], int]:
        """把实时订阅转交给共享的内存事件库。"""
        return await self.store.open_subscription(session_id)

    async def close_subscription(
        self, session_id: str, queue: asyncio.Queue[ApplicationEvent]
    ) -> None:
        """关闭共享内存事件库中的订阅。"""
        await self.store.close_subscription(session_id, queue)
