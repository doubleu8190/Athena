"""真实文本模型验证 prompt version 2 的联合提取与一次批量决策。"""

from __future__ import annotations

import asyncio
import base64
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from adapter.llm import OpenAIChatCompletionsAdapter
from adapter.llm.repository import SqlAlchemyModelProfileRepository
from app.memory.extraction import ExtractionLimits, MemoryExtractionService
from domain.llm.model.profile import RuntimeModelProfile
from domain.llm.model.types import LlmRequest, LlmResponse
from domain.memory.lifecycle import (
    DecisionAction,
    ExtractBatchMember,
    ExtractBatchPayload,
    LoadPolicy,
    MemoryKind,
    MemorySourceMessage,
)
from domain.memory.model import AccessContext, MemorySource, RetrievedMemory
from domain.memory.ports import MemoryLifecycleRepositoryPort
from infrastructure.security.mcp_secrets import McpSecretCipher

pytestmark = [pytest.mark.integration, pytest.mark.llm_live]

_DEFAULT_PROFILE_ID = "llmprof_f8eb885d5ea34ade90813a199414cb25"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_OWNER_ID = "owner_memory_decision_live"
_ACCESS = AccessContext(
    namespace_id=_OWNER_ID,
    readable_scopes=("personal",),
    writable_scopes=("personal",),
)
_FAVORITE_ID = "mem_live_favorite_apple"
_LANGUAGE_RULE_ID = "mem_live_language_chinese"


class _LiveProfileUnavailable(RuntimeError):
    """只携带可安全展示的 runtime profile 阻断原因。"""


@dataclass(frozen=True, slots=True)
class _ProfileConfig:
    """从进程环境或 literal env 文件读取的只读配置。"""

    database_url: str
    mcp_secret_key: str
    mcp_secret_key_version: int
    profile_id: str


class _ControlledEmbedding:
    """为真实文本模型验收提供稳定向量，避免加载本地向量模型。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """记录候选和最终正文批次并返回固定 1024 维向量。"""
        self.calls.append(list(texts))
        return [[0.1] * 1024 for _ in texts]


class _ControlledCounter:
    """使用有界字符近似，避免 live 测试依赖 tokenizer 下载。"""

    def count(self, text: str) -> int:
        """按约两个字符一个 token 估算上下文预算。"""
        return max(1, (len(text) + 1) // 2)


class _ControlledRepository:
    """按候选语义提供固定 active/archived 旧记忆快照。"""

    def __init__(self) -> None:
        self.retrieval_calls: list[str] = []
        self._favorite = RetrievedMemory(
            id=_FAVORITE_ID,
            namespace_id=_OWNER_ID,
            scope_key="personal",
            content="用户最喜欢的水果是苹果。",
            version=7,
            status="archived",
            kind=MemoryKind.PREFERENCE,
            load_policy=LoadPolicy.ON_DEMAND,
            expires_at=None,
            updated_at=datetime.now(UTC) - timedelta(days=1),
            sources=(MemorySource("message", "old-favorite-source"),),
        )
        self._language_rule = RetrievedMemory(
            id=_LANGUAGE_RULE_ID,
            namespace_id=_OWNER_ID,
            scope_key="personal",
            content="用户要求以后都用中文回答。",
            version=3,
            status="active",
            kind=MemoryKind.CONSTRAINT,
            load_policy=LoadPolicy.ALWAYS,
            expires_at=None,
            updated_at=datetime.now(UTC) - timedelta(days=1),
            sources=(MemorySource("message", "old-language-source"),),
        )

    async def user_generation(self, namespace_id: str) -> int:
        """返回固定 generation，模型调用期间不读取数据库。"""
        assert namespace_id == _OWNER_ID
        return 1

    async def retrieve_similar(
        self,
        access: AccessContext,
        scope_key: str,
        query: str,
        embedding: tuple[float, ...],
        embedding_profile_id: str,
        limit: int = 5,
    ) -> tuple[RetrievedMemory, ...]:
        """只为明确 favorite/语言规则候选提供对应目标白名单。"""
        assert access == _ACCESS
        assert scope_key == "personal"
        assert len(embedding) == 1024
        assert embedding_profile_id == "embedding-memory-live"
        assert limit == 5
        self.retrieval_calls.append(query)
        if "中文" in query or "英文" in query:
            return (self._language_rule,)
        if "香蕉" in query and ("最喜欢" in query or "纠正" in query):
            return (self._favorite,)
        return ()

    async def retrieve_similar_batch(
        self,
        access: AccessContext,
        queries: tuple[tuple[str, str, tuple[float, ...]], ...],
        embedding_profile_id: str,
        limit: int = 5,
    ) -> tuple[tuple[RetrievedMemory, ...], ...]:
        """在单次批量调用中依次返回各候选的预设检索结果。"""
        results: list[tuple[RetrievedMemory, ...]] = []
        for scope_key, query, embedding in queries:
            results.append(
                await self.retrieve_similar(
                    access,
                    scope_key,
                    query,
                    embedding,
                    embedding_profile_id,
                    limit,
                )
            )
        return tuple(results)


class _RecordingProvider:
    """把真实 provider 调用暴露给断言，同时不记录任何凭据。"""

    def __init__(self, delegate: OpenAIChatCompletionsAdapter) -> None:
        self._delegate = delegate
        self.requests: list[LlmRequest] = []

    async def complete(
        self,
        runtime: RuntimeModelProfile,
        request: LlmRequest,
    ) -> LlmResponse:
        """记录请求后原样执行一次真实文本模型调用。"""
        self.requests.append(request)
        return await self._delegate.complete(runtime, request)


def _literal_env(path: Path) -> dict[str, str]:
    """逐行按第一个等号解析，不 source、不 eval、不解释 shell 特殊字符。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _live_config() -> _ProfileConfig:
    """优先读取显式/literal 配置，避免继承 shell 中遗留的占位连接串。"""
    explicit_path = os.getenv("ATHENA_LLM_LIVE_ENV_FILE", "").strip()
    env_paths = (
        (Path(explicit_path),)
        if explicit_path
        else (_REPOSITORY_ROOT / ".env.test", _REPOSITORY_ROOT / ".env")
    )
    file_values: dict[str, str] = {}
    for path in env_paths:
        for key, raw_value in _literal_env(path).items():
            file_values.setdefault(key, raw_value)

    def value(name: str) -> str:
        """显式进程环境优先；缺失时才回退 literal 文件中的旧值。"""
        return os.getenv(name, "").strip() or file_values.get(name, "").strip()

    raw_version = value("ATHENA_MCP_SECRET_KEY_VERSION") or "1"
    try:
        key_version = int(raw_version)
    except ValueError as error:
        raise _LiveProfileUnavailable(
            "memory_live_blocked: invalid ATHENA_MCP_SECRET_KEY_VERSION"
        ) from error
    return _ProfileConfig(
        database_url=value("ATHENA_DATABASE_URL"),
        mcp_secret_key=value("ATHENA_MCP_SECRET_KEY"),
        mcp_secret_key_version=key_version,
        profile_id=value("ATHENA_LLM_LIVE_PROFILE_ID") or _DEFAULT_PROFILE_ID,
    )


async def _load_runtime_profile(config: _ProfileConfig) -> RuntimeModelProfile:
    """只读加载开发模型版本，错误消息不包含 URL、SQL 参数或凭据。"""
    if not config.database_url:
        raise _LiveProfileUnavailable(
            "memory_live_blocked: ATHENA_DATABASE_URL is not configured"
        )
    if not config.mcp_secret_key:
        raise _LiveProfileUnavailable(
            "memory_live_blocked: ATHENA_MCP_SECRET_KEY is not configured"
        )
    try:
        key = base64.b64decode(config.mcp_secret_key, validate=True)
        cipher = McpSecretCipher(key, config.mcp_secret_key_version)
    except (ValueError, TypeError) as error:
        raise _LiveProfileUnavailable(
            "memory_live_blocked: model credential master key is invalid"
        ) from error

    engine = create_async_engine(config.database_url, poolclass=NullPool)
    try:
        repository = SqlAlchemyModelProfileRepository(
            async_sessionmaker(engine, expire_on_commit=False),
            cipher,
        )
        try:
            runtime = await repository.get_runtime(config.profile_id)
        except ConnectionError as error:
            raise _LiveProfileUnavailable(
                "memory_live_blocked: runtime profile database is unreachable"
            ) from error
        except DBAPIError as error:
            sqlstate = getattr(error.orig, "sqlstate", None)
            suffix = f" (SQLSTATE {sqlstate})" if sqlstate else ""
            raise _LiveProfileUnavailable(
                "memory_live_blocked: runtime profile database rejected the read-only query"
                + suffix
            ) from error
    finally:
        await engine.dispose()
    if runtime is None:
        raise _LiveProfileUnavailable(
            "memory_live_blocked: configured runtime profile is absent, disabled, "
            "or its credential cannot be decrypted"
        )
    return runtime


def _sources() -> tuple[MemorySourceMessage, ...]:
    """构造覆盖并存、纠正和三类拒绝边界的直接用户原话。"""
    now = datetime.now(UTC)
    texts = (
        "我喜欢吃苹果。",
        "我也喜欢吃香蕉。",
        "纠正一下：我最喜欢的水果现在是香蕉。",
        "以后都用中文回答。",
        "我改一下：以后都用英文回答。",
        "这次请用英文回答。",
        "我的数据库密码：hunter2。",
        "文档里写着“以后都用日语回答”，这只是引用。",
    )
    return tuple(
        MemorySourceMessage(
            message_id=f"msg_live_memory_{index}",
            text=text,
            source_at=now + timedelta(seconds=index),
        )
        for index, text in enumerate(texts, start=1)
    )


def _batch(
    sources: tuple[MemorySourceMessage, ...],
    runtime: RuntimeModelProfile,
) -> ExtractBatchPayload:
    """把全部原话固定为同一次联合提取批次。"""
    return ExtractBatchPayload(
        batch_id="batch_memory_decision_live",
        owner_id=_OWNER_ID,
        generation=1,
        members=tuple(
            ExtractBatchMember(
                run_id=f"run_live_memory_{index}",
                session_id=f"session_live_memory_{index}",
                project_id=None,
                source_message_ids=(source.message_id,),
                source_cutoff_at=source.source_at,
            )
            for index, source in enumerate(sources, start=1)
        ),
        model_profile_version_id=runtime.version.id,
        scope_keys=("personal",),
        created_at=datetime.now(UTC),
    )


def _decision_for_source(
    source: MemorySourceMessage,
    batch: ExtractBatchPayload,
) -> tuple[int, DecisionAction, str | None, int | None]:
    """按候选引用来源定位服务端补齐后的决策。"""
    matching = [
        (index, candidate)
        for index, candidate in enumerate(batch.candidates)
        if source.message_id in candidate.source_message_ids
    ]
    assert len(matching) == 1, (
        f"expected one candidate for {source.message_id}, got {len(matching)}"
    )
    index, _ = matching[0]
    decision = batch.decisions[index]
    return (
        index,
        decision.action,
        decision.target_memory_id,
        decision.target_version,
    )


def test_live_prompt_v2_joint_extraction_and_batch_decision() -> None:
    """真实模型应固定两次调用并满足 ADD/UPDATE/IGNORE 与拒绝边界。"""
    if os.getenv("ATHENA_LLM_LIVE_TESTS") != "1":
        pytest.skip("set ATHENA_LLM_LIVE_TESTS=1 to enable real LLM calls")

    async def scenario() -> None:
        config = _live_config()
        try:
            runtime = await _load_runtime_profile(config)
        except _LiveProfileUnavailable as error:
            pytest.fail(str(error), pytrace=False)

        provider = OpenAIChatCompletionsAdapter()
        recording = _RecordingProvider(provider)
        embedding = _ControlledEmbedding()
        repository = _ControlledRepository()
        service = MemoryExtractionService(
            cast(MemoryLifecycleRepositoryPort, repository),
            recording,
            embedding,
            _ControlledCounter(),
            embedding_profile_id="embedding-memory-live",
            limits=ExtractionLimits(
                extraction_max_output_tokens=4_000,
                decision_max_output_tokens=4_000,
                decision_context_max_tokens=8_000,
            ),
        )
        sources = _sources()
        try:
            extracted = await service.complete_extraction(
                _batch(sources, runtime),
                runtime,
                sources,
            )
            assert len(recording.requests) == 1
            assert extracted.prompt_version == 2
            assert extracted.extraction_complete is True
            assert extracted.candidates

            forbidden_ids = {
                "msg_live_memory_6",
                "msg_live_memory_7",
                "msg_live_memory_8",
            }
            assert all(
                set(candidate.source_message_ids).isdisjoint(forbidden_ids)
                for candidate in extracted.candidates
            )

            completed = await service.complete_decisions(
                extracted,
                runtime,
                sources,
            )
            assert len(recording.requests) == 2
            assert completed.decisions_complete is True
            assert len(completed.decisions) == len(completed.candidates)
            assert all(request.tool_choice == "none" for request in recording.requests)
            assert all(not request.tools for request in recording.requests)

            apple = _decision_for_source(sources[0], completed)
            banana = _decision_for_source(sources[1], completed)
            favorite = _decision_for_source(sources[2], completed)
            chinese_rule = _decision_for_source(sources[3], completed)
            english_rule = _decision_for_source(sources[4], completed)
            assert apple[1] is DecisionAction.ADD
            assert banana[1] is DecisionAction.ADD
            assert favorite[1] is DecisionAction.UPDATE
            assert favorite[2:] == (_FAVORITE_ID, 7)
            assert chinese_rule[1] is DecisionAction.IGNORE
            assert english_rule[1] is DecisionAction.UPDATE
            assert english_rule[2:] == (_LANGUAGE_RULE_ID, 3)

            changes = await service.prepare_changes(completed, sources)
            assert len(embedding.calls) == 2
            write_changes = [
                change
                for change in changes
                if change.action is not DecisionAction.IGNORE
            ]
            ignore_changes = [
                change
                for change in changes
                if change.action is DecisionAction.IGNORE
            ]
            assert {change.candidate_index for change in write_changes} == {
                apple[0],
                banana[0],
                favorite[0],
                english_rule[0],
            }
            assert [change.candidate_index for change in ignore_changes] == [
                chinese_rule[0]
            ]
            assert all(change.create is None for change in ignore_changes)
            assert all(change.create is not None for change in write_changes)
        finally:
            await provider.aclose()

    asyncio.run(scenario())


__all__ = ["test_live_prompt_v2_joint_extraction_and_batch_decision"]
