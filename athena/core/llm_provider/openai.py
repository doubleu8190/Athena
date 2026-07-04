"""OpenAI-compatible LLM provider.

Covers OpenAI, DeepSeek, and any OpenAI-compatible API (e.g. vLLM, LiteLLM proxy).
"""

from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

from athena.core.llm_provider.base import LLMProvider, LLMResponse
from athena.core.secrets import get_secret
from athena.logging_config import get_logger

logger = get_logger(__name__)


class OpenAIProvider(LLMProvider):
    """Provider for OpenAI and OpenAI-compatible APIs.

    Supports: GPT-4o, GPT-4, DeepSeek-V3, and any API matching the
    OpenAI chat completions format.
    """

    def __init__(
        self,
        api_key_env: str = "OPENAI_API_KEY",
        model: str = "gpt-4o",
        base_url: str | None = None,
    ) -> None:
        self._api_key_env = api_key_env
        self._model = model
        self._base_url = base_url
        self._client: AsyncOpenAI | None = None

    @property
    def provider_name(self) -> str:
        return "openai"

    @property
    def model(self) -> str:
        return self._model

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            api_key = get_secret(self._api_key_env, "")
            kwargs = {"api_key": api_key}
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._client = AsyncOpenAI(**kwargs)
        return self._client

    async def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> LLMResponse:
        client = self._get_client()

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        if tools:
            # Accept both raw (flat) tools and pre-wrapped OpenAI-format tools
            openai_tools = []
            for tool in tools:
                if "function" in tool:
                    # Already wrapped: {"type": "function", "function": {"name": ...}}
                    openai_tools.append(tool)
                else:
                    # Flat format: {"name": ..., "description": ..., "parameters_schema": ...}
                    openai_tools.append({
                        "type": "function",
                        "function": {
                            "name": tool["name"],
                            "description": tool.get("description", ""),
                            "parameters": tool.get("parameters_schema", {}),
                        },
                    })
            kwargs["tools"] = openai_tools
            kwargs["tool_choice"] = "auto"

        try:
            response = await client.chat.completions.create(**kwargs)
            choice = response.choices[0]
            message = choice.message

            tool_calls = []
            if message.tool_calls:
                for tc in message.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        logger.error("tool_call_arguments_json_decode_error", tool_call_id=tc.id, arguments=tc.function.arguments)
                        args = {}
                    tool_calls.append({
                        "id": tc.id,
                        "name": tc.function.name,
                        "arguments": args,
                    })

            return LLMResponse(
                text=message.content or "",
                tool_calls=tool_calls,
                token_usage={
                    "prompt_tokens": response.usage.prompt_tokens if response.usage else 0,
                    "completion_tokens": response.usage.completion_tokens if response.usage else 0,
                    "total_tokens": response.usage.total_tokens if response.usage else 0,
                },
                model=response.model,
                finish_reason=choice.finish_reason or "stop",
            )
        except Exception as e:
            logger.error("openai_generate_failed", error=str(e), model=self._model)
            raise

    async def generate_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ):
        client = self._get_client()

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }

        if tools:
            openai_tools = []
            for tool in tools:
                if "function" in tool:
                    openai_tools.append(tool)
                else:
                    openai_tools.append({
                        "type": "function",
                        "function": {
                            "name": tool["name"],
                            "description": tool.get("description", ""),
                            "parameters": tool.get("parameters_schema", {}),
                        },
                    })
            kwargs["tools"] = openai_tools
            kwargs["tool_choice"] = "auto"

        stream = await client.chat.completions.create(**kwargs)
        async for chunk in stream:
            choice = chunk.choices[0] if chunk.choices else None
            if not choice:
                continue
            delta = choice.delta
            yield LLMResponse(
                text=delta.content or "",
                tool_calls=[],
                model=chunk.model,
                finish_reason=choice.finish_reason or "",
            )
