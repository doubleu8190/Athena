"""Anthropic Claude LLM provider.

Uses Anthropic's Messages API with native tool use support.
"""

from __future__ import annotations

import json
import os
from typing import Any

from anthropic import AsyncAnthropic

from athena.core.llm_provider.base import LLMProvider, LLMResponse
from athena.logging_config import get_logger

logger = get_logger(__name__)


class AnthropicProvider(LLMProvider):
    """Provider for Anthropic Claude models.

    Supports: Claude Opus 4, Claude Sonnet 4, and other Claude models
    with native tool use capability.
    """

    def __init__(
        self,
        api_key_env: str = "ANTHROPIC_API_KEY",
        model: str = "claude-sonnet-4-6",
        base_url: str | None = None,
    ):
        self._api_key_env = api_key_env
        self._model = model
        self._base_url = base_url
        self._client: AsyncAnthropic | None = None

    @property
    def provider_name(self) -> str:
        return "anthropic"

    @property
    def model(self) -> str:
        return self._model

    def _get_client(self) -> AsyncAnthropic:
        if self._client is None:
            api_key = os.environ.get(self._api_key_env, "")
            kwargs = {"api_key": api_key}
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._client = AsyncAnthropic(**kwargs)
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

        # Convert standard messages to Anthropic format
        system_prompt = ""
        anthropic_messages = []

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "system":
                system_prompt += content + "\n"
            elif role == "tool":
                # Tool result message
                anthropic_messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": content,
                    }],
                })
            elif role == "assistant" and msg.get("tool_calls"):
                # Assistant message with tool calls
                tool_blocks = []
                for tc in msg["tool_calls"]:
                    tool_blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": tc["name"],
                        "input": tc.get("arguments", {}),
                    })
                text_blocks = [{"type": "text", "text": content}] if content else []
                anthropic_messages.append({
                    "role": "assistant",
                    "content": text_blocks + tool_blocks,
                })
            else:
                anthropic_messages.append({
                    "role": role,
                    "content": content,
                })

        # Convert tools to Anthropic format
        anthropic_tools = None
        if tools:
            anthropic_tools = []
            for tool in tools:
                anthropic_tools.append({
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("parameters_schema", {}),
                })

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": anthropic_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system_prompt.strip():
            kwargs["system"] = system_prompt.strip()
        if anthropic_tools:
            kwargs["tools"] = anthropic_tools

        try:
            response = await client.messages.create(**kwargs)

            text = ""
            tool_calls = []

            for block in response.content:
                if block.type == "text":
                    text += block.text
                elif block.type == "tool_use":
                    tool_calls.append({
                        "id": block.id,
                        "name": block.name,
                        "arguments": block.input if isinstance(block.input, dict) else {},
                    })

            return LLMResponse(
                text=text,
                tool_calls=tool_calls,
                token_usage={
                    "prompt_tokens": response.usage.input_tokens if response.usage else 0,
                    "completion_tokens": response.usage.output_tokens if response.usage else 0,
                    "total_tokens": (
                        response.usage.input_tokens + response.usage.output_tokens
                        if response.usage else 0
                    ),
                },
                model=response.model,
                finish_reason=response.stop_reason or "stop",
            )
        except Exception as e:
            logger.error("anthropic_generate_failed", error=str(e), model=self._model)
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

        system_prompt = ""
        anthropic_messages = []

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "system":
                system_prompt += content + "\n"
            else:
                anthropic_messages.append({"role": role, "content": content})

        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": anthropic_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        if system_prompt.strip():
            kwargs["system"] = system_prompt.strip()

        async with client.messages.stream(**kwargs) as stream:
            async for event in stream:
                if event.type == "content_block_delta":
                    if event.delta.type == "text_delta":
                        yield LLMResponse(
                            text=event.delta.text,
                            model=self._model,
                            finish_reason="",
                        )
