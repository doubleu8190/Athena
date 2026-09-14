"""Structured Output 调用的统一入口。"""

from __future__ import annotations

from typing import Any, TypeVar

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

from athena.core.llm.provider import LLMProvider
from athena.utils.logging import get_logger

ModelT = TypeVar("ModelT", bound=BaseModel)

logger = get_logger(__name__)


class StructuredLLMService:
    """使用模型原生 Structured Output 生成并校验 Pydantic 模型。"""

    def __init__(self, primary: LLMProvider) -> None:
        """绑定主模型。

        参数：
            primary: 编排使用的主 LLM Provider。

        返回值：
            None。

        异常：
            不主动抛出业务异常。
        """
        self._primary = primary

    def validate_primary(self, schema: type[ModelT]) -> None:
        """确保主模型和所有 fallback 模型都支持结构化输出。

        参数：
            schema: 编排阶段将强制使用的 Pydantic 输出模型。

        返回值：
            None。

        异常：
            RuntimeError: 任一编排模型不支持该 schema。
        """

        providers = [self._primary, *self._primary.fallback_providers]
        unsupported = [
            provider
            for provider in providers
            if not provider.supports_structured_output(schema)
        ]
        if unsupported:
            raise RuntimeError(
                "structured output is required for multi-agent orchestration; "
                f"unsupported providers: {len(unsupported)}"
            )

    async def generate(
        self,
        schema: type[ModelT],
        system_prompt: str,
        user_message: str,
    ) -> ModelT:
        """调用主模型生成并返回 ``schema`` 实例。

        参数：
            schema: 期望输出的 Pydantic 模型。
            system_prompt: 包含字段约束说明的系统提示词。
            user_message: 规划或任务目标文本。

        返回值：
            ModelT: 已由 Pydantic 完成校验的结构化结果。

        异常：
            NotImplementedError: 主模型不支持结构化输出。
            ValueError: 模型返回空或无法解析为指定模型。
        """

        runnable = self._primary.structured_runnable(schema)
        messages: list[BaseMessage] = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_message),
        ]
        try:
            result: Any = await runnable.ainvoke(messages)
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            logger.exception(
                "structured_output_request_failed",
                schema=schema.__name__,
                error=detail,
            )
            raise RuntimeError(
                "结构化 LLM 请求失败："
                f"{detail}。请确认 LLM_PROVIDERS 中的 base_url 指向支持 "
                "OpenAI 兼容 JSON 和 structured output 的接口。"
            ) from exc
        if result is None:
            raise ValueError("structured LLM returned no result")
        if isinstance(result, schema):
            return result
        if isinstance(result, BaseModel):
            return schema.model_validate(result.model_dump(mode="json"))
        if isinstance(result, dict):
            return schema.model_validate(result)
        raise ValueError(
            f"structured LLM returned unsupported type: {type(result).__name__}"
        )
