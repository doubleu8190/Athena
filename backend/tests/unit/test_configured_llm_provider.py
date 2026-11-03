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
from configuration.settings import LLMProviderConfig, LLMRetrySettings, Settings


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


def test_stream_disables_retries_on_the_actual_sdk_clients() -> None:
    """无网络构造真实 SDK，验证流式客户端关闭重试且不修改普通调用。"""
    provider = ConfiguredLLMProvider(LLMOptions(providers=(
        LLMProviderOptions(api_key="unit-test-key", base_url="http://127.0.0.1:1/v1"),
    )))
    assert provider._stream_model.root_client.max_retries == 0
    assert provider._stream_model.root_async_client.max_retries == 0
    assert provider._model.root_client.max_retries == 2
    assert provider._stream_model.root_client is not provider._model.root_client


@pytest.mark.asyncio
async def test_configured_provider_invokes_the_configured_second_model(monkeypatch) -> None:
    from infrastructure.integrations.llm import llm_provider

    models: list[str] = []

    def create_model(options, *, sdk_max_retries=None):
        model_name = options.primary.model
        models.append(model_name)
        return RunnableLambda(lambda _messages: AIMessage(content=model_name))

    monkeypatch.setattr(llm_provider, "_create_model", create_model)
    provider = ConfiguredLLMProvider(
        LLMOptions(
            providers=(
                LLMProviderOptions(provider="ollama", model="main"),
                LLMProviderOptions(provider="ollama", model="light"),
            ),
            retry=RetryOptions(attempts=1, delay_seconds=0),
        )
    )

    result = await provider.complete_secondary([HumanMessage(content="rewrite")])

    assert result.content == "light"
    assert models == ["main", "main", "light"]


@pytest.mark.asyncio
async def test_secondary_model_uses_secondary_timeout_policy(monkeypatch) -> None:
    from infrastructure.integrations.llm import llm_provider

    async def slow(messages):
        await asyncio.sleep(0.05)
        return AIMessage(content="too late")

    def create_model(options, *, sdk_max_retries=None):
        if options.primary.model == "light":
            return RunnableLambda(slow)
        return RunnableLambda(lambda _messages: AIMessage(content="main"))

    monkeypatch.setattr(llm_provider, "_create_model", create_model)
    settings = Settings(
        _env_file=None,
        llm_providers=[
            LLMProviderConfig(provider="ollama", model="main"),
            LLMProviderConfig(provider="ollama", model="light"),
        ],
        llm_retry=LLMRetrySettings(max_attempts=1, timeout_ms=1000, min_delay_ms=0),
        llm_secondary_retry=LLMRetrySettings(max_attempts=1, timeout_ms=5, min_delay_ms=0),
    )
    provider = ConfiguredLLMProvider(settings)

    with pytest.raises(RuntimeError, match="LLM invocation failed"):
        await provider.complete_secondary([HumanMessage(content="rewrite")])


@pytest.mark.asyncio
async def test_secondary_structured_output_uses_secondary_model_and_retry(monkeypatch) -> None:
    from infrastructure.integrations.llm import llm_provider

    class Rewrite(BaseModel):
        rewritten_query: str

    models: list[str] = []
    calls = 0

    class Model:
        def __init__(self, name):
            self.name = name

        def with_structured_output(self, schema, *, include_raw=False):
            assert schema is Rewrite
            assert include_raw is False

            async def invoke(_messages):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("retry")
                return Rewrite(rewritten_query="改写问题")

            return RunnableLambda(afunc=invoke, func=lambda _messages: None)

    def create_model(options, *, sdk_max_retries=None):
        models.append(options.primary.model)
        return Model(options.primary.model)

    monkeypatch.setattr(llm_provider, "_create_model", create_model)
    provider = ConfiguredLLMProvider(
        Settings(
            _env_file=None,
            llm_providers=[
                LLMProviderConfig(provider="ollama", model="main"),
                LLMProviderConfig(provider="ollama", model="light"),
            ],
            llm_retry=LLMRetrySettings(max_attempts=1, timeout_ms=1000, min_delay_ms=0),
            llm_secondary_retry=LLMRetrySettings(max_attempts=2, timeout_ms=1000, min_delay_ms=0),
        )
    )

    result = await provider.invoke_secondary_structured(
        [HumanMessage(content="rewrite")], Rewrite
    )

    assert result == Rewrite(rewritten_query="改写问题")
    assert models == ["main", "main", "light"]
    assert calls == 2


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
