"""与生产环境对齐、使用隔离工作区的评估运行时。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

from athena.config.settings import Settings
from athena.core.files.runtime import FileIntelligenceRuntime
from athena.core.llm.provider import LLMProvider
from athena.core.memory.memory import MemoryManager
from athena.core.memory.retrieval import HybridRetrievalManager
from athena.core.retrieval.trace import RetrievalTrace
from athena.evaluation.bindings import BindingIndex
from athena.evaluation.dataset import load_cases, load_manifest
from athena.evaluation.ingestion import ingest_dataset
from athena.evaluation.models import (
    EvalCase,
    RankedResult,
    RetrievalContext,
    RetrievalOutcome,
)
from athena.evaluation.readiness import ReadinessReport, check_bindings
from athena.evaluation.snapshot import EvaluationWorkspace, SnapshotManifest, build_snapshot_manifest
from athena.gateway.ws.manager import WebSocketManager
from athena.infrastructure.chroma.memory_store import ChromaMemoryStore
from athena.infrastructure.sqlite.database import Database
from athena.infrastructure.sqlite.memory_repository import SqliteMemoryRepository


@dataclass
class RuntimePreparation:
    """评估运行时准备阶段生成的快照、绑定和就绪结果。"""

    manifest: Any
    bindings: BindingIndex
    readiness: ReadinessReport


class EvaluationRuntime:
    """持有一组隔离的数据库和索引，并提供只读检索。"""

    def __init__(
        self,
        *,
        settings: Settings,
        primary_llm: LLMProvider,
        secondary_llm: LLMProvider | None = None,
        workspace: Path,
    ) -> None:
        """创建使用隔离数据目录的评估运行时。

        参数：
            settings: 生产配置基线；路径会被替换为工作区路径。
            primary_llm: 主 LLM 提供商。
            secondary_llm: 检索使用的副提供商；省略时使用主提供商。
            workspace: 当前评估工作区目录。
        """
        self.production_settings = settings
        self.settings = settings
        self.primary_llm = primary_llm
        self.secondary_llm = secondary_llm or primary_llm
        self.workspace = EvaluationWorkspace(workspace, settings)
        self.database: Database | None = None
        self.memory_manager: MemoryManager | None = None
        self.memory_retrieval: HybridRetrievalManager | None = None
        self.file_runtime: FileIntelligenceRuntime | None = None
        self.bindings = BindingIndex()
        self.dataset_manifest: Any = None
        self.snapshot_manifest: Any = None
        self._trace_records: list[RetrievalTrace] = []

    async def prepare(self, dataset: Path) -> RuntimePreparation:
        """初始化数据库、索引和文件服务，并导入数据集。

        参数：
            dataset: 包含清单、用例和语料的评估数据集目录。

        返回值：
            快照清单、别名绑定和资源就绪状态。

        异常：
            RuntimeError: 工作区与生产路径冲突或资源未就绪。
            ValueError: 数据集清单或用例格式非法。
            Exception: 底层数据库、解析器或索引初始化失败。
        """
        # 打开任何数据库前，先解析所有相对于生产环境的路径。
        self.workspace.assert_root_isolated(self.production_settings)
        self.workspace.settings = self.workspace.isolated_settings().model_copy(
            update={
                "retrieval_trace_enabled": True,
                "retrieval_trace_include_raw_query": False,
            }
        )
        try:
            self.workspace.create()
            self.settings = self.workspace.settings
            self.database = Database(str(self.workspace.db_path))
            await self.database.connect()
            self.memory_manager = MemoryManager(
                self.settings,
                SqliteMemoryRepository(),
                ChromaMemoryStore(path=str(self.workspace.chroma_path)),
            )
            await self.memory_manager.initialize()
            self.file_runtime = FileIntelligenceRuntime(
                self.database.files,
                self.primary_llm,
                self.secondary_llm,
                settings=self.settings,
                ws_manager=WebSocketManager(),
                trace_sink=self._trace_sink,
            )
            await self.file_runtime.initialize()
            self.memory_retrieval = HybridRetrievalManager(
                self.secondary_llm,
                self.memory_manager,
                self.settings,
                trace_sink=self._trace_sink,
            )
            self.dataset_manifest = load_manifest(dataset / "manifest.json")
            cases = load_cases(dataset / "cases.jsonl", manifest=self.dataset_manifest)
            existing_manifest_path = self.workspace.root / "snapshot-manifest.json"
            existing_bindings_path = self.workspace.root / "bindings.json"
            if existing_manifest_path.exists() or existing_bindings_path.exists():
                if not existing_manifest_path.exists() or not existing_bindings_path.exists():
                    raise RuntimeError("evaluation workspace has an incomplete snapshot")
                existing = SnapshotManifest.read(existing_manifest_path)
                expected_hash = self.dataset_manifest.content_manifest_sha256
                if (
                    existing.dataset_id != self.dataset_manifest.dataset_id
                    or existing.dataset_version != self.dataset_manifest.version
                    or existing.dataset_content_manifest_sha256 != expected_hash
                    or existing.settings_hash != self._settings_hash()
                ):
                    raise RuntimeError(
                        "evaluation workspace snapshot inputs differ; use a new workspace"
                    )
                self.bindings = BindingIndex.read(existing_bindings_path)
                self.snapshot_manifest = existing
                readiness = check_bindings(
                    cases,
                    self.bindings,
                    attachments_ready=True,
                    sqlite_ready=self.workspace.db_path.exists(),
                    chroma_ready=self.workspace.chroma_path.exists(),
                )
                readiness.require_ready()
                return RuntimePreparation(existing, self.bindings, readiness)
            self.bindings = await ingest_dataset(
                dataset_root=dataset,
                manifest=self.dataset_manifest,
                workspace=self.workspace,
                database=self.database,
                memory_manager=self.memory_manager,
                file_runtime=self.file_runtime,
            )
            self.snapshot_manifest = build_snapshot_manifest(
                run_id=self.workspace.root.name,
                dataset_id=self.dataset_manifest.dataset_id,
                dataset_version=self.dataset_manifest.version,
                dataset_content_manifest_sha256=self.dataset_manifest.content_manifest_sha256,
                settings=self.settings,
                workspace=self.workspace,
                embedding_model="chroma-default",
                llm_model=(
                    self.settings.secondary_llm.model
                    if self.settings.secondary_llm
                    else self.settings.primary_llm.model
                ),
                parser_version="runtime",
                application_commit="unknown",
            )
            self.snapshot_manifest.write(self.workspace.root / "snapshot-manifest.json")
            readiness = check_bindings(
                cases,
                self.bindings,
                attachments_ready=self._attachments_ready(),
                sqlite_ready=True,
                chroma_ready=self.file_runtime.vector_index_ready,
            )
            readiness.require_ready()
            return RuntimePreparation(self.snapshot_manifest, self.bindings, readiness)
        except Exception:
            await self.close()
            raise

    def _settings_hash(self) -> str:
        """返回当前隔离设置的稳定摘要。"""
        from athena.evaluation.snapshot import settings_hash

        return settings_hash(self.settings)

    def _attachments_ready(self) -> bool:
        """检查评估会话内所有附件是否处于 READY 状态。"""
        # Attachment 状态在摄取阶段已校验；该轻量检查由运行时成功构建完成时代表。
        return self.file_runtime is not None

    async def retrieve(self, case: EvalCase) -> RetrievalOutcome:
        """在隔离快照中执行单个只读检索用例。

        参数：
            case: 待执行的评估用例。

        返回值：
            包含候选结果、最终结果、耗时和错误码的检索结果。

        异常：
            RuntimeError: ``prepare`` 尚未成功完成。
            Exception: 底层记忆或文件检索失败。
        """
        if self.memory_retrieval is None or self.file_runtime is None:
            raise RuntimeError("evaluation runtime has not been prepared")
        started = perf_counter()
        trace_start = len(self._trace_records)
        context = RetrievalContext(
            evaluation_run_id=self.snapshot_manifest.run_id,
            evaluation_case_id=case.case_id,
        )
        source = case.scope.get("source")
        results: list[RankedResult] = []
        trace: dict[str, Any] = {}
        if source == "memory":
            found = await self.memory_retrieval.retrieve(
                case.query,
                record_access=False,
            )
            aliases = tuple(
                case.scope.get("memory_aliases", case.scope.get("memory_alias", ()))
            )
            if isinstance(aliases, str):
                aliases = (aliases,)
            reverse = {
                memory_id: alias
                for alias, memory_id in self.bindings.memory_aliases.items()
            }
            results = [
                RankedResult(
                    item_id=item.chunk_id,
                    locator=item.metadata,
                    document_alias=reverse.get(item.chunk_id),
                    route=item.source,
                    rank=rank,
                    score=item.score,
                )
                for rank, item in enumerate(found, 1)
            ]
        elif source == "file":
            # 数据集别名有意不使用生产会话 ID。
            # 摄取过程会将所有快照附件放入此隔离会话。
            session_id = f"evaluation-{self.dataset_manifest.version}"
            aliases = case.scope.get("attachment_aliases", [])
            if len(aliases) != 1:
                return RetrievalOutcome(
                    case.case_id,
                    error_code="file_scope_requires_one_attachment",
                    total_duration_ms=(perf_counter() - started) * 1000,
                    retrieval_context=context,
                )
            file_id = self.bindings.attachment_aliases.get(aliases[0])
            if file_id is None:
                return RetrievalOutcome(
                    case.case_id,
                    error_code="attachment_binding_missing",
                    total_duration_ms=(perf_counter() - started) * 1000,
                    retrieval_context=context,
                )
            response = await self.file_runtime.search_file(
                session_id,
                file_id,
                case.query,
            )
            results = [
                RankedResult(
                    item_id=item["id"],
                    locator=item.get("locator", {}),
                    document_alias=aliases[0],
                    route="file",
                    rank=rank,
                    score=item.get("score"),
                )
                for rank, item in enumerate(response.get("results", []), 1)
            ]
        trace = self._trace_summary(trace_start)
        return RetrievalOutcome(
            case.case_id,
            candidates=tuple(results),
            final_results=tuple(results),
            stage_durations_ms=self._stage_durations(trace),
            total_duration_ms=(perf_counter() - started) * 1000,
            trace=trace,
            retrieval_context=context,
        )

    def _trace_sink(self, trace: RetrievalTrace) -> None:
        """收集当前评估运行的 trace，供对应 RetrievalOutcome 使用。"""
        self._trace_records.append(trace)

    def _trace_summary(self, start: int) -> dict[str, Any]:
        """返回单次 case 的安全 trace 摘要，不包含原始 Query 或正文。"""
        traces = self._trace_records[start:]
        return traces[-1].as_log_fields() if traces else {}

    @staticmethod
    def _stage_durations(trace: dict[str, Any]) -> dict[str, float]:
        """将 trace 阶段转换为评估报告使用的阶段耗时映射。"""
        return {
            f"{stage['stage']}:{stage['route']}": stage["duration_ms"]
            for stage in trace.get("stage_durations_ms", [])
        }

    async def close(self) -> None:
        """关闭数据库并释放评估工作区锁；重复调用安全。"""
        if self.database is not None:
            await self.database.close()
        self.workspace.close()
