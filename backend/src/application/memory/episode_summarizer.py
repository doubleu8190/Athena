"""从冻结的 Run 执行证据生成任务回顾。"""

from __future__ import annotations

from pathlib import Path

from domain.integrations import LLMPort
from domain.memory import CompletedTurn, MemoryEpisodeProposal
from shared import read_system_prompt, render_prompt_template
from application.contracts.memory import (
    MemoryEpisodeProposalSchema,
    memory_domain_to_schema,
    memory_schema_to_domain,
)


class LLMMemoryEpisodeSummarizer:
    """用结构化 LLM 输出压缩一次真实任务的结果和边界。"""

    def __init__(self, llm: LLMPort, *, prompt_root: Path | None = None) -> None:
        """保存模型依赖，并配置可选的回顾提示词目录。

        参数：
        - `llm`（LLMPort）：执行模型请求的语言模型依赖；约束：无额外运行时限制。
        - `prompt_root`（Path | None）：提示模板文件所在目录；None 表示使用默认目录；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._llm = llm
        self._prompt_root = prompt_root

    async def summarize(self, turn: CompletedTurn) -> MemoryEpisodeProposal:
        """调用模型生成结构化记忆回顾提案。

        参数：
        - `turn`（CompletedTurn）：当前已经完成的对话轮次及其消息；约束：无额外运行时限制。

        返回：
        - MemoryEpisodeProposal：保存对话摘要、来源和有效状态的记忆回顾记录。

        异常：
        - TypeError：校验条件 `not isinstance(value, MemoryEpisodeProposalSchema)` 成立时（异常说明：memory_episode_proposal_schema_required）。
        - 未列出的依赖异常按当前实现传播。
        """
        evidence = turn.execution_evidence
        if evidence is None:
            return MemoryEpisodeProposal(retain=False)
        prompt_template = read_system_prompt("episode_summarization.md")
        prompt = render_prompt_template(
            prompt_template,
            {
                "user_request": turn.user_text,
                "assistant_response": turn.assistant_text,
                "execution_evidence": memory_domain_to_schema(
                    evidence
                ).model_dump_json(),
            },
        )
        value = await self._llm.invoke_structured(
            [{"role": "system", "content": prompt}],
            MemoryEpisodeProposalSchema,
        )
        if not isinstance(value, MemoryEpisodeProposalSchema):
            raise TypeError("memory_episode_proposal_schema_required")
        return memory_schema_to_domain(value)


__all__ = ["LLMMemoryEpisodeSummarizer"]
