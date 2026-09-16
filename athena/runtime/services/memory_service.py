"""Memory retrieval service extracted from ``LangGraphRuntime``.

Determines whether memory retrieval is needed for a user message and
performs the actual retrieval against the memory backend.  The decision
logic uses deterministic routing for simple cases and falls back to the
LLM only for ambiguous, reference-heavy turns.
"""

from __future__ import annotations

import json
import asyncio
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from langchain_core.messages import HumanMessage

from athena.core.llm.provider import LLMProvider
from athena.core.memory.retrieval import MemoryRetrievalService
from athena.core.memory.contracts import MemoryRetrievalRequest
from athena.utils.logging import get_logger
from athena.utils.prompts import get_prompt
from athena.utils.llm import extract_json_from_llm_response, extract_message_text

logger = get_logger(__name__)


class _MemoryRetrievalPlan(BaseModel):
    """Validated LLM output for refining an already-approved retrieval request."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    task: str = Field(default="", max_length=500)
    limit: int = Field(default=8, ge=1, le=100)


def _recent_history_by_turns(
    history: list[dict[str, Any]],
    keep_count: int = 6,
) -> list[dict[str, Any]]:
    """按 ``run_id`` 保留系统消息和最近若干个完整对话轮次。

    一个 ``run_id`` 对应一轮，因此同一轮中的 user/assistant/tool 消息不会
    被截断。没有 ``run_id`` 的消息（例如压缩摘要）作为前缀保留。
    """
    if not history or keep_count <= 0:
        return []

    # Newer persisted messages carry a run_id, which is the authoritative
    # turn boundary. Older rows did not, so infer boundaries from user
    # messages while retaining system/summary messages as a stable prefix.
    has_run_ids = any(item.get("run_id") for item in history)
    prefix: list[dict[str, Any]] = []
    turns: list[list[dict[str, Any]]] = []

    if has_run_ids:
        turns_by_run_id: dict[str, list[dict[str, Any]]] = {}
        run_order: list[str] = []
        for message in history:
            run_id = message.get("run_id")
            if not run_id or message.get("role") == "system":
                prefix.append(message)
                continue
            key = str(run_id)
            if key not in turns_by_run_id:
                turns_by_run_id[key] = []
                run_order.append(key)
            turns_by_run_id[key].append(message)
        turns = [turns_by_run_id[key] for key in run_order]
    else:
        current: list[dict[str, Any]] | None = None
        for message in history:
            role = message.get("role")
            if role == "system" or current is None and role != "user":
                if current is None:
                    prefix.append(message)
                else:
                    current.append(message)
                continue
            if role == "user":
                if current is not None:
                    turns.append(current)
                current = [message]
            elif current is not None:
                current.append(message)
            else:
                prefix.append(message)
        if current is not None:
            turns.append(current)

    return prefix + [item for turn in turns[-keep_count:] for item in turn]


class MemoryService:
    """Encapsulates memory-request construction and retrieval.

    Deterministic routing handles the majority of ordinary requests;
    the LLM is consulted only for ambiguous, reference-heavy turns.
    """

    def __init__(
        self,
        llm: LLMProvider,
        memory_retrieval: MemoryRetrievalService,
        retrieval_timeout_seconds: float = 3.0,
    ):
        self._llm = llm
        self._memory_retrieval = memory_retrieval
        self._retrieval_timeout_seconds = retrieval_timeout_seconds

    async def build_memory_request(
        self,
        session_id: str,
        user_message: str,
        history: list[dict[str, Any]] | None = None,
    ) -> MemoryRetrievalRequest | None:
        """Create a retrieval request, using LLM only for ambiguous complex turns."""
        history = history or []
        text = (user_message or "").strip()
        if not text:
            return None
        low_value = (
            "你好", "谢谢", "感谢", "天气", "帮我算", "翻译", "好的", "收到",
            "明白", "可以", "行",
        )
        # 极短的低价值问候/致谢（≤12 字符）
        if len("".join(text.split())) <= 12 and any(item in text for item in low_value):
            return None
        markers = (
            "继续",
            "刚才",
            "之前",
            "上次",
            "这个项目",
            "那个方案",
            "已有",
            "记忆",
        )
        knowledge_markers = (
            "是什么", "什么是", "目标", "标准", "要求", "规范", "定义",
            "原理", "原因", "区别", "对比", "方法", "步骤", "指南",
            "知识", "概念", "课程", "教材", "识字", "年级", "如何",
            "怎么", "为什么", "哪些", "多少", "清单", "规则", "政策",
            "历史", "资料", "参考",
        )
        is_context_reference = any(marker in text for marker in markers)
        is_knowledge_query = any(marker in text for marker in knowledge_markers)
        is_question = text.endswith(("?", "？"))
        # 知识库查询经常是一个短主题短语，不能再用固定长度直接过滤。
        # 只有明确的执行指令才跳过；其它有内容的消息都先检索，再由
        # 检索器根据结果为空与否决定是否向模型注入上下文。
        imperative_markers = ("执行", "修复", "修改", "运行", "打开", "删除", "提交")
        if (
            not is_context_reference
            and not is_knowledge_query
            and not is_question
            and any(marker in text for marker in imperative_markers)
        ):
            return None
        request = MemoryRetrievalRequest(
            session_id=session_id,
            query=text,
            task=text,
            reason=(
                "context_reference"
                if is_context_reference
                else "knowledge_query"
                if is_knowledge_query
                else "substantive_task"
            ),
        )
        # Deterministic routing handles ordinary requests. Complex, reference-
        # heavy turns benefit from an LLM-generated compact query, but malformed
        # or unavailable responses always fall back to the deterministic request.
        ambiguous_references = (
            "继续",
            "这个",
            "那个",
            "它",
            "上述",
            "前面",
            "之前",
            "上次",
            "刚才",
        )
        # 复杂情况满足以下任一条件：
        # 1. 消息长度超过 120 字符（可能包含大量细节）。
        # 2. 包含两个及以上的问号（可能表示多轮追问或复杂问题）。
        # 3. 消息长度在 50 字符以内，但包含模糊指代词（如"这个"、"它"、"上面"等），这类消息高度依赖上下文，原始文本不足以作为检索查询。
        complex_turn = (
            len(text) > 120
            or text.count("?") + text.count("？") > 1
            or (len(text) < 50 and any(item in text for item in ambiguous_references))
        )
        if not complex_turn:
            return request
        try:
            recent_history = json.dumps(
                _recent_history_by_turns(history), ensure_ascii=False, default=str
            )[-3000:]
            prompt = get_prompt("memory_retrieval_request").format(
                user_message=text,
                recent_history=recent_history or "[]",
            )
            async with asyncio.timeout(self._retrieval_timeout_seconds):
                response = await self._llm.ainvoke([HumanMessage(content=prompt)])
            data = extract_json_from_llm_response(extract_message_text(response)) or {}
            plan = _MemoryRetrievalPlan.model_validate(data)
            query = plan.query.strip()
            if not query:
                return request
            return MemoryRetrievalRequest(
                session_id=session_id,
                query=query,
                task=plan.task.strip() or text,
                reason="llm_complex_request",
                limit=plan.limit,
            )
        except TimeoutError:
            logger.warning(
                "memory_request_generation_timeout",
                session_id=session_id,
                timeout_seconds=self._retrieval_timeout_seconds,
            )
            return request
        except Exception as exc:
            logger.warning(
                "memory_request_generation_failed",
                session_id=session_id,
                error=str(exc),
            )
            return request

    async def retrieve_memory_context(
        self,
        session_id: str,
        memory_request: dict[str, Any] | None,
        *,
        run_id: str = "",
    ) -> str:
        """检索与用户消息相关的长期记忆上下文。

        参数：
            session_id (str): 会话唯一标识。
            memory_request (dict[str, Any] | None): 已构建的检索请求。

        返回值：
            str: 格式化后的记忆上下文；检索失败时返回空字符串。

        异常：
            不向主流程传播检索异常；失败仅记录警告并返回空字符串。
        """
        started = time.perf_counter()
        try:
            if not memory_request:
                return ""
            request = MemoryRetrievalRequest.model_validate(memory_request)
            async with asyncio.timeout(self._retrieval_timeout_seconds):
                context = await self._memory_retrieval.get_context(request)
            logger.info(
                "memory_injection_success",
                session_id=session_id,
                run_id=run_id,
                memory_context_length=len(context),
                duration_ms=round((time.perf_counter() - started) * 1000),
            )
            return context
        except TimeoutError:
            logger.warning(
                "memory_retrieval_timeout",
                session_id=session_id,
                run_id=run_id,
                timeout_seconds=self._retrieval_timeout_seconds,
                duration_ms=round((time.perf_counter() - started) * 1000),
            )
            return ""
        except Exception as e:
            logger.warning(
                "memory_injection_failed",
                session_id=session_id,
                run_id=run_id,
                error=str(e),
                duration_ms=round((time.perf_counter() - started) * 1000),
            )
            return ""
