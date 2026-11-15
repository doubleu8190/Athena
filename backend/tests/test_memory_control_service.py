"""显式 remember/correct/forget 的来源、版本、回执和敏感边界测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from app.memory.service import MemoryService
from app.memory.write_core import control_arguments_digest
from domain.llm.ports import EmbeddingPort, TokenCounterPort
from domain.memory.lifecycle import (
    ControlCommand,
    ControlReceiptResult,
    ControlSourceSnapshot,
    LoadPolicy,
    MemoryKind,
    MemoryScopeState,
    MemorySourceMessage,
)
from domain.memory.model import AccessContext, MemoryRepositoryPort, RerankerPort
from domain.memory.ports import MemoryControlSourcePort, MemoryLifecycleRepositoryPort

NOW = datetime(2026, 11, 14, tzinfo=UTC)
ACCESS = AccessContext("owner_1", ("personal",), ("personal",))


class _Embedding:
    """返回稳定有效向量并记录正文。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """记录一次正文向量请求。"""
        self.calls.append(texts)
        return [[1.0] * 1024 for _ in texts]


class _Reranker:
    """控制测试不使用重排。"""

    async def score(self, query: str, contents: list[str]) -> list[float]:
        """返回固定满分。"""
        del query
        return [1.0 for _ in contents]


class _Counter:
    """按字符数提供确定 token 估计。"""

    def count(self, text: str) -> int:
        """返回字符长度。"""
        return len(text)


class _Source:
    """返回固定 Run 的直接用户消息。"""

    def __init__(
        self,
        messages: tuple[MemorySourceMessage, ...],
        *,
        project_id: str | None = None,
    ) -> None:
        self.messages = messages
        self.project_id = project_id

    async def read_control_source(
        self,
        *,
        owner_id: str,
        session_id: str,
        run_id: str,
    ) -> ControlSourceSnapshot:
        """返回固定 Run 快照。"""
        return ControlSourceSnapshot(
            owner_id=owner_id,
            auth_session_id="auth_session_1",
            session_id=session_id,
            project_id=self.project_id,
            run_id=run_id,
            messages=self.messages,
        )


class _Lifecycle:
    """模拟 generation 比较、控制命令和回执查询。"""

    def __init__(self) -> None:
        self.generation = 1
        self.applied: list[ControlCommand] = []
        self.receipt_queries: list[tuple[str, str, str]] = []
        self.receipts: dict[tuple[str, str, str], ControlReceiptResult] = {}

    async def control_receipt(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        arguments_digest: str,
    ) -> ControlReceiptResult | None:
        """返回指定控制身份的原回执。"""
        key = (run_id, tool_call_id, arguments_digest)
        self.receipt_queries.append(key)
        return self.receipts.get(key)

    async def scope_state(
        self,
        namespace_id: str,
        scope_key: str,
    ) -> MemoryScopeState:
        """返回已启用 scope。"""
        return MemoryScopeState(
            namespace_id=namespace_id,
            scope_key=scope_key,
            enabled=True,
        )

    async def user_generation(self, namespace_id: str) -> int:
        """返回当前 generation。"""
        del namespace_id
        return self.generation

    async def apply_control(self, command: ControlCommand) -> ControlReceiptResult:
        """按锁内 generation 语义记录一次控制提交。"""
        if command.decision_generation != self.generation:
            raise ValueError("memory_control_generation_changed")
        self.generation += 1
        self.applied.append(command)
        result = ControlReceiptResult(
            operation=command.receipt.operation,
            memory_id=command.create_memory_id,
            superseded_memory_id=(
                command.target_memory_id
                if command.operation == "correct"
                else None
            ),
            generation=self.generation,
        )
        self.receipts[
            (
                command.receipt.run_id,
                command.receipt.tool_call_id,
                command.receipt.arguments_digest,
            )
        ] = result
        return result


class _CommitResponseLost(_Lifecycle):
    """模拟业务已提交但响应丢失。"""

    async def apply_control(self, command: ControlCommand) -> ControlReceiptResult:
        """提交回执后抛出传输错误。"""
        await super().apply_control(command)
        raise RuntimeError("response lost after commit")


class _ConcurrentLifecycle(_Lifecycle):
    """模拟用户行锁下同键控制串行重放。"""

    def __init__(self) -> None:
        super().__init__()
        self._lock = asyncio.Lock()

    async def apply_control(self, command: ControlCommand) -> ControlReceiptResult:
        """只允许一个同键命令真正应用。"""
        async with self._lock:
            key = (
                command.receipt.run_id,
                command.receipt.tool_call_id,
                command.receipt.arguments_digest,
            )
            existing = self.receipts.get(key)
            if existing is not None:
                return existing
            return await super().apply_control(command)


class _GenerationAdvanced(_Lifecycle):
    """模拟 embedding 准备期间其他控制先提交。"""

    async def user_generation(self, namespace_id: str) -> int:
        """首次读取后推进 generation，使提交锁检查失败。"""
        current = await super().user_generation(namespace_id)
        self.generation += 1
        return current


def _service(
    lifecycle: _Lifecycle,
    messages: tuple[MemorySourceMessage, ...],
    *,
    project_id: str | None = None,
    embedding: _Embedding | None = None,
) -> MemoryService:
    """组合显式控制服务和测试替身。"""
    return MemoryService(
        cast(MemoryRepositoryPort, object()),
        cast(EmbeddingPort, embedding or _Embedding()),
        cast(RerankerPort, _Reranker()),
        cast(TokenCounterPort, _Counter()),
        lifecycle=cast(MemoryLifecycleRepositoryPort, lifecycle),
        control_source=cast(
            MemoryControlSourcePort,
            _Source(messages, project_id=project_id),
        ),
    )


async def _remember(
    service: MemoryService,
    *,
    evidence: str = "我喜欢苹果",
    content: str = "我喜欢苹果",
    load_policy: LoadPolicy = LoadPolicy.ON_DEMAND,
    expires_at: datetime | None = None,
) -> ControlReceiptResult:
    """以固定 Run 身份执行 remember。"""
    return await service.remember(
        ACCESS,
        run_id="run_1",
        session_id="session_1",
        tool_call_id="tool_1",
        content=content,
        scope="personal",
        kind=MemoryKind.PREFERENCE,
        load_policy=load_policy,
        evidence=evidence,
        expires_at=expires_at,
    )


def test_remember_saves_direct_content_without_fact_resolution() -> None:
    """remember 直接保存正文，来源由唯一匹配消息推导。"""

    async def scenario() -> None:
        matched = MemorySourceMessage("message_1", "记住，我喜欢苹果", NOW)
        unrelated = MemorySourceMessage("message_2", "天气不错", NOW + timedelta(seconds=1))
        lifecycle = _Lifecycle()
        embedding = _Embedding()
        service = _service(
            lifecycle,
            (matched, unrelated),
            embedding=embedding,
        )

        result = await _remember(service)

        command = lifecycle.applied[0]
        assert result.memory_id is not None
        assert command.operation == "remember"
        assert command.content == "我喜欢苹果"
        assert command.kind is MemoryKind.PREFERENCE
        assert command.load_policy is LoadPolicy.ON_DEMAND
        assert command.evidence_sources == (matched,)
        assert command.target_memory_id is None
        assert command.expected_version is None
        assert not hasattr(command, "fact")
        arguments = MemoryService._control_arguments(
            ACCESS,
            run_id="run_1",
            session_id="session_1",
            tool_call_id="tool_1",
            operation="remember",
            content="我喜欢苹果",
            scope="personal",
            kind=MemoryKind.PREFERENCE,
            load_policy=LoadPolicy.ON_DEMAND,
            evidence="我喜欢苹果",
            expires_at=None,
            target_memory_id=None,
            expected_version=None,
        )
        assert command.receipt.arguments_digest == control_arguments_digest(arguments)
        assert embedding.calls == [["我喜欢苹果"]]

    asyncio.run(scenario())


def test_correct_requires_target_version_and_preserves_superseded_receipt() -> None:
    """correct 用新行替代显式旧版本，并返回旧目标 ID。"""

    async def scenario() -> None:
        source = MemorySourceMessage(
            "message_correct",
            "纠正一下，我喜欢香蕉",
            NOW,
        )
        lifecycle = _Lifecycle()
        service = _service(lifecycle, (source,))

        result = await service.correct(
            ACCESS,
            run_id="run_1",
            session_id="session_1",
            tool_call_id="tool_correct",
            content="我喜欢香蕉",
            scope="personal",
            kind=MemoryKind.PREFERENCE,
            load_policy=LoadPolicy.ON_DEMAND,
            evidence="我喜欢香蕉",
            target_memory_id="mem_apple",
            expected_version=4,
            expires_at=None,
        )

        command = lifecycle.applied[0]
        assert command.operation == "correct"
        assert command.target_memory_id == "mem_apple"
        assert command.expected_version == 4
        assert command.create_memory_id == result.memory_id
        assert result.superseded_memory_id == "mem_apple"

    asyncio.run(scenario())


def test_control_rejects_missing_target_or_stale_expected_version() -> None:
    """correct 必须显式提供正版本；无目标和零版本均 fail closed。"""

    async def scenario() -> None:
        service = _service(
            _Lifecycle(),
            (MemorySourceMessage("message_1", "纠正一下，我喜欢香蕉", NOW),),
        )
        with pytest.raises(ValueError, match="memory_control_target_required"):
            await service.correct(
                ACCESS,
                run_id="run_1",
                session_id="session_1",
                tool_call_id="tool_correct",
                content="我喜欢香蕉",
                scope="personal",
                kind=MemoryKind.PREFERENCE,
                load_policy=LoadPolicy.ON_DEMAND,
                evidence="我喜欢香蕉",
                target_memory_id="",
                expected_version=0,
                expires_at=None,
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("messages", "error_code"),
    [
        (
            (
                MemorySourceMessage("message_intent", "记住这个偏好", NOW),
                MemorySourceMessage("message_evidence", "我喜欢苹果", NOW + timedelta(seconds=1)),
            ),
            "memory_control_intent_missing",
        ),
        (
            (
                MemorySourceMessage("message_intent", "记住，我喜欢苹果", NOW),
                MemorySourceMessage("message_evidence", "我喜欢苹果", NOW + timedelta(seconds=1)),
            ),
            "memory_control_evidence_invalid",
        ),
    ],
)
def test_control_rejects_intent_and_evidence_from_different_messages(
    messages: tuple[MemorySourceMessage, ...],
    error_code: str,
) -> None:
    """同一控制意图和正文证据必须来自唯一消息。"""

    async def scenario() -> None:
        lifecycle = _Lifecycle()
        service = _service(lifecycle, messages)
        with pytest.raises(ValueError, match=error_code):
            await _remember(service)
        assert lifecycle.applied == []

    asyncio.run(scenario())


def test_control_checks_only_content_and_expiry_boundaries() -> None:
    """代码不再推断极性/基数/条件，只校验正文与到期时间的来源边界。"""

    async def scenario() -> None:
        source = MemorySourceMessage(
            "message_1",
            "记住，我不喜欢苹果，2030年12月31日到期",
            NOW,
        )
        lifecycle = _Lifecycle()
        service = _service(lifecycle, (source,))

        await _remember(
            service,
            evidence="我不喜欢苹果",
            content="用户不喜欢苹果",
            expires_at=datetime(2030, 12, 31, tzinfo=UTC),
        )
        assert lifecycle.applied[0].content == "用户不喜欢苹果"

        # 伪造不在用户原话中的 evidence 仍会被拒绝。
        with pytest.raises(ValueError, match="memory_control_evidence_invalid"):
            await _remember(
                service,
                evidence="不存在的原话",
                content="用户不喜欢苹果",
            )

        no_date = _Lifecycle()
        service = _service(
            no_date,
            (MemorySourceMessage("message_2", "记住，我喜欢苹果", NOW),),
        )
        with pytest.raises(ValueError, match="memory_control_evidence_invalid"):
            await _remember(
                service,
                expires_at=datetime(2030, 12, 31, tzinfo=UTC),
            )

    asyncio.run(scenario())


def test_always_load_policy_is_not_downgraded_by_keyword_heuristics() -> None:
    """语义分类交给模型；显式控制代码保留模型给出的 load policy。"""

    async def scenario() -> None:
        lifecycle = _Lifecycle()
        service = _service(
            lifecycle,
            (MemorySourceMessage("message_1", "记住，我喜欢苹果", NOW),),
        )
        await _remember(service, load_policy=LoadPolicy.ALWAYS)
        assert lifecycle.applied[0].load_policy is LoadPolicy.ALWAYS

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("evidence", "content"),
    [
        ("记住，密码是 hunter2", "密码是 hunter2"),
        ("记住，我喜欢苹果", "password=hunter2"),
    ],
)
def test_control_rejects_sensitive_content_and_evidence(
    evidence: str,
    content: str,
) -> None:
    """正文和证据在持久化前都必须通过共享敏感检测。"""

    async def scenario() -> None:
        lifecycle = _Lifecycle()
        service = _service(
            lifecycle,
            (MemorySourceMessage("message_1", evidence, NOW),),
        )
        with pytest.raises(ValueError, match="memory_sensitive_content"):
            await _remember(service, evidence=evidence, content=content)
        assert lifecycle.applied == []

    asyncio.run(scenario())


def test_recover_control_queries_original_digest_without_reapplying() -> None:
    """响应丢失后 recover_control 只查原参数摘要回执。"""

    async def scenario() -> None:
        lifecycle = _CommitResponseLost()
        service = _service(
            lifecycle,
            (MemorySourceMessage("message_1", "记住，我喜欢苹果", NOW),),
        )
        with pytest.raises(RuntimeError, match="response lost"):
            await _remember(service)

        original_digest = lifecycle.applied[0].receipt.arguments_digest
        recovered = await service.recover_control(
            ACCESS,
            run_id="run_1",
            session_id="session_1",
            tool_call_id="tool_1",
            operation="remember",
            content="我喜欢苹果",
            scope="personal",
            kind=MemoryKind.PREFERENCE,
            load_policy=LoadPolicy.ON_DEMAND,
            evidence="我喜欢苹果",
            expires_at=None,
            target_memory_id=None,
            expected_version=None,
        )

        assert recovered is not None
        assert recovered.memory_id is not None
        assert lifecycle.receipt_queries[-1][2] == original_digest
        assert len(lifecycle.applied) == 1

    asyncio.run(scenario())


def test_recovery_reuses_canonical_nfkc_and_whitespace_digest() -> None:
    """首次提交与恢复对全角字符、换行和重复空白生成同一个摘要。"""

    async def scenario() -> None:
        lifecycle = _CommitResponseLost()
        service = _service(
            lifecycle,
            (MemorySourceMessage("message_1", "记住，我喜欢　苹果\n现在", NOW),),
        )
        with pytest.raises(RuntimeError, match="response lost"):
            await _remember(
                service,
                evidence="我喜欢  苹果\n现在",
                content="我喜欢　苹果",
            )

        original_digest = lifecycle.applied[0].receipt.arguments_digest
        recovered = await service.recover_control(
            ACCESS,
            run_id="run_1",
            session_id="session_1",
            tool_call_id="tool_1",
            operation="remember",
            content="  我喜欢\n苹果  ",
            scope="personal",
            kind=MemoryKind.PREFERENCE,
            load_policy=LoadPolicy.ON_DEMAND,
            evidence="我喜欢 苹果 现在",
            expires_at=None,
            target_memory_id=None,
            expected_version=None,
        )

        assert recovered is not None
        assert lifecycle.receipt_queries[-1][2] == original_digest

        changed = await service.recover_control(
            ACCESS,
            run_id="run_1",
            session_id="session_1",
            tool_call_id="tool_1",
            operation="remember",
            content="我喜欢香蕉",
            scope="personal",
            kind=MemoryKind.PREFERENCE,
            load_policy=LoadPolicy.ON_DEMAND,
            evidence="我喜欢 苹果 现在",
            expires_at=None,
            target_memory_id=None,
            expected_version=None,
        )
        assert changed is None
        assert lifecycle.receipt_queries[-1][2] != original_digest
        assert len(lifecycle.applied) == 1

    asyncio.run(scenario())


def test_generation_change_between_read_and_commit_is_rejected() -> None:
    """generation 在事务外读取，提交时变化必须 fail closed。"""

    async def scenario() -> None:
        lifecycle = _GenerationAdvanced()
        service = _service(
            lifecycle,
            (MemorySourceMessage("message_1", "记住，我喜欢苹果", NOW),),
        )
        with pytest.raises(ValueError, match="memory_control_generation_changed"):
            await _remember(service)
        assert lifecycle.applied == []

    asyncio.run(scenario())


def test_concurrent_same_control_key_replays_one_receipt() -> None:
    """同一 Run、tool call 和参数摘要并发只应用一次。"""

    async def scenario() -> None:
        lifecycle = _ConcurrentLifecycle()
        service = _service(
            lifecycle,
            (MemorySourceMessage("message_1", "记住，我喜欢苹果", NOW),),
        )

        first, second = await asyncio.gather(
            _remember(service),
            _remember(service),
        )

        assert first == second
        assert len(lifecycle.applied) == 1
        assert lifecycle.generation == 2

    asyncio.run(scenario())


def test_forget_and_recover_forget_keep_explicit_target_control() -> None:
    """forget 保持显式目标和版本，recover_forget_control 不改写正文。"""

    async def scenario() -> None:
        lifecycle = _CommitResponseLost()
        source = MemorySourceMessage("message_forget", "忘记我喜欢苹果", NOW)
        service = _service(lifecycle, (source,))

        with pytest.raises(RuntimeError, match="response lost"):
            await service.forget(
                ACCESS,
                run_id="run_1",
                session_id="session_1",
                tool_call_id="tool_forget",
                scope="personal",
                evidence="忘记我喜欢苹果",
                target_memory_id="mem_apple",
                expected_version=2,
                clear_scope=False,
            )
        command = lifecycle.applied[0]
        assert command.operation == "forget"
        assert command.target_memory_id == "mem_apple"
        assert command.expected_version == 2

        recovered = await service.recover_forget_control(
            ACCESS,
            run_id="run_1",
            session_id="session_1",
            tool_call_id="tool_forget",
            scope="personal",
            evidence="忘记我喜欢苹果",
            target_memory_id="mem_apple",
            expected_version=2,
            clear_scope=False,
        )
        assert recovered is not None
        assert len(lifecycle.applied) == 1

    asyncio.run(scenario())
