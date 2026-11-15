"""联合提取、相似检索和一次批量 ADD/UPDATE/IGNORE 决策测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

import pytest

from app.memory.extraction import (
    ExtractionContextBudgetExceeded,
    ExtractionLimits,
    ExtractionSchemaError,
    MemoryExtractionService,
)
from domain.llm.model.profile import ModelProfileVersion, RuntimeModelProfile
from domain.llm.model.types import LlmRequest, LlmResponse, LlmText
from domain.memory.lifecycle import (
    DecisionAction,
    ExtractBatchMember,
    ExtractBatchPayload,
    LoadPolicy,
    MemoryCandidate,
    MemoryKind,
    MemorySourceMessage,
)
from domain.memory.model import AccessContext, MemorySource, RetrievedMemory
from domain.memory.ports import MemoryLifecycleRepositoryPort

NOW = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)


class _Embedding:
    """返回固定 1024 维向量并记录批次调用。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """记录待嵌入正文并返回有限非零向量。"""
        self.calls.append(texts)
        return [[0.1] * 1024 for _ in texts]


class _Provider:
    """按顺序返回预设 JSON 文本。"""

    def __init__(self, outputs: list[dict[str, object]]) -> None:
        self._outputs = [json.dumps(item, ensure_ascii=False) for item in outputs]
        self.requests: list[LlmRequest] = []

    async def complete(
        self,
        runtime: RuntimeModelProfile,
        request: LlmRequest,
    ) -> LlmResponse:
        """记录禁用工具的请求并返回下一段预设文本。"""
        del runtime
        self.requests.append(request)
        return LlmResponse(
            output=(LlmText(self._outputs.pop(0)),),
            status="completed",
            incomplete_reason=None,
            model="memory-test",
            response_id=None,
            request_id=None,
            usage=None,
        )


class _Counter:
    """用字符长度模拟 token，便于稳定验证上下文预算。"""

    def count(self, text: str) -> int:
        """返回固定近似 token 数。"""
        return len(text)


class _Repository:
    """提供 generation 和按 query 预置的检索快照。"""

    def __init__(
        self,
        *,
        generation: int = 1,
        retrieval: dict[str, tuple[RetrievedMemory, ...]] | None = None,
    ) -> None:
        self.generation = generation
        self.retrieval = retrieval or {}
        self.retrieval_calls: list[tuple[str, str, int]] = []

    async def user_generation(self, namespace_id: str) -> int:
        """返回当前固定 generation。"""
        del namespace_id
        return self.generation

    async def retrieve_similar_batch(
        self,
        access: AccessContext,
        queries: tuple[tuple[str, str, tuple[float, ...]], ...],
        embedding_profile_id: str,
        limit: int = 5,
    ) -> tuple[tuple[RetrievedMemory, ...], ...]:
        """在单次批量调用中依次返回各候选的预设检索结果。"""
        assert access.namespace_id == "owner_test"
        assert embedding_profile_id == "embedding-test"
        assert limit == 5
        results: list[tuple[RetrievedMemory, ...]] = []
        for scope_key, query, embedding in queries:
            assert scope_key in access.readable_scopes
            assert len(embedding) == 1024
            self.retrieval_calls.append((scope_key, query, limit))
            results.append(self.retrieval.get(query, ()))
        return tuple(results)


def _runtime() -> RuntimeModelProfile:
    """构造不含真实凭据的固定模型快照。"""
    return RuntimeModelProfile(
        version=ModelProfileVersion(
            id="llmprof_memory_test",
            name="memory-test",
            protocol="chat_completions",
            base_url="https://llm.example.test/v1",
            model="memory-test",
            has_api_key=True,
            context_window_tokens=32_768,
            tokenizer_encoding="o200k_base",
            supports_stream=False,
            supports_tools=False,
            max_output_tokens=8_192,
            request_timeout_seconds=30,
            execution_timeout_seconds=60,
            max_concurrency=1,
            temperature=None,
            enabled=True,
            created_at=NOW,
        ),
        api_key="test-only",
    )


def _service(
    repository: _Repository,
    provider: _Provider,
    *,
    limits: ExtractionLimits | None = None,
) -> MemoryExtractionService:
    """装配提取服务及无外部依赖替身。"""
    return MemoryExtractionService(
        cast(MemoryLifecycleRepositoryPort, repository),
        provider,
        _Embedding(),
        _Counter(),
        embedding_profile_id="embedding-test",
        limits=limits,
    )


def _source(
    message_id: str,
    text: str,
    source_at: datetime = NOW,
) -> MemorySourceMessage:
    """构造一条固定直接用户来源。"""
    return MemorySourceMessage(
        message_id=message_id,
        text=text,
        source_at=source_at,
    )


def _batch(
    sources: tuple[MemorySourceMessage, ...],
    *,
    candidates: tuple[MemoryCandidate, ...] = (),
    extraction_complete: bool = True,
    generation: int = 1,
    project_id: str | None = None,
) -> ExtractBatchPayload:
    """把来源绑定到单 Run 批次并固定 prompt version 2。"""
    members = tuple(
        ExtractBatchMember(
            run_id=f"run_{index}",
            session_id=f"session_{index}",
            project_id=project_id,
            source_message_ids=(source.message_id,),
            source_cutoff_at=source.source_at,
        )
        for index, source in enumerate(sources, start=1)
    )
    scope_keys = (
        ("personal",)
        if project_id is None
        else ("personal", f"project:{project_id}")
    )
    return ExtractBatchPayload(
        batch_id="batch_test",
        owner_id="owner_test",
        generation=generation,
        members=members,
        model_profile_version_id="llmprof_memory_test",
        scope_keys=scope_keys,
        created_at=NOW,
        candidates=candidates,
        extraction_complete=extraction_complete,
    )


def _candidate(
    content: str,
    evidence: str,
    source_id: str,
    *,
    kind: MemoryKind = MemoryKind.PREFERENCE,
    scope: str = "personal",
    load_policy: LoadPolicy = LoadPolicy.ON_DEMAND,
    classification_reason: str = "用户直接表达",
) -> MemoryCandidate:
    """构造不含结构事实字段的新候选。"""
    return MemoryCandidate(
        content=content,
        kind=kind,
        scope=cast(Literal["personal", "project"], scope),
        source_message_ids=(source_id,),
        evidence=evidence,
        proposed_load_policy=load_policy,
        classification_reason=classification_reason,
        expires_at=None,
    )


def _candidate_payload(
    content: str,
    evidence: str,
    source_id: str,
    *,
    scope: str = "personal",
) -> dict[str, object]:
    """构造首轮模型的完整 JSON 候选。"""
    return {
        "content": content,
        "kind": "preference",
        "scope": scope,
        "source_message_ids": [source_id],
        "evidence": evidence,
        "proposed_load_policy": "on_demand",
        "classification_reason": "用户直接表达",
        "expires_at": None,
    }


def _retrieved(
    memory_id: str,
    content: str,
    *,
    version: int = 3,
    status: str = "active",
    scope_key: str = "personal",
) -> RetrievedMemory:
    """构造服务端检索快照。"""
    return RetrievedMemory(
        id=memory_id,
        namespace_id="owner_test",
        scope_key=scope_key,
        content=content,
        version=version,
        status=cast(Literal["active", "archived"], status),
        kind=MemoryKind.PREFERENCE,
        load_policy=LoadPolicy.ON_DEMAND,
        expires_at=None,
        updated_at=NOW,
        sources=(MemorySource("message", "old-message", NOW),),
    )


def test_distinct_likes_add_side_by_side_with_one_batch_decision() -> None:
    """苹果和香蕉是两个兼容喜好，首轮一次、批量决策一次且都 ADD。"""

    async def scenario() -> None:
        apple = _source("msg_apple", "我喜欢苹果")
        banana = _source("msg_banana", "我也喜欢香蕉")
        provider = _Provider(
            [
                {
                    "candidates": [
                        _candidate_payload("用户喜欢苹果", apple.text, apple.message_id),
                        _candidate_payload("用户喜欢香蕉", banana.text, banana.message_id),
                    ]
                },
                {
                    "decisions": [
                        {"candidate_index": 0, "action": "ADD"},
                        {"candidate_index": 1, "action": "ADD"},
                    ]
                },
            ]
        )
        service = _service(_Repository(), provider)
        batch = _batch(
            (apple, banana),
            extraction_complete=False,
        )

        extracted = await service.complete_extraction(
            batch,
            _runtime(),
            (apple, banana),
        )
        completed = await service.complete_decisions(
            extracted,
            _runtime(),
            (apple, banana),
        )

        assert [item.action for item in completed.decisions] == [
            DecisionAction.ADD,
            DecisionAction.ADD,
        ]
        assert len(provider.requests) == 2
        assert provider.requests[1].max_output_tokens == 4_000
        assert all(request.tool_choice == "none" for request in provider.requests)
        extraction_schema = provider.requests[0].messages[0].content
        assert '"fact_key"' not in extraction_schema
        assert '"cardinality"' not in extraction_schema

    asyncio.run(scenario())


def test_candidates_and_sources_are_ordered_by_original_source_time() -> None:
    """模型反向返回候选和来源时，服务端仍按原始时间分配索引并送决策。"""

    async def scenario() -> None:
        early = _source("msg_early", "我喜欢苹果", NOW - timedelta(minutes=2))
        early_detail = _source(
            "msg_early_detail",
            "我喜欢红苹果",
            NOW - timedelta(minutes=1),
        )
        late = _source("msg_late", "我也喜欢香蕉", NOW)
        provider = _Provider(
            [
                {
                    "candidates": [
                        _candidate_payload("用户喜欢香蕉", late.text, late.message_id),
                        {
                            **_candidate_payload(
                                "用户喜欢苹果",
                                early.text,
                                early.message_id,
                            ),
                            "source_message_ids": [
                                early_detail.message_id,
                                early.message_id,
                            ],
                        },
                    ]
                },
                {
                    "decisions": [
                        {"candidate_index": 0, "action": "ADD"},
                        {"candidate_index": 1, "action": "ADD"},
                    ]
                },
            ]
        )
        repository = _Repository()
        service = _service(repository, provider)
        sources = (late, early_detail, early)

        extracted = await service.complete_extraction(
            _batch(sources, extraction_complete=False),
            _runtime(),
            sources,
        )

        assert [candidate.content for candidate in extracted.candidates] == [
            "用户喜欢苹果",
            "用户喜欢香蕉",
        ]
        assert extracted.candidates[0].source_message_ids == (
            early.message_id,
            early_detail.message_id,
        )

        await service.complete_decisions(extracted, _runtime(), sources)
        request_text = provider.requests[-1].messages[-1].content
        payload = json.loads(request_text.split("\n", 1)[1].rsplit("\n", 1)[0])
        candidates = payload["candidates"]
        assert [candidate["candidate_index"] for candidate in candidates] == [0, 1]
        assert [candidate["content"] for candidate in candidates] == [
            "用户喜欢苹果",
            "用户喜欢香蕉",
        ]
        assert candidates[0]["source_message_ids"] == [
            early.message_id,
            early_detail.message_id,
        ]
        assert [source["message_id"] for source in candidates[0]["sources"]] == [
            early.message_id,
            early_detail.message_id,
        ]
        assert repository.retrieval_calls == [
            ("personal", "用户喜欢苹果", 5),
            ("personal", "用户喜欢香蕉", 5),
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("expires_at", "accepted"),
    [
        ("2030-12-31", False),
        ("2030-12-31T00:00:00", False),
        ("2030-12-31T00:00:00Z", True),
    ],
)
def test_candidate_expiry_requires_an_aware_datetime(
    expires_at: str,
    accepted: bool,
) -> None:
    """date-only 和 naive datetime 稳定拒绝，只有带时区值可进入候选。"""

    async def scenario() -> None:
        source = _source("msg_expiry", "这条偏好在2030年12月31日到期")
        candidate = _candidate_payload(
            "用户偏好到2030年12月31日",
            source.text,
            source.message_id,
        )
        candidate["expires_at"] = expires_at
        provider = _Provider([{"candidates": [candidate]}])
        service = _service(_Repository(), provider)

        if not accepted:
            with pytest.raises(
                ExtractionSchemaError,
                match="memory_extraction_schema_invalid",
            ):
                await service.complete_extraction(
                    _batch((source,), extraction_complete=False),
                    _runtime(),
                    (source,),
                )
            return

        completed = await service.complete_extraction(
            _batch((source,), extraction_complete=False),
            _runtime(),
            (source,),
        )
        assert completed.candidates[0].expires_at is not None
        assert completed.candidates[0].expires_at.utcoffset() is not None

    asyncio.run(scenario())


def test_explicit_favorite_update_uses_snapshot_version_and_archived_target() -> None:
    """明确纠正只给目标 ID，版本由检索快照固定且 archived 可被 UPDATE 激活。"""

    async def scenario() -> None:
        source = _source("msg_favorite", "纠正一下，我最喜欢香蕉")
        candidate = _candidate(
            "用户最喜欢香蕉",
            source.text,
            source.message_id,
        )
        old = _retrieved(
            "mem_apple",
            "用户最喜欢苹果",
            version=7,
            status="archived",
        )
        provider = _Provider(
            [
                {
                    "decisions": [
                        {
                            "candidate_index": 0,
                            "action": "UPDATE",
                            "target_memory_id": old.id,
                            "content": "用户最喜欢香蕉，此前最喜欢苹果",
                        }
                    ]
                }
            ]
        )
        repository = _Repository(retrieval={candidate.content: (old,)})
        service = _service(repository, provider)

        completed = await service.complete_decisions(
            _batch((source,), candidates=(candidate,)),
            _runtime(),
            (source,),
        )
        changes = await service.prepare_changes(completed, (source,))

        decision = completed.decisions[0]
        assert decision.action is DecisionAction.UPDATE
        assert decision.target_memory_id == old.id
        assert decision.target_version == 7
        assert decision.scope_key == "personal"
        assert changes[0].action is DecisionAction.UPDATE
        assert changes[0].create is not None
        assert changes[0].create.content == "用户最喜欢香蕉，此前最喜欢苹果"
        assert changes[0].source_evidence == (source,)
        assert repository.retrieval_calls == [("personal", candidate.content, 5)]

    asyncio.run(scenario())


def test_same_batch_final_value_still_uses_one_decision_without_old_retrieval() -> None:
    """无旧检索结果时也必须一次批量处理同批中间值和最终纠正。"""

    async def scenario() -> None:
        apple = _source("msg_batch_apple", "我最喜欢苹果")
        banana = _source("msg_batch_banana", "纠正一下，现在最喜欢香蕉")
        candidates = (
            _candidate("用户最喜欢苹果", apple.text, apple.message_id),
            _candidate("用户最喜欢香蕉", banana.text, banana.message_id),
        )
        provider = _Provider(
            [
                {
                    "decisions": [
                        {"candidate_index": 0, "action": "IGNORE"},
                        {"candidate_index": 1, "action": "ADD"},
                    ]
                }
            ]
        )
        repository = _Repository()
        service = _service(repository, provider)

        completed = await service.complete_decisions(
            _batch((apple, banana), candidates=candidates),
            _runtime(),
            (apple, banana),
        )

        assert len(provider.requests) == 1
        assert len(repository.retrieval_calls) == 2
        assert [item.action for item in completed.decisions] == [
            DecisionAction.IGNORE,
            DecisionAction.ADD,
        ]

    asyncio.run(scenario())


def test_same_batch_later_language_rule_ignores_earlier_duplicate() -> None:
    """较早规则命中旧记忆仍须 IGNORE，只有同批最终规则可以 UPDATE。"""

    async def scenario() -> None:
        chinese = _source(
            "msg_language_chinese",
            "以后都用中文回答。",
            NOW,
        )
        english = _source(
            "msg_language_english",
            "我改一下：以后都用英文回答。",
            NOW + timedelta(minutes=1),
        )
        chinese_candidate = _candidate(
            "用户要求以后都用中文回答",
            chinese.text,
            chinese.message_id,
            kind=MemoryKind.CONSTRAINT,
            load_policy=LoadPolicy.ALWAYS,
        )
        english_candidate = _candidate(
            "用户要求以后都用英文回答",
            english.text,
            english.message_id,
            kind=MemoryKind.CONSTRAINT,
            load_policy=LoadPolicy.ALWAYS,
        )
        old = _retrieved(
            "mem_language_rule",
            "用户要求以后都用中文回答",
            version=3,
        )
        provider = _Provider(
            [
                {
                    "decisions": [
                        {"candidate_index": 0, "action": "IGNORE"},
                        {
                            "candidate_index": 1,
                            "action": "UPDATE",
                            "target_memory_id": old.id,
                            "content": "用户要求以后都用英文回答",
                        },
                    ]
                }
            ]
        )
        repository = _Repository(
            retrieval={
                chinese_candidate.content: (old,),
                english_candidate.content: (old,),
            }
        )
        service = _service(repository, provider)

        completed = await service.complete_decisions(
            _batch(
                (chinese, english),
                candidates=(chinese_candidate, english_candidate),
            ),
            _runtime(),
            (chinese, english),
        )
        changes = await service.prepare_changes(completed, (chinese, english))

        assert len(provider.requests) == 1
        system_prompt = provider.requests[0].messages[0].content
        assert "按整批时间线联合判断" in system_prompt
        assert "较早候选必须 IGNORE" in system_prompt
        assert (
            "较早中文候选 IGNORE，仅最终英文候选 UPDATE 该旧记忆"
            in system_prompt
        )
        assert [decision.action for decision in completed.decisions] == [
            DecisionAction.IGNORE,
            DecisionAction.UPDATE,
        ]
        assert [
            change.candidate_index
            for change in changes
            if change.action is not DecisionAction.IGNORE
        ] == [1]

    asyncio.run(scenario())


def test_no_candidates_skips_decision_model_call() -> None:
    """没有候选时正常完成批次，不产生空决策 LLM 调用。"""

    async def scenario() -> None:
        source = _source("msg_empty", "这次用英文回答")
        provider = _Provider([])
        service = _service(_Repository(), provider)

        completed = await service.complete_decisions(
            _batch((source,), candidates=()),
            _runtime(),
            (source,),
        )

        assert completed.decisions_complete is True
        assert completed.decisions == ()
        assert provider.requests == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("target_memory_id", "action"),
    [
        ("mem_not_retrieved", "UPDATE"),
        (None, "ADD"),
    ],
)
def test_decision_target_must_be_in_candidate_whitelist(
    target_memory_id: str | None,
    action: str,
) -> None:
    """未检索 ID 不能 UPDATE；合法 ADD 也不能伪造目标。"""

    async def scenario() -> None:
        source = _source("msg_target", "我最喜欢香蕉")
        candidate = _candidate("用户最喜欢香蕉", source.text, source.message_id)
        decision: dict[str, object] = {
            "candidate_index": 0,
            "action": action,
        }
        if target_memory_id is not None:
            decision["target_memory_id"] = target_memory_id
        elif action == "ADD":
            decision["target_memory_id"] = "mem_forged"
        provider = _Provider([{"decisions": [decision]}])
        service = _service(_Repository(), provider)

        with pytest.raises(ExtractionSchemaError):
            await service.complete_decisions(
                _batch((source,), candidates=(candidate,)),
                _runtime(),
                (source,),
            )

    asyncio.run(scenario())


def test_decision_schema_rejects_model_supplied_version_or_scope() -> None:
    """版本和 scope 只能来自检索快照，模型额外字段直接拒绝。"""

    async def scenario() -> None:
        source = _source("msg_version", "记住，我喜欢苹果")
        candidate = _candidate("用户喜欢苹果", source.text, source.message_id)
        provider = _Provider(
            [
                {
                    "decisions": [
                        {
                            "candidate_index": 0,
                            "action": "ADD",
                            "target_version": 99,
                            "scope_key": "project:forged",
                        }
                    ]
                }
            ]
        )
        service = _service(_Repository(), provider)

        with pytest.raises(ExtractionSchemaError):
            await service.complete_decisions(
                _batch((source,), candidates=(candidate,)),
                _runtime(),
                (source,),
            )

    asyncio.run(scenario())


def test_generation_change_does_not_discard_async_extraction() -> None:
    """无关 generation 变化不再整批作废异步提取，仍正常执行批量检索与决策。"""

    async def scenario() -> None:
        source = _source("msg_generation", "我喜欢苹果")
        candidate = _candidate("用户喜欢苹果", source.text, source.message_id)
        provider = _Provider([{"decisions": [{"candidate_index": 0, "action": "ADD"}]}])
        repository = _Repository(generation=2)
        service = _service(repository, provider)

        decided = await service.complete_decisions(
            _batch((source,), candidates=(candidate,), generation=1),
            _runtime(),
            (source,),
        )

        assert decided.decisions_complete is True
        assert len(repository.retrieval_calls) == 1
        assert len(provider.requests) == 1

    asyncio.run(scenario())


def test_project_scope_requires_project_batch_and_candidate_sources_are_verified() -> None:
    """无项目批次不能伪造 project scope，候选来源必须属于固定消息。"""

    async def scenario() -> None:
        source = _source("msg_scope", "本项目默认使用中文")
        project_candidate = _candidate(
            "本项目默认使用中文",
            source.text,
            source.message_id,
            kind=MemoryKind.CONSTRAINT,
            scope="project",
            load_policy=LoadPolicy.ALWAYS,
        )
        provider = _Provider(
            [
                {
                    "candidates": [
                        {
                            "content": project_candidate.content,
                            "kind": project_candidate.kind.value,
                            "scope": "project",
                            "source_message_ids": [source.message_id],
                            "evidence": source.text,
                            "proposed_load_policy": "always",
                            "classification_reason": "项目长期规则",
                            "expires_at": None,
                        }
                    ]
                }
            ]
        )
        service = _service(_Repository(), provider)
        with pytest.raises(ExtractionSchemaError, match="memory_candidate_scope_forbidden"):
            await service.complete_extraction(
                _batch((source,), extraction_complete=False),
                _runtime(),
                (source,),
            )

        forged = dict(_candidate_payload("用户喜欢苹果", source.text, source.message_id))
        forged["source_message_ids"] = ["msg_forged"]
        provider = _Provider([{"candidates": [forged]}])
        service = _service(_Repository(), provider)
        with pytest.raises(ExtractionSchemaError, match="memory_candidate_source_forged"):
            await service.complete_extraction(
                _batch((source,), extraction_complete=False),
                _runtime(),
                (source,),
            )

    asyncio.run(scenario())


def test_sensitive_candidate_is_skipped_and_forged_evidence_rejected() -> None:
    """敏感候选不落库；不在来源中的伪证据整次拒绝。"""

    async def scenario() -> None:
        source = _source("msg_sensitive", "请记住我的密码是 hunter2")
        sensitive = _candidate_payload(
            "用户密码是 hunter2",
            source.text,
            source.message_id,
        )
        provider = _Provider([{"candidates": [sensitive]}])
        service = _service(_Repository(), provider)
        completed = await service.complete_extraction(
            _batch((source,), extraction_complete=False),
            _runtime(),
            (source,),
        )
        assert completed.candidates == ()

        forged = _candidate_payload("用户喜欢苹果", "我不曾说过这句话", source.message_id)
        provider = _Provider([{"candidates": [forged]}])
        service = _service(_Repository(), provider)
        with pytest.raises(ExtractionSchemaError, match="memory_candidate_evidence_forged"):
            await service.complete_extraction(
                _batch((source,), extraction_complete=False),
                _runtime(),
                (source,),
            )

    asyncio.run(scenario())


def test_decision_changed_content_sensitive_or_ignore_write_is_rejected() -> None:
    """决策改写正文也要做敏感检查，IGNORE 不生成任何写计划。"""

    async def scenario() -> None:
        source = _source("msg_changed", "记住，我喜欢苹果")
        candidate = _candidate("用户喜欢苹果", source.text, source.message_id)
        provider = _Provider(
            [
                {
                    "decisions": [
                        {
                            "candidate_index": 0,
                            "action": "ADD",
                            "content": "用户密码是 hunter2",
                        }
                    ]
                }
            ]
        )
        service = _service(_Repository(), provider)
        with pytest.raises(ExtractionSchemaError, match="memory_sensitive_content"):
            await service.complete_decisions(
                _batch((source,), candidates=(candidate,)),
                _runtime(),
                (source,),
            )

        provider = _Provider(
            [{"decisions": [{"candidate_index": 0, "action": "IGNORE"}]}]
        )
        service = _service(_Repository(), provider)
        completed = await service.complete_decisions(
            _batch((source,), candidates=(candidate,)),
            _runtime(),
            (source,),
        )
        changes = await service.prepare_changes(completed, (source,))
        assert changes[0].action is DecisionAction.IGNORE
        assert changes[0].create is None

    asyncio.run(scenario())


def test_decision_coverage_and_unique_update_targets_are_strict() -> None:
    """缺失索引和同批重复 UPDATE 目标均拒绝。"""

    async def scenario() -> None:
        first = _source("msg_first", "我喜欢苹果")
        second = _source("msg_second", "我也喜欢香蕉")
        candidates = (
            _candidate("用户喜欢苹果", first.text, first.message_id),
            _candidate("用户喜欢香蕉", second.text, second.message_id),
        )
        provider = _Provider(
            [{"decisions": [{"candidate_index": 0, "action": "ADD"}]}]
        )
        service = _service(_Repository(), provider)
        with pytest.raises(ExtractionSchemaError, match="memory_decision_coverage_invalid"):
            await service.complete_decisions(
                _batch((first, second), candidates=candidates),
                _runtime(),
                (first, second),
            )

        old = _retrieved("mem_old", "用户喜欢水果")
        repository = _Repository(
            retrieval={
                candidates[0].content: (old,),
                candidates[1].content: (old,),
            }
        )
        provider = _Provider(
            [
                {
                    "decisions": [
                        {
                            "candidate_index": 0,
                            "action": "UPDATE",
                            "target_memory_id": old.id,
                        },
                        {
                            "candidate_index": 1,
                            "action": "UPDATE",
                            "target_memory_id": old.id,
                        },
                    ]
                }
            ]
        )
        service = _service(repository, provider)
        with pytest.raises(ExtractionSchemaError, match="memory_decision_target_duplicate"):
            await service.complete_decisions(
                _batch((first, second), candidates=candidates),
                _runtime(),
                (first, second),
            )

    asyncio.run(scenario())


def test_context_budget_fails_before_batch_decision_call() -> None:
    """完整旧记忆上下文超预算时明确失败，不静默删除模型未收到的目标 ID。"""

    async def scenario() -> None:
        source = _source("msg_budget", "我喜欢苹果")
        candidate = _candidate("用户喜欢苹果", source.text, source.message_id)
        old = _retrieved("mem_large", "苹果" * 2_000)
        provider = _Provider([])
        service = _service(
            _Repository(retrieval={candidate.content: (old,)}),
            provider,
            limits=ExtractionLimits(decision_context_max_tokens=512),
        )

        with pytest.raises(
            ExtractionContextBudgetExceeded,
            match="memory_decision_context_too_large",
        ):
            await service.complete_decisions(
                _batch((source,), candidates=(candidate,)),
                _runtime(),
                (source,),
            )
        assert provider.requests == []

    asyncio.run(scenario())


def test_prepare_changes_reuses_candidate_embeddings_and_only_reembeds_rewritten() -> None:
    """prepare_changes 复用未改写决策的候选向量，只为被改写的新正文重新生成向量。"""

    class _TrackingEmbedder:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        async def embed(self, texts: list[str]) -> list[list[float]]:
            self.calls.append(list(texts))
            return [
                [0.2 if text == "用户最喜欢香蕉（改写）" else 0.1] * 1024
                for text in texts
            ]

    async def scenario() -> None:
        first = _source("msg_reuse_1", "我喜欢苹果")
        second = _source("msg_reuse_2", "我喜欢香蕉")
        c1 = _candidate("用户喜欢苹果", first.text, first.message_id)
        c2 = _candidate("用户喜欢香蕉", second.text, second.message_id)
        embedder = _TrackingEmbedder()
        provider = _Provider(
            [
                {
                    "decisions": [
                        {"candidate_index": 0, "action": "ADD"},
                        {
                            "candidate_index": 1,
                            "action": "ADD",
                            "content": "用户最喜欢香蕉（改写）",
                        },
                    ]
                }
            ]
        )
        service = MemoryExtractionService(
            _Repository(),
            provider,
            embedder,
            _Counter(),
            embedding_profile_id="embedding-test",
        )
        decided = await service.complete_decisions(
            _batch((first, second), candidates=(c1, c2)),
            _runtime(),
            (first, second),
        )
        assert embedder.calls == [["用户喜欢苹果", "用户喜欢香蕉"]]

        changes = await service.prepare_changes(decided, (first, second))
        # 第一个决策未改写，直接复用；只对第二个改写后的正文重新生成向量。
        assert embedder.calls == [
            ["用户喜欢苹果", "用户喜欢香蕉"],
            ["用户最喜欢香蕉（改写）"],
        ]
        assert changes[0].create is not None
        assert changes[0].create.embedding[0] == pytest.approx(0.1)
        assert changes[1].create is not None
        assert changes[1].create.embedding[0] == pytest.approx(0.2)

    asyncio.run(scenario())
