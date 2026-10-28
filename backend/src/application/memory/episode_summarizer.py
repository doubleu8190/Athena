"""从冻结的 Run 执行证据生成任务回顾。"""

from __future__ import annotations

from pathlib import Path

from domain.integrations import LLMPort
from domain.memory import CompletedTurn, MemoryEpisodeProposal
from shared import read_system_prompt, render_prompt_template
from shared.contracts.memory import (
    MemoryEpisodeProposalSchema,
    memory_domain_to_schema,
    memory_schema_to_domain,
)


class LLMMemoryEpisodeSummarizer:
    """用结构化 LLM 输出压缩一次真实任务的结果和边界。"""

    def __init__(
        self, llm: LLMPort, *, prompt_root: Path | None = None
    ) -> None:
        """保存结构化模型端口和 episode 提示词路径。

        参数：``llm`` 是支持结构化输出的模型；``prompt_root`` 可覆盖默认提示词目录。
        返回：无。
        异常：不主动抛出业务异常。
        """
        self._llm = llm
        self._prompt_root = prompt_root

    async def summarize(self, turn: CompletedTurn) -> MemoryEpisodeProposal:
        """根据用户目标、最终答复和冻结执行证据生成回顾。

        参数：turn 是已经冻结 Run/Worker 来源的完成轮次。
        返回：可保存的任务回顾提案；无可复用内容时 retain 为 False。
        异常：模型调用失败或输出不符合严格契约时向上传播，供作业重试。
        """
        evidence = turn.execution_evidence
        if evidence is None:
            return MemoryEpisodeProposal(retain=False)
        prompt_template = (
            read_system_prompt("episode_summarization.md")
            if self._prompt_root is None
            else (self._prompt_root / "episode_summarization.md").read_text(
                encoding="utf-8"
            )
        )
        values = {
            "user_request": turn.user_text,
            "assistant_response": turn.assistant_text,
            "execution_evidence": memory_domain_to_schema(evidence).model_dump_json(),
        }
        prompt = render_prompt_template(prompt_template, values)
        value = await self._llm.invoke_structured(
            [{"role": "system", "content": prompt}],
            MemoryEpisodeProposalSchema,
        )
        if not isinstance(value, MemoryEpisodeProposalSchema):
            raise TypeError("memory_episode_proposal_schema_required")
        return memory_schema_to_domain(value)


__all__ = ["LLMMemoryEpisodeSummarizer"]
