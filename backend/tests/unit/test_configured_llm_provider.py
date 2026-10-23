"""Tests for configured LLM provider behavior."""

import asyncio
import time
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel

from infrastructure.integrations.llm.llm_provider import ConfiguredLLMProvider
from infrastructure.integrations.llm.options import (
    LLMOptions,
    LLMProviderOptions,
    RetryOptions,
)


def _configured_provider(
    model: BaseChatModel,
    *,
    attempts: int = 1,
    timeout_seconds: float = 1.0,
) -> ConfiguredLLMProvider:
    provider = ConfiguredLLMProvider(
        LLMOptions(
            providers=(LLMProviderOptions(provider="ollama"),),
            retry=RetryOptions(
                attempts=attempts,
                timeout_seconds=timeout_seconds,
                delay_seconds=0,
            ),
        )
    )
    provider._model = model
    return provider


def _chat_result(content: str) -> ChatResult:
    return ChatResult(
        generations=[ChatGeneration(message=AIMessage(content=content))]
    )


class _TestChatModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "test-chat-model"

    def _generate(
        self,
        _messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: object | None = None,
        **kwargs: object,
    ) -> ChatResult:
        raise NotImplementedError


@pytest.mark.asyncio
async def test_configured_provider_retries_using_its_options() -> None:
    class FlakyModel(_TestChatModel):
        calls: int = 0

        def _generate(
            self,
            _messages: list[BaseMessage],
            stop: list[str] | None = None,
            run_manager: object | None = None,
            **kwargs: object,
        ) -> ChatResult:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary failure")
            return _chat_result("ok")

    model = FlakyModel()
    provider = _configured_provider(model, attempts=2)

    response = await provider.invoke([HumanMessage(content="hello")])
    assert response.content == "ok"
    assert model.calls == 2


@pytest.mark.asyncio
async def test_configured_provider_retries_structured_output() -> None:
    class StructuredAnswer(BaseModel):
        answer: str

    class FlakyStructuredModel(_TestChatModel):
        calls: int = 0

        def with_structured_output(
            self,
            schema: type[Any] | dict[str, Any],
            *,
            include_raw: bool = False,
            **kwargs: Any,
        ) -> RunnableLambda[Any, Any]:
            assert schema is StructuredAnswer
            assert include_raw is False

            async def invoke(_messages: list[Any]) -> StructuredAnswer:
                self.calls += 1
                if self.calls == 1:
                    return StructuredAnswer.model_validate(
                        {"unexpected": "shape"}
                    )
                return StructuredAnswer(answer="ok")

            return RunnableLambda(func=lambda _messages: None, afunc=invoke)

    model = FlakyStructuredModel()
    provider = _configured_provider(model, attempts=2)

    response = await provider.invoke_structured(
        [HumanMessage(content="hello")],
        StructuredAnswer,
    )

    assert response == StructuredAnswer(answer="ok")
    assert model.calls == 2


@pytest.mark.asyncio
async def test_configured_provider_bounds_slow_model_calls() -> None:
    class SlowModel(_TestChatModel):
        async def _agenerate(
            self,
            _messages: list[BaseMessage],
            stop: list[str] | None = None,
            run_manager: object | None = None,
            **kwargs: object,
        ) -> ChatResult:
            await asyncio.sleep(0.02)
            return _chat_result("late")

    provider = _configured_provider(SlowModel(), timeout_seconds=0.001)

    with pytest.raises(RuntimeError, match="LLM invocation failed"):
        await provider.invoke([HumanMessage(content="hello")])


@pytest.mark.asyncio
async def test_configured_provider_offloads_synchronous_model_calls() -> None:
    class SlowSyncModel(_TestChatModel):
        def _generate(
            self,
            _messages: list[BaseMessage],
            stop: list[str] | None = None,
            run_manager: object | None = None,
            **kwargs: object,
        ) -> ChatResult:
            time.sleep(0.06)
            return _chat_result("ok")

    provider = _configured_provider(SlowSyncModel())
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        deadline = asyncio.get_running_loop().time() + 0.04
        while asyncio.get_running_loop().time() < deadline:
            ticks += 1
            await asyncio.sleep(0.002)

    result, _ = await asyncio.gather(
        provider.invoke([HumanMessage(content="hello")]), heartbeat()
    )

    assert result.content == "ok"
    assert ticks >= 5
