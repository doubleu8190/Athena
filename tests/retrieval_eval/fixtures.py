"""Deterministic fixture pipeline for legacy retrieval evaluation.

It invokes the production ``HybridRetrievalManager`` with in-memory ports, so
the offline suite exercises the legacy rewrite, fusion, filtering, and top-k
logic without requiring network access or a local Chroma index.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Any

from langchain_core.messages import AIMessage

from athena.config.settings import Settings
from athena.core.files.runtime import FileIntelligenceRuntime
from athena.core.memory.retrieval import HybridRetrievalManager
from athena.core.retrieval.trace import RetrievalTrace
from athena.models.file import Attachment, AttachmentStatus, FileChunk
from tests.retrieval_eval.metrics import (
    EvaluationOutcome,
    RankedResult,
    RetrievalEvalCase,
)


@dataclass(frozen=True)
class FixtureItem:
    item_id: str
    content: str
    keywords: tuple[str, ...]
    locator: dict[str, Any]


FIXTURE_ITEMS = (
    FixtureItem(
        "fixture:preferred_frontend",
        "preferred_frontend: React",
        ("前端", "框架", "react", "frontend"),
        {},
    ),
    FixtureItem(
        "fixture:database_decision",
        "database migration: PostgreSQL was selected for the project",
        ("数据库", "postgresql", "迁移", "database"),
        {},
    ),
    FixtureItem(
        "fixture:error_e11000",
        "E11000 duplicate key error: resolve by checking the unique index",
        ("e11000", "duplicate", "唯一", "错误"),
        {"path": "logs/mongo.txt", "start_line": 18, "end_line": 18},
    ),
    FixtureItem(
        "fixture:api_user_service",
        "UserService.create_user validates the email before persistence",
        ("userservice", "create_user", "用户", "api"),
        {"path": "src/users/service.py", "start_line": 42, "end_line": 58},
    ),
    FixtureItem(
        "fixture:pdf_contract_risk",
        "Termination payment risk is described in section 4.2",
        ("合同", "风险", "termination", "payment"),
        {"page": 12},
    ),
    FixtureItem(
        "fixture:excel_revenue",
        "Revenue sheet: Q2 total revenue is 120000",
        ("收入", "revenue", "q2", "总和"),
        {"sheet": "Revenue", "start_row": 2, "end_row": 8},
    ),
    FixtureItem(
        "fixture:unrelated_database",
        "unrelated note: use SQLite for local development",
        ("sqlite", "本地"),
        {},
    ),
)

_ITEM_BY_ATTACHMENT = {
    "fixture:log": "fixture:error_e11000",
    "fixture:code": "fixture:api_user_service",
    "fixture:contract": "fixture:pdf_contract_risk",
    "fixture:revenue": "fixture:excel_revenue",
}
_ITEMS_BY_ID = {item.item_id: item for item in FIXTURE_ITEMS}


def _match_score(item: FixtureItem, query: str) -> float | None:
    normalized = query.lower()
    hits = sum(keyword in normalized for keyword in item.keywords)
    return 0.80 + min(hits, 3) * 0.05 if hits else None


class _EchoLLM:
    async def ainvoke(self, messages: list[Any]) -> AIMessage:
        prompt = str(messages[-1].content)
        return AIMessage(content=prompt.rsplit("原始查询：", 1)[-1].strip())


class _FixtureMemory:
    def _rank(self, query: str) -> list[dict[str, Any]]:
        normalized = query.lower()
        matches: list[tuple[float, FixtureItem]] = []
        for item in FIXTURE_ITEMS:
            score = _match_score(item, normalized)
            if score is not None:
                matches.append((score, item))
        matches.sort(key=lambda entry: (-entry[0], entry[1].item_id))
        now = datetime.now().isoformat()
        return [
            {
                "id": item.item_id,
                "content": item.content,
                "metadata": {"created_at": now, "access_count": 0},
                "score": score,
            }
            for score, item in matches
        ]

    async def search(
        self, query: str, n_results: int = 5, where: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return self._rank(query)[:n_results]

    async def keyword_search(
        self, query: str, n_results: int = 10, where: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return self._rank(query)[:n_results]

    def pending_access_stats(self, _ids: Any) -> dict[str, tuple[int, str]]:
        return {}


class _FixtureFileRepository:
    def __init__(self) -> None:
        now = datetime.now()
        self._attachments = {
            attachment_id: Attachment(
                id=attachment_id,
                session_id="fixture-session",
                filename=f"{attachment_id.rsplit(':', 1)[-1]}.txt",
                mime_type="text/plain",
                size_bytes=1,
                sha256="0" * 64,
                storage_key="fixture",
                adapter_name=(
                    "code" if attachment_id == "fixture:code"
                    else "pdf" if attachment_id == "fixture:contract"
                    else "excel" if attachment_id == "fixture:revenue"
                    else "text"
                ),
                status=AttachmentStatus.READY,
                created_at=now,
                updated_at=now,
            )
            for attachment_id in _ITEM_BY_ATTACHMENT
        }

    async def get_attachment(
        self, attachment_id: str, session_id: str | None = None
    ) -> Attachment | None:
        attachment = self._attachments.get(attachment_id)
        if attachment is None or (session_id is not None and attachment.session_id != session_id):
            return None
        return attachment

    async def search_chunks(
        self, attachment_id: str, query: str, limit: int = 10
    ) -> list[FileChunk]:
        item = _ITEMS_BY_ID[_ITEM_BY_ATTACHMENT[attachment_id]]
        if _match_score(item, query) is None:
            return []
        return [
            FileChunk(
                id=item.item_id,
                attachment_id=attachment_id,
                ordinal=0,
                content=item.content,
                locator=item.locator,
            )
        ][:limit]


class _FixtureCollection:
    def query(
        self,
        query_texts: list[str],
        n_results: int,
        where: dict[str, str],
        include: list[str],
    ) -> dict[str, list[list[Any]]]:
        attachment_id = where["attachment_id"]
        item = _ITEMS_BY_ID[_ITEM_BY_ATTACHMENT[attachment_id]]
        if _match_score(item, query_texts[0]) is None:
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}
        return {
            "ids": [[item.item_id]],
            "documents": [[item.content]],
            "metadatas": [[{"locator_json": json.dumps(item.locator)}]],
            "distances": [[0.2]],
        }


class _FixtureWS:
    async def send_to_session(self, _session_id: str, _event: dict[str, Any]) -> None:
        return None


class FixtureLegacyPipeline:
    """Runs deterministic fixture queries through current legacy memory ranking."""

    def __init__(self) -> None:
        self._traces: list[RetrievalTrace] = []
        settings = Settings(
            _env_file=None,
            debug=False,
            retrieval_trace_enabled=True,
            retrieval_trace_query_hash_salt="offline-fixture",
        )
        self._manager = HybridRetrievalManager(
            _EchoLLM(),
            _FixtureMemory(),
            settings,
            trace_sink=self._traces.append,
        )
        self._file_runtime = FileIntelligenceRuntime(
            _FixtureFileRepository(),
            _EchoLLM(),
            _EchoLLM(),
            settings=settings,
            ws_manager=_FixtureWS(),
            trace_sink=self._traces.append,
        )
        self._file_runtime._collection = _FixtureCollection()  # fixture-only port
        self._locators = {item.item_id: item.locator for item in FIXTURE_ITEMS}

    async def evaluate(self, case: RetrievalEvalCase) -> EvaluationOutcome:
        started = perf_counter()
        traces_before = len(self._traces)
        if case.source == "file":
            final_ids: list[str] = []
            for attachment_id in case.attachment_ids:
                response = await self._file_runtime.search_file(
                    "fixture-session", attachment_id, case.query
                )
                final_ids.extend(item["id"] for item in response["results"])
            results = None
        else:
            results = await self._manager.retrieve(case.query)
            final_ids = [result.chunk_id for result in results]
        traces = self._traces[traces_before:]
        candidate_ids = list(
            dict.fromkeys(
                item.item_id for trace in traces for item in trace.candidates
            )
        )
        stage_durations: dict[str, float] = {}
        for trace in traces:
            for stage in trace.stages:
                stage_durations[stage.stage] = (
                    stage_durations.get(stage.stage, 0.0) + stage.duration_ms
                )
        return EvaluationOutcome(
            case_id=case.case_id,
            candidates=tuple(
                RankedResult(item_id=item_id, locator=self._locators.get(item_id, {}))
                for item_id in candidate_ids
            ),
            final_results=tuple(
                RankedResult(
                    item_id=item_id,
                    locator=self._locators.get(item_id, {}),
                )
                for item_id in dict.fromkeys(final_ids)
            ),
            stage_durations_ms=stage_durations,
            llm_calls=sum(
                stage.stage == "query_expand"
                for trace in traces
                for stage in trace.stages
            ),
            total_duration_ms=(perf_counter() - started) * 1000,
        )
