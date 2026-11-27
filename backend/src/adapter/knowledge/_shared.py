"""知识库 SQLAlchemy 仓储共享常量、行锁辅助、可见性查询与领域转换函数。"""

from __future__ import annotations

import random
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Integer,
    String,
    Text,
    bindparam,
    delete,
    exists,
    func,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.sql import Select

from adapter.access.repository import load_grants_map_in_session
from adapter.knowledge.model import (
    KnowledgeBaseModel,
    KnowledgeChunkModel,
    KnowledgeDocumentModel,
    KnowledgeJobModel,
    KnowledgeRequestModel,
    KnowledgeSourceModel,
    KnowledgeVersionModel,
)
from common.date import as_utc
from domain.access import ResourceType, VisibilityGrant, VisibilityScope
from domain.knowledge.model import (
    ChunkLocator,
    Document,
    DocumentState,
    DocumentVersion,
    KnowledgeBase,
    SearchHit,
)

PUBLISH_CHUNK_BATCH_SIZE = 200
MAX_KEYWORD_TOKENS = 20

# 仅在 search_keyword 构造 tsquery 前过滤的高频结构中英文停用词：
# 严格避开否定词（不/未/无/非/no/not/nor）与单字母绩效等级（s/b/c），防止改变业务语义。
_KEYWORD_STOPWORDS: frozenset[str] = frozenset(
    {
        # 中文高频结构助词、连词、疑问词与语气词
        "的",
        "了",
        "在",
        "是",
        "和",
        "与",
        "及",
        "或",
        "也",
        "都",
        "就",
        "中",
        "里",
        "着",
        "过",
        "把",
        "被",
        "对",
        "从",
        "到",
        "为",
        "之",
        "其",
        "这",
        "那",
        "什么",
        "怎么",
        "怎样",
        "如何",
        "多少",
        "是否",
        "分别",
        "还是",
        "请问",
        "哪些",
        "哪个",
        "哪里",
        "为什么",
        "一下",
        "吗",
        "呢",
        "啊",
        "吧",
        "呀",
        # 英文高频功能词与疑问词（保留否定词与单字母等级）
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "of",
        "to",
        "in",
        "for",
        "on",
        "with",
        "at",
        "by",
        "from",
        "as",
        "and",
        "or",
        "if",
        "that",
        "this",
        "these",
        "those",
        "it",
        "its",
        "what",
        "which",
        "who",
        "whom",
        "where",
        "when",
        "why",
        "how",
        "do",
        "does",
        "did",
        "can",
        "could",
        "should",
        "would",
        "will",
        "please",
        "about",
        "into",
    }
)

# 识别高区分度复合标识符：政策编号/单号、多段版本号、邮箱、含连接符的复合符号与 C++/C# 类词元。
_PRIORITY_IDENTIFIER_RE = re.compile(
    r"^(?:"
    r"(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9_.:/@+-]+"
    r"|\d+(?:[._:/-]\d+)+"
    r"|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    r"|[A-Za-z0-9]+(?:[_.:/+-]+[A-Za-z0-9]+)+"
    r"|[A-Za-z][A-Za-z0-9]*[+#-]+"
    r")$"
)

# PostgreSQL 批量写入 knowledge_chunks：仅绑定原生 TEXT[]/INTEGER[] 参数以兼容 asyncpg，
# 在数据库端完成 jsonb / tsvector / vector(1024) 转换，将 N 次串行往返压减为每 200 条 1 次 RTT。
_PG_BATCH_INSERT_CHUNKS_SQL = text(
    """
    INSERT INTO knowledge_chunks (
        id,
        document_id,
        version_id,
        ordinal,
        content,
        locator,
        token_count,
        search_document,
        embedding
    )
    SELECT
        t.id,
        :document_id,
        :version_id,
        t.ordinal,
        t.content,
        CAST(t.locator_json AS jsonb),
        t.token_count,
        to_tsvector('simple', t.search_text),
        CAST(t.embedding_text AS vector(1024))
    FROM unnest(
        :ids,
        :ordinals,
        :contents,
        :locators,
        :token_counts,
        :search_texts,
        :embeddings
    ) AS t(id, ordinal, content, locator_json, token_count, search_text, embedding_text)
    """
).bindparams(
    bindparam("document_id", type_=String),
    bindparam("version_id", type_=String),
    bindparam("ids", type_=ARRAY(Text)),
    bindparam("ordinals", type_=ARRAY(Integer)),
    bindparam("contents", type_=ARRAY(Text)),
    bindparam("locators", type_=ARRAY(Text)),
    bindparam("token_counts", type_=ARRAY(Integer)),
    bindparam("search_texts", type_=ARRAY(Text)),
    bindparam("embeddings", type_=ARRAY(Text)),
)


@dataclass(frozen=True, slots=True)
class _FileSourceInput:
    """文件原件持久化参数，供统一文档创建与替换事务使用。"""

    source_id: str
    filename: str
    media_type: str
    storage_key: str
    size_bytes: int
    sha256: str


def _personal_privacy_guard(actor_id: str) -> ColumnElement[bool]:
    """个人私有知识库始终强制 owner_id 纵深防御；公司/部门库由上层权限引擎鉴权。"""
    return or_(
        KnowledgeBaseModel.visibility_scope != VisibilityScope.PERSONAL.value,
        KnowledgeBaseModel.owner_id == actor_id,
    )


def _lease_expiry_expr(lease_seconds: int) -> Any:
    """生成基于数据库时钟的租约截止时间表达式。"""
    return func.clock_timestamp() + text(f"INTERVAL '{lease_seconds} seconds'")


def _claimable_job_clauses() -> tuple[ColumnElement[bool], ...]:
    """返回可认领（待执行或租约已过期）的入库/清理任务公共过滤条件。"""
    return (
        KnowledgeJobModel.kind.in_(("ingestion", "cleanup")),
        KnowledgeJobModel.state.in_(("queued", "retry", "processing")),
        KnowledgeJobModel.next_attempt_at <= func.clock_timestamp(),
        KnowledgeJobModel.lease_until.is_(None)
        | (KnowledgeJobModel.lease_until < func.clock_timestamp()),
    )


def _activate_job_lease(job: KnowledgeJobModel, lease_seconds: int) -> None:
    """将已锁定的任务推进为 processing，并递增尝试次数与 fencing token。"""
    job.state = "processing"
    job.attempt += 1
    job.fencing_token += 1
    job.lease_until = _lease_expiry_expr(lease_seconds)
    job.updated_at = func.clock_timestamp()


def _is_active_job_lease(
    task: KnowledgeJobModel | None,
    fencing_token: int,
    now: datetime,
) -> bool:
    """校验任务仍处于当前 worker 持有的未过期 processing 租约。"""
    return (
        task is not None
        and task.state == "processing"
        and task.fencing_token == fencing_token
        and task.lease_until is not None
        and task.lease_until > now
    )


def _new_queued_job(
    *,
    job_id: str,
    owner_id: str,
    kind: str,
    target_id: str,
    at: Any,
) -> KnowledgeJobModel:
    """构造初始入队状态的知识库后台任务行。"""
    return KnowledgeJobModel(
        id=job_id,
        owner_id=owner_id,
        kind=kind,
        target_id=target_id,
        state="queued",
        attempt=0,
        fencing_token=0,
        next_attempt_at=at,
        created_at=at,
        updated_at=at,
    )


async def _lock_base(
    session: AsyncSession,
    base_id: str,
    *,
    owner_id: str | None = None,
    active_only: bool = True,
) -> KnowledgeBaseModel | None:
    """以 FOR UPDATE 锁定知识库行，按需附带个人私有纵深防御与未删除过滤。"""
    conditions: list[ColumnElement[bool]] = [KnowledgeBaseModel.id == base_id]
    if owner_id is not None:
        conditions.append(_personal_privacy_guard(owner_id))
    if active_only:
        conditions.append(KnowledgeBaseModel.deleted_time.is_(None))
    return await session.scalar(select(KnowledgeBaseModel).where(*conditions).with_for_update())


async def _lock_document(
    session: AsyncSession,
    document_id: str,
    *,
    base_id: str | None = None,
    active_only: bool = True,
) -> KnowledgeDocumentModel | None:
    """以 FOR UPDATE 锁定知识库文档行，按需限定所属知识库与未删除状态。"""
    conditions: list[ColumnElement[bool]] = [KnowledgeDocumentModel.id == document_id]
    if base_id is not None:
        conditions.append(KnowledgeDocumentModel.knowledge_base_id == base_id)
    if active_only:
        conditions.append(KnowledgeDocumentModel.deleted_time.is_(None))
    return await session.scalar(select(KnowledgeDocumentModel).where(*conditions).with_for_update())


async def _lock_job(session: AsyncSession, job_id: str) -> KnowledgeJobModel | None:
    """以 FOR UPDATE 锁定后台任务行。"""
    return await session.scalar(
        select(KnowledgeJobModel).where(KnowledgeJobModel.id == job_id).with_for_update()
    )


async def _load_base_grants(
    session: AsyncSession,
    base_id: str,
) -> tuple[VisibilityGrant, ...]:
    """在当前数据库会话中加载单个知识库的可见性授权列表。"""
    grants_map = await load_grants_map_in_session(session, ResourceType.KNOWLEDGE_BASE, (base_id,))
    return grants_map.get(base_id, ())


async def _delete_document_versions_and_requests(
    session: AsyncSession,
    document_ids: Sequence[str],
    *,
    exclude_job_id: str,
    include_doc_cleanup_jobs: bool = False,
) -> list[str]:
    """清理目标文档集合关联的版本任务、版本记录与幂等请求，并返回待回收原件 ID 列表。"""
    if not document_ids:
        return []
    version_rows = (
        await session.execute(
            select(KnowledgeVersionModel.id, KnowledgeVersionModel.source_id).where(
                KnowledgeVersionModel.document_id.in_(document_ids),
            )
        )
    ).all()
    version_ids = [row[0] for row in version_rows]
    candidate_source_ids = [row[1] for row in version_rows if row[1] is not None]
    if version_ids:
        await session.execute(
            delete(KnowledgeJobModel).where(
                KnowledgeJobModel.id != exclude_job_id,
                KnowledgeJobModel.target_id.in_(version_ids),
            )
        )
    if include_doc_cleanup_jobs:
        await session.execute(
            delete(KnowledgeJobModel).where(
                KnowledgeJobModel.id != exclude_job_id,
                KnowledgeJobModel.kind == "cleanup",
                KnowledgeJobModel.target_id.in_([f"doc:{item}" for item in document_ids]),
            )
        )
    await session.execute(
        delete(KnowledgeVersionModel).where(KnowledgeVersionModel.document_id.in_(document_ids))
    )
    await session.execute(
        delete(KnowledgeRequestModel).where(KnowledgeRequestModel.document_id.in_(document_ids))
    )
    return candidate_source_ids


async def _delete_unused_sources(
    session: AsyncSession,
    candidate_source_ids: Sequence[str],
) -> None:
    """仅回收不再被任何版本引用的原件记录（跨管理员删除同样安全生效）。"""
    if not candidate_source_ids:
        return
    unused_sources = list(
        await session.scalars(
            select(KnowledgeSourceModel.id).where(
                KnowledgeSourceModel.id.in_(candidate_source_ids),
                ~exists(
                    select(KnowledgeVersionModel.id).where(
                        KnowledgeVersionModel.source_id == KnowledgeSourceModel.id,
                    )
                ),
            )
        )
    )
    if unused_sources:
        await session.execute(
            delete(KnowledgeSourceModel).where(KnowledgeSourceModel.id.in_(unused_sources))
        )


def _visible_documents_query(owner_id: str, base_id: str) -> Select[KnowledgeDocumentModel]:
    """构造未删除知识库下的可见未删除文档基础查询。"""
    return (
        select(KnowledgeDocumentModel)
        .join(
            KnowledgeBaseModel,
            KnowledgeBaseModel.id == KnowledgeDocumentModel.knowledge_base_id,
        )
        .where(
            KnowledgeDocumentModel.knowledge_base_id == base_id,
            KnowledgeDocumentModel.deleted_time.is_(None),
            KnowledgeBaseModel.deleted_time.is_(None),
            _personal_privacy_guard(owner_id),
        )
    )


def _base(
    model: KnowledgeBaseModel,
    *,
    grants: tuple[VisibilityGrant, ...] = (),
) -> KnowledgeBase:
    """将知识库 ORM 行转换为领域实体。"""
    return KnowledgeBase(
        model.id,
        model.owner_id,
        model.name,
        model.description,
        model.created_at,
        model.updated_at,
        model.deleted_time,
        visibility_scope=VisibilityScope(model.visibility_scope),
        owner_department_id=model.owner_department_id,
        visibility_grants=grants,
    )


def _document(
    model: KnowledgeDocumentModel,
    *,
    latest_version: DocumentVersion | None = None,
) -> Document:
    """将知识库文档 ORM 行转换为领域实体。"""
    return Document(
        model.id,
        model.owner_id,
        model.knowledge_base_id,
        model.title,
        model.active_version_id,
        model.pending_version_id,
        model.created_at,
        model.updated_at,
        model.deleted_time,
        latest_version=latest_version,
    )


async def _load_latest_versions(
    session: AsyncSession, document_ids: Sequence[str]
) -> dict[str, DocumentVersion]:
    """用单次批量窗口查询为给定文档集合取回各自最新版本。"""
    unique_ids = tuple(dict.fromkeys(document_ids))
    if not unique_ids:
        return {}
    # 按 document_id 分区并按 created_at DESC, id DESC 排序取首条，
    # 与 list_versions 顺序 100% 一致，不按版本上传者过滤以支持跨管理员协作。
    ranked_versions = (
        select(
            KnowledgeVersionModel.id.label("version_id"),
            func.row_number()
            .over(
                partition_by=KnowledgeVersionModel.document_id,
                order_by=(
                    KnowledgeVersionModel.created_at.desc(),
                    KnowledgeVersionModel.id.desc(),
                ),
            )
            .label("row_num"),
        )
        .where(
            KnowledgeVersionModel.document_id.in_(unique_ids),
        )
        .subquery()
    )
    rows = await session.scalars(
        select(KnowledgeVersionModel).join(
            ranked_versions,
            (KnowledgeVersionModel.id == ranked_versions.c.version_id)
            & (ranked_versions.c.row_num == 1),
        )
    )
    return {row.document_id: _version(row) for row in rows}


def _version(model: KnowledgeVersionModel) -> DocumentVersion:
    """将版本 ORM 行转换为领域对象。"""
    return DocumentVersion(
        id=model.id,
        document_id=model.document_id,
        state=DocumentState(model.state),
        error_code=model.error_code,
        warnings=tuple(model.warnings),
        chunk_count=model.chunk_count,
        created_at=model.created_at,
        completed_at=model.completed_at,
    )


def _locator(value: dict[str, object]) -> ChunkLocator:
    """将 JSONB 定位器字典解析为 ChunkLocator 值对象。"""
    return ChunkLocator(
        kind=str(value.get("kind", "text")),
        page=_optional_int(value.get("page")),
        start_line=_optional_int(value.get("start_line")),
        end_line=_optional_int(value.get("end_line")),
        heading=_optional_str(value.get("heading")),
        sheet=_optional_str(value.get("sheet")),
        cell_range=_optional_str(value.get("cell_range")),
        slide=_optional_int(value.get("slide")),
        shape=_optional_str(value.get("shape")),
        media_id=_optional_str(value.get("media_id")),
    )


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _tsquery_token(token: str) -> str:
    """转义受控 token 中的反斜杠与单引号，避免用户输入破坏 tsquery 词法或被解释为运算符。"""
    escaped = token.replace("\\", "\\\\").replace("'", "''")
    return "'" + escaped + "'"


def _format_pgvector_literal(vector: Sequence[float] | None) -> str | None:
    """将浮点向量序列化为 pgvector 文本字面量 `[v0,v1,...]`，空向量返回 None。"""
    if vector is None:
        return None
    return "[" + ",".join(str(float(value)) for value in vector) + "]"


def _is_priority_identifier(token: str) -> bool:
    """判断词元是否为编号、版本号、邮箱或复合标识符，截断时优先保留。"""
    return _PRIORITY_IDENTIFIER_RE.fullmatch(token) is not None


def _select_keyword_tokens(tokens: tuple[str, ...]) -> tuple[str, ...]:
    """在构造 tsquery 前过滤纯标点与高频结构停用词，并在超限时优先保留尾部复合标识符。"""
    deduped = tuple(token for token in dict.fromkeys(raw.strip() for raw in tokens) if token)
    if not deduped:
        return ()
    filtered = [
        token
        for token in deduped
        if any(ch.isalnum() for ch in token) and token.lower() not in _KEYWORD_STOPWORDS
    ]
    if not filtered:
        # 全部词元均被过滤（例如纯停用词或纯符号查询）时，回退原始去重非空词集兜底。
        return deduped[:MAX_KEYWORD_TOKENS]
    if len(filtered) <= MAX_KEYWORD_TOKENS:
        return tuple(filtered)
    # 分词器会把正则提取的政策编号/单号/邮箱/版本号追加在列表尾部，
    # 超过 20 词时必须优先保留复合标识符，再按原顺序补齐其余实词。
    priority = [token for token in filtered if _is_priority_identifier(token)]
    if len(priority) >= MAX_KEYWORD_TOKENS:
        return tuple(priority[:MAX_KEYWORD_TOKENS])
    remaining_budget = MAX_KEYWORD_TOKENS - len(priority)
    priority_set = set(priority)
    others = [token for token in filtered if token not in priority_set][:remaining_budget]
    selected = priority_set | set(others)
    return tuple(token for token in filtered if token in selected)


def _join_active_chunks[*Ts](query: Select[*Ts]) -> Select[*Ts]:
    """为分片查询挂载 版本 → 文档 → 知识库 三表联结。"""
    return (
        query.join(
            KnowledgeVersionModel,
            KnowledgeVersionModel.id == KnowledgeChunkModel.version_id,
        )
        .join(KnowledgeDocumentModel, KnowledgeDocumentModel.id == KnowledgeChunkModel.document_id)
        .join(KnowledgeBaseModel, KnowledgeBaseModel.id == KnowledgeDocumentModel.knowledge_base_id)
    )


def _active_chunk_clauses(
    owner_id: str,
    *,
    base_ids: list[str] | None = None,
) -> tuple[ColumnElement[bool], ...]:
    """返回可见且处于已发布活动版本的分片过滤条件。"""
    clauses: list[ColumnElement[bool]] = []
    if base_ids is not None:
        clauses.append(KnowledgeDocumentModel.knowledge_base_id.in_(base_ids))
    clauses.extend(
        (
            KnowledgeDocumentModel.deleted_time.is_(None),
            KnowledgeBaseModel.deleted_time.is_(None),
            _personal_privacy_guard(owner_id),
            KnowledgeDocumentModel.active_version_id == KnowledgeVersionModel.id,
            KnowledgeVersionModel.state == "ready",
        )
    )
    return tuple(clauses)


def _visible_chunk_rows_query(
    owner_id: str, base_ids: list[str]
) -> Select[str, str, str, str, dict[str, object], int, str, str]:
    """构造显式标量列投影的可见分片查询，彻底省去 1024 维 embedding 与 search_document 大列。"""
    return _join_active_chunks(
        select(
            KnowledgeChunkModel.id,
            KnowledgeChunkModel.document_id,
            KnowledgeChunkModel.version_id,
            KnowledgeChunkModel.content,
            KnowledgeChunkModel.locator,
            KnowledgeChunkModel.ordinal,
            KnowledgeDocumentModel.title,
            KnowledgeDocumentModel.knowledge_base_id,
        )
    ).where(*_active_chunk_clauses(owner_id, base_ids=base_ids))


def _visible_chunks_query(
    owner_id: str, base_ids: list[str]
) -> Select[KnowledgeChunkModel, str, str]:
    """保留原有 3 元组 ORM 投影签名以兼容测试导入，并通过 load_only 跳过大列加载。"""
    return _join_active_chunks(
        select(
            KnowledgeChunkModel,
            KnowledgeDocumentModel.title,
            KnowledgeDocumentModel.knowledge_base_id,
        ).options(
            load_only(
                KnowledgeChunkModel.id,
                KnowledgeChunkModel.document_id,
                KnowledgeChunkModel.version_id,
                KnowledgeChunkModel.ordinal,
                KnowledgeChunkModel.content,
                KnowledgeChunkModel.locator,
            )
        )
    ).where(*_active_chunk_clauses(owner_id, base_ids=base_ids))


def _rows_to_search_hits(
    rows: Iterable[Row[str, str, str, str, dict[str, object], int, str, str]],
    channel: str,
) -> tuple[SearchHit, ...]:
    """将关键词或语义召回的 8 列投影结果按排名转换为 SearchHit 元组。"""
    return tuple(
        _search_hit_from_row(
            chunk_id,
            document_id,
            version_id,
            content,
            locator,
            ordinal,
            title,
            base_id,
            channel,
            position,
        )
        for position, (
            chunk_id,
            document_id,
            version_id,
            content,
            locator,
            ordinal,
            title,
            base_id,
        ) in enumerate(rows, start=1)
    )


def _search_hit_from_row(
    chunk_id: str,
    document_id: str,
    version_id: str,
    content: str,
    locator: dict[str, object],
    ordinal: int,
    title: str,
    base_id: str,
    channel: str,
    position: int,
) -> SearchHit:
    return SearchHit(
        chunk_id=chunk_id,
        document_id=document_id,
        version_id=version_id,
        title=title,
        text=content,
        locators=(_locator(locator),),
        channels=(channel,),
        rank=float(position),
        chunk_ordinal=ordinal,
        chunk_ids=(chunk_id,),
        knowledge_base_id=base_id,
    )


async def _next_version_time(
    session: AsyncSession, document_id: str, requested: datetime
) -> datetime:
    """同一文档内把并发或固定时钟下的版本创建时间严格递增。"""
    latest = await session.scalar(
        select(func.max(KnowledgeVersionModel.created_at)).where(
            KnowledgeVersionModel.document_id == document_id,
        )
    )
    normalized_requested = as_utc(requested)
    if latest is not None:
        # PostgreSQL TIMESTAMPTZ 统一归约至 UTC 后再比较，确保同一文档版本时间严格递增。
        normalized_latest = as_utc(latest)
        if normalized_latest >= normalized_requested:
            return normalized_latest + timedelta(microseconds=1)
    return normalized_requested


def _retry_delay_seconds(attempt: int, jitter_factor: float | None = None) -> float:
    """返回5/30/120秒指数退避，并乘以±20%随机抖动。"""
    if attempt < 1:
        raise ValueError("attempt_invalid")
    base = (5, 30, 120)[min(attempt - 1, 2)]
    factor = random.SystemRandom().uniform(0.8, 1.2) if jitter_factor is None else jitter_factor
    if not 0.8 <= factor <= 1.2:
        raise ValueError("jitter_factor_invalid")
    return base * factor
