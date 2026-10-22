from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from domain.integrations.llm_ports import LLMPort

from .specs import QueryHints, UserTaskSpec


_PROMPT_ROOT = Path(__file__).resolve().parents[4] / "prompt"


@dataclass(frozen=True, slots=True)
class TaskUnderstandingResult:
    task: UserTaskSpec
    source: str


class TaskUnderstandingService:
    """Use deterministic fast paths before an optional structured LLM."""

    def __init__(
        self,
        structured_llm: LLMPort | None = None,
        *,
        timeout_seconds: float = 15.0,
        prompt_root: Path | None = None,
    ) -> None:
        self._structured_llm = structured_llm
        self._timeout_seconds = timeout_seconds
        self._prompt_root = prompt_root or _PROMPT_ROOT

    async def understand(
        self,
        *,
        user_message: str,
        history: list[dict[str, Any]] | None = None,
        attachment_refs: list[dict[str, Any]] | None = None,
        knowledge_bases: list[dict[str, Any]] | None = None,
    ) -> TaskUnderstandingResult:
        text = user_message.strip()
        attachments = tuple(
            str(item.get("id", ""))
            for item in (attachment_refs or [])
            if item.get("id")
        )
        fast = self._fast_path(text, attachments)

        if fast is not None:
            return TaskUnderstandingResult(fast, "fast_path")

        try:
            value = await asyncio.wait_for(
                self._call_structured(
                    text,
                    history or [],
                    attachment_refs or [],
                    knowledge_bases or [],
                ),
                timeout=self._timeout_seconds,
            )
            return TaskUnderstandingResult(
                self._normalize(value, text=text, attachment_ids=attachments), "llm"
            )
        except Exception:
            pass
        return TaskUnderstandingResult(
            self._normalize(
                self._heuristic(text), text=text, attachment_ids=attachments
            ),
            "fallback",
        )

    async def _call_structured(
        self,
        text: str,
        history: list[dict[str, Any]],
        attachments: list[dict[str, Any]],
        knowledge_bases: list[dict[str, Any]],
    ) -> Any:
        """调用结构化 LLM 适配器。"""
        if self._structured_llm is None:
            raise RuntimeError("structured LLM is not configured")
        context = {
            "history": history[-8:],
            "attachments": attachments,
            "knowledge_bases": knowledge_bases,
        }
        prompt = (self._prompt_root / "task_understanding.md").read_text(
            encoding="utf-8"
        )
        value = await self._structured_llm.invoke(
            [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"current_message": text, **context},
                        ensure_ascii=False,
                    ),
                },
            ],
            **context,
        )
        if isinstance(value, dict) and isinstance(value.get("content"), str):
            try:
                return json.loads(value["content"])
            except json.JSONDecodeError:
                return value
        return value

    def _normalize(
        self, value: Any, *, text: str, attachment_ids: tuple[str, ...]
    ) -> UserTaskSpec:
        """将模型结果和附件引用规范化为生产契约。"""
        if isinstance(value, UserTaskSpec):
            payload = value.model_dump()
        elif hasattr(value, "model_dump"):
            payload = value.model_dump()
        elif isinstance(value, dict):
            payload = dict(value)
            if isinstance(payload.get("query_hints"), (list, tuple, str)):
                payload["query_hints"] = {"knowledge": payload["query_hints"]}
        else:
            payload = {}
        payload.setdefault("goal", text or "answer the user")
        payload["attachment_ids"] = tuple(
            dict.fromkeys((*payload.get("attachment_ids", ()), *attachment_ids))
        )
        requirements = list(payload.get("context_requirements") or ())
        if attachment_ids and "file" not in requirements:
            requirements.append("file")
        payload["context_requirements"] = tuple(requirements or ("conversation",))
        hints = payload.get("query_hints") or {}
        if isinstance(hints, QueryHints):
            payload["query_hints"] = hints.model_dump()
        return UserTaskSpec.model_validate(payload)

    @staticmethod
    def _fast_path(text: str, attachments: tuple[str, ...]) -> UserTaskSpec | None:
        if not text:
            return UserTaskSpec(
                goal="respond to the empty input",
                attachment_ids=attachments,
                confidence=1.0,
            )
        lowered = text.lower()
        requests_multi_agent_execution = any(
            marker in lowered
            for marker in (
                "multi-agent",
                "multi agent",
                "多 agent",
                "多agent",
                "多智能体",
                "多个代理",
            )
        ) and any(
            marker in lowered
            for marker in ("运行", "执行", "拆分", "分工", "并行")
        )
        if requests_multi_agent_execution:
            return UserTaskSpec(
                goal=text,
                mode="plan",
                attachment_ids=attachments,
                confidence=1.0,
            )
        if any(marker in text.lower() for marker in ("执行命令", "运行命令", "shell", "exec_shell")):
            command = text
            lowered_command = command.lower()
            for marker in ("exec_shell", "shell", "执行命令", "运行命令"):
                if marker in lowered_command:
                    command = command[lowered_command.find(marker) + len(marker):].strip(" ：:`")
                    break
            return UserTaskSpec(
                goal=text,
                mode="act",
                slots={"tool_name": "exec_shell", "command": command},
                attachment_ids=attachments,
                confidence=1.0,
            )
        if len(text) <= 16 and any(
            marker in text
            for marker in ("你好", "您好", "谢谢", "感谢", "好的", "收到", "明白")
        ):
            return UserTaskSpec(goal=text, attachment_ids=attachments, confidence=1.0)
        if any(
            marker in text
            for marker in (
                "你知道我",
                "你记得我",
                "还记得我",
                "我最喜欢",
                "我最爱吃",
                "我喜欢吃",
                "我爱吃",
                "我的偏好",
                "之前说过",
                "上次说过",
            )
        ):
            return UserTaskSpec(
                goal=text,
                mode="retrieve",
                context_requirements=("conversation", "memory"),
                attachment_ids=attachments,
                confidence=1.0,
                query_hints=QueryHints(memory=text),
            )
        if len(text) <= 16 and any(
            marker in text.lower()
            for marker in ("search", "find", "查找", "检索", "查询")
        ):
            return UserTaskSpec(
                goal=text,
                mode="retrieve",
                context_requirements=("conversation", "knowledge"),
                attachment_ids=attachments,
                confidence=1.0,
                query_hints=QueryHints(knowledge=text),
            )
        return None

    @staticmethod
    def _heuristic(text: str) -> dict[str, Any]:
        lowered = text.lower()
        planning = any(
            marker in lowered
            for marker in (
                "multi-agent",
                "multi agent",
                "multi-worker",
                "multi worker",
                "worker",
                "多 agent",
                "多agent",
                "多智能体",
                "多个 worker",
                "多个代理",
                "并行执行",
                "并行处理",
                "拆分任务",
                "分工协作",
            )
        )
        retrieving = any(
            marker in lowered
            for marker in (
                "search",
                "find",
                "查找",
                "检索",
                "查询",
                "之前说过",
                "知识库",
                "附件",
                "最喜欢",
                "记得我",
                "我的偏好",
            )
        )
        needs_memory = any(
            marker in lowered
            for marker in ("最喜欢", "记得我", "我的偏好", "之前说过", "上次说过")
        )
        requirements = ["conversation"]
        if needs_memory:
            requirements.append("memory")
        if retrieving and not needs_memory:
            requirements.append("knowledge")
        return {
            "goal": text,
            "mode": "plan" if planning else ("retrieve" if retrieving else "answer"),
            "confidence": 0.65 if retrieving else 0.45,
            "context_requirements": tuple(requirements),
            "query_hints": {
                "memory": text if needs_memory else "",
                "knowledge": text if retrieving and not needs_memory else "",
            },
        }


__all__ = ["TaskUnderstandingResult", "TaskUnderstandingService"]
