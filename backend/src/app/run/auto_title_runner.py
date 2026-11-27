"""首轮主 Run 完成后的异步自动标题生成与后台任务生命周期管理。"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

from app.run.auto_title import (
    AUTO_TITLE_MAX_OUTPUT_TOKENS,
    AUTO_TITLE_PROMPT_INPUT_MAX_CHARS,
    AUTO_TITLE_SYSTEM_PROMPT,
    AUTO_TITLE_TIMEOUT_SECONDS,
    build_auto_title_user_prompt,
    fallback_title_from_message,
    sanitize_generated_title,
)
from domain.llm.model import LlmMessage, LlmRequest, LlmRole, LlmText
from domain.llm.model.profile import RuntimeModelProfile
from domain.llm.ports import LlmStreamProvider
from domain.llm.profile_ports import ModelProfileRepository
from domain.session import SessionScope

if TYPE_CHECKING:
    from adapter.session.repository import SqlAlchemySessionRepository

logger = logging.getLogger(__name__)

_SECRET_PATTERN = re.compile(r"(?i)(bearer\s+\S+|sk-[A-Za-z0-9_-]{6,})")


def _sanitize_error_detail(error: BaseException) -> str:
    """折叠空白、脱敏常见凭据片段并截取前 120 字符，避免日志泄漏密钥或换行注入。"""
    compact = " ".join(str(error).split())
    masked = _SECRET_PATTERN.sub("[REDACTED]", compact)
    return masked[:120]


class AutoTitleRunner:
    """隔离管理首轮对话自动标题生成后台任务，限时且异常完全兜底。"""

    def __init__(
        self,
        *,
        profiles: ModelProfileRepository,
        provider: LlmStreamProvider,
        sessions: SqlAlchemySessionRepository | None,
        timeout_seconds: float = AUTO_TITLE_TIMEOUT_SECONDS,
    ) -> None:
        """保存模型配置、LLM 提供方、会话仓储和有界超时配置。"""
        self._profiles = profiles
        self._provider = provider
        self._sessions = sessions
        self._timeout_seconds = max(0.01, float(timeout_seconds))
        self._background_tasks: set[asyncio.Task[None]] = set()

    def schedule(
        self,
        *,
        owner_id: str,
        session_id: str,
        run_model_profile_id: str,
        trigger_text: str,
    ) -> None:
        """登记受控异步后台任务生成首轮标题，避免未回收 Task 警告。"""
        if self._sessions is None:
            return
        task = asyncio.create_task(
            self._run_auto_title_task(
                owner_id=owner_id,
                session_id=session_id,
                run_model_profile_id=run_model_profile_id,
                trigger_text=trigger_text,
            )
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def wait_background_tasks(self) -> None:
        """等待受控后台任务（如首轮自动标题生成）全部安全退出，供测试与优雅停机使用。"""
        while self._background_tasks:
            pending = tuple(self._background_tasks)
            await asyncio.gather(*pending, return_exceptions=True)

    async def _run_auto_title_task(
        self,
        *,
        owner_id: str,
        session_id: str,
        run_model_profile_id: str,
        trigger_text: str,
    ) -> None:
        """仅当未删除会话仍无标题时生成并条件写入自动标题，异常全量兜底隔离。"""
        if self._sessions is None:
            return
        try:
            visible = await self._sessions.get_visible(
                SessionScope(session_id=session_id, owner_id=owner_id)
            )
            if visible is None or visible.title is not None:
                return
            title = await self._generate_auto_title(
                run_model_profile_id=run_model_profile_id,
                trigger_text=trigger_text,
            )
            if not title:
                return
            await self._sessions.set_title_if_empty(owner_id, session_id, title)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "session_auto_title_failed session_id=%s exception_type=%s",
                session_id,
                type(error).__name__,
            )

    async def _generate_auto_title(
        self,
        *,
        run_model_profile_id: str,
        trigger_text: str,
    ) -> str:
        """优先调用压缩模型（否则复用本次 Run 模型）限时生成 <=20 字标题，
        失败或超时则降级截取前 24 字。
        """
        fallback = fallback_title_from_message(trigger_text)
        excerpt = trigger_text.strip()[:AUTO_TITLE_PROMPT_INPUT_MAX_CHARS]
        if not excerpt:
            return fallback

        runtime: RuntimeModelProfile | None = None
        try:
            policy = await self._profiles.get_current_compression_policy()
            if policy is not None:
                runtime = await self._profiles.get_runtime(policy.compression_model_profile_id)
        except Exception:
            runtime = None
        if runtime is None:
            try:
                runtime = await self._profiles.get_runtime(run_model_profile_id)
            except Exception:
                runtime = None
        if runtime is None:
            return fallback

        # 将用户首条消息明确包裹为待提炼原文，并分配足够的 output token 预算以兼容思考模型。
        request = LlmRequest(
            model_profile=runtime.version.id,
            messages=(
                LlmMessage(role=LlmRole.SYSTEM, content=AUTO_TITLE_SYSTEM_PROMPT),
                LlmMessage(role=LlmRole.USER, content=build_auto_title_user_prompt(excerpt)),
            ),
            tool_choice="none",
            max_output_tokens=min(AUTO_TITLE_MAX_OUTPUT_TOKENS, runtime.version.max_output_tokens),
            temperature=(
                0.2 if runtime.version.temperature is None else runtime.version.temperature
            ),
            timeout_seconds=min(
                float(runtime.version.request_timeout_seconds),
                self._timeout_seconds,
            ),
        )
        try:
            async with asyncio.timeout(self._timeout_seconds):
                response = await self._provider.complete(runtime, request)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "session_auto_title_llm_fallback model_profile_id=%s "
                "exception_type=%s error_detail=%s",
                runtime.version.id,
                type(error).__name__,
                _sanitize_error_detail(error),
            )
            return fallback

        if response.status != "completed":
            logger.warning(
                "session_auto_title_incomplete_fallback model_profile_id=%s "
                "status=%s incomplete_reason=%s",
                runtime.version.id,
                response.status,
                response.incomplete_reason,
            )
            return fallback
        raw_text = "".join(part.text for part in response.output if isinstance(part, LlmText))
        cleaned = sanitize_generated_title(raw_text)
        if not cleaned:
            logger.warning(
                "session_auto_title_empty_output_fallback model_profile_id=%s raw_length=%d",
                runtime.version.id,
                len(raw_text),
            )
            return fallback
        return cleaned
