"""根据首条用户消息生成会话标题。"""

from __future__ import annotations

import asyncio
import re

from langchain_core.messages import HumanMessage, SystemMessage

from domain.integrations import LLMPort
from domain.sessions import MessageRepository, SessionTitleJob, SessionTitleJobRepository


class SessionTitleService:
    """执行受限输入、超时和输出校验的标题生成调用。"""

    def __init__(
        self,
        llm: LLMPort,
        messages: MessageRepository,
        *,
        timeout_seconds: float = 8.0,
    ) -> None:
        self._llm = llm
        self._messages = messages
        self._timeout_seconds = timeout_seconds

    async def generate(self, job: SessionTitleJob) -> str:
        message = await self._messages.get(job.source_message_id)
        if message is None or message.session_id != job.session_id:
            raise LookupError("title_source_message_missing")
        content = message.content[:2000].strip()
        if content.lower() in {
            "please process the attached files.",
            "please process the attached files",
            "请处理附件。",
            "请处理附件",
        }:
            content = ""
        attachment_names = job.attachment_names
        if not content and not attachment_names:
            raise ValueError("title_source_empty")
        user_content = "首条消息：\n" + (content or "（无文字）")
        if attachment_names:
            user_content += "\n附件文件名：\n" + "\n".join(attachment_names[:10])
        result = await asyncio.wait_for(
            self._llm.invoke(
                [
                    SystemMessage(
                        content=(
                            "你只负责为聊天命名，不负责回答或执行用户的请求。"
                            "根据用户首条消息提炼任务主题，忽略用户要求的回答格式、"
                            "示例答案和礼貌性开头。"
                            "只返回标题，不要引号、编号或解释。"
                            "中文标题控制在 8 到 20 个字，英文标题控制在 3 到 8 个词，"
                            "最终不超过 32 个字符。"
                            "例如：‘请用一句话介绍你能做什么’应命名为‘介绍助手能力’，"
                            "而不是直接写介绍内容。"
                            "如果只有附件，从文件名概括用户可能要做的事。"
                        )
                    ),
                    HumanMessage(content=user_content),
                ],
                temperature=0,
            ),
            timeout=self._timeout_seconds,
        )
        raw_content = getattr(result, "content", result)
        if isinstance(raw_content, list):
            raw_content = "".join(
                block.get("text", "")
                for block in raw_content
                if isinstance(block, dict)
            )
        if not isinstance(raw_content, str):
            raise ValueError("title_output_invalid")
        title = re.sub(r"\s+", " ", raw_content).strip().strip("\"'“”‘’`「」『』")
        title = title.strip("：:，,。.!！?？-–— ")
        if not title:
            raise ValueError("title_output_empty")
        return title[:32]


__all__ = ["SessionTitleService"]
