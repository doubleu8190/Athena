"""保护隐私的检索轨迹数据结构。

本模块中的类型有意不包含文档内容。调用方可以将其写入结构化日志，或在离线
评估中使用，而无需让核心检索代码依赖某个遥测厂商。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from athena.utils.ids import generate_time_id

_DEFAULT_QUERY_HASH_SALT = secrets.token_hex(32)


@dataclass
class RetrievalStageTrace:
    """单个检索阶段的耗时和结果摘要。"""

    stage: str
    route: str
    query_index: int
    duration_ms: float
    result_count: int
    error_code: str | None = None

    def as_log_fields(self) -> dict[str, Any]:
        """生成结构化日志字段。

        返回值：
            dict[str, Any]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        return {
            "stage": self.stage,
            "route": self.route,
            "query_index": self.query_index,
            "duration_ms": round(self.duration_ms, 3),
            "result_count": self.result_count,
            "error_code": self.error_code,
        }


@dataclass
class RetrievalCandidateTrace:
    """候选项在当前路由中的证据，不包含源内容。"""

    item_id: str
    route: str
    rank: int
    native_score: float | None
    fused_score: float | None = None
    selected: bool = False

    def as_log_fields(self) -> dict[str, Any]:
        """生成结构化日志字段。

        返回值：
            dict[str, Any]: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        return {
            "item_id": self.item_id,
            "route": self.route,
            "rank": self.rank,
            "native_score": self.native_score,
            "fused_score": self.fused_score,
            "selected": self.selected,
        }


@dataclass
class RetrievalTrace:
    """请求级检索轨迹，支持安全的结构化日志序列化。"""

    source_scope: str
    query: str
    query_hash_salt: str = ""
    include_raw_query: bool = False
    query_labels: list[str] = field(default_factory=list)
    analyzer_mode: str = "rule"
    request_id: str = field(default_factory=generate_time_id)
    stages: list[RetrievalStageTrace] = field(default_factory=list)
    candidates: list[RetrievalCandidateTrace] = field(default_factory=list)
    selected_ids: list[str] = field(default_factory=list)
    fallback_reasons: list[str] = field(default_factory=list)
    _started_at: float = field(default_factory=perf_counter, init=False, repr=False)
    _total_duration_ms: float | None = field(default=None, init=False, repr=False)

    @property
    def query_hash(self) -> str:
        """

        返回值：
            str: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        salt = self.query_hash_salt or _DEFAULT_QUERY_HASH_SALT
        payload = f"{salt}\x00{self.query}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @property
    def query_length(self) -> int:
        """

        返回值：
            int: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        return len(self.query)

    def add_stage(
        self,
        *,
        stage: str,
        route: str,
        query_index: int,
        duration_ms: float,
        result_count: int,
        error_code: str | None = None,
    ) -> None:
        """

        参数：
            stage (str): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            route (str): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            query_index (int): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            duration_ms (float): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            result_count (int): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            error_code (str | None): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        self.stages.append(
            RetrievalStageTrace(
                stage=stage,
                route=route,
                query_index=query_index,
                duration_ms=duration_ms,
                result_count=result_count,
                error_code=error_code,
            )
        )

    def add_candidate(
        self,
        *,
        item_id: str,
        route: str,
        rank: int,
        native_score: float | None,
    ) -> None:
        """

        参数：
            item_id (str): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            route (str): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            rank (int): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。
            native_score (float | None): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        self.candidates.append(
            RetrievalCandidateTrace(
                item_id=item_id,
                route=route,
                rank=rank,
                native_score=native_score,
            )
        )

    def set_fused_scores(self, scores: dict[str, float]) -> None:
        """

        参数：
            scores (dict[str, float]): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        for candidate in self.candidates:
            candidate.fused_score = scores.get(candidate.item_id)

    def mark_selected(self, item_ids: list[str]) -> None:
        """

        参数：
            item_ids (list[str]): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        self.selected_ids = list(dict.fromkeys(item_ids))
        selected = set(self.selected_ids)
        for candidate in self.candidates:
            candidate.selected = candidate.item_id in selected

    def add_fallback(self, reason: str) -> None:
        """

        参数：
            reason (str): 调用方必须传入符合类型注解的值；可选参数按默认值处理，其他约束由方法内部校验。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        if reason not in self.fallback_reasons:
            self.fallback_reasons.append(reason)

    def finish(self) -> None:
        """完成追踪。

        返回值：
            None: 返回该方法声明类型的业务结果，内容由方法职责确定。

        异常：
            异常: 底层校验、存储、网络或服务调用失败且未被当前方法处理时向上传播。
        """
        if self._total_duration_ms is None:
            self._total_duration_ms = (perf_counter() - self._started_at) * 1000

    def as_log_fields(self) -> dict[str, Any]:
        """返回可安全传递给结构化日志记录器的字段。

        候选文档文本被有意排除。除非本地开发显式启用，否则原始查询也会被省略。
        """
        self.finish()
        candidate_counts: dict[str, int] = {}
        for candidate in self.candidates:
            candidate_counts[candidate.route] = (
                candidate_counts.get(candidate.route, 0) + 1
            )
        fields: dict[str, Any] = {
            "request_id": self.request_id,
            "source_scope": self.source_scope,
            "query_labels": self.query_labels,
            "analyzer_mode": self.analyzer_mode,
            "query_hash": self.query_hash,
            "query_length": self.query_length,
            "routes": sorted(candidate_counts),
            "stage_durations_ms": [stage.as_log_fields() for stage in self.stages],
            "candidate_counts": candidate_counts,
            "selected_ids": self.selected_ids,
            "score_components": [
                candidate.as_log_fields() for candidate in self.candidates
            ],
            "fallback_reason": self.fallback_reasons or None,
            "total_duration_ms": round(self._total_duration_ms or 0.0, 3),
        }
        if self.include_raw_query:
            fields["query"] = self.query
        return fields
