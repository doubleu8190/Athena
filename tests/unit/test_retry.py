"""LLM 重试策略单元测试."""

import pytest
from unittest.mock import AsyncMock

from athena.core.llm.retry import (
    ErrorCategory,
    RetryConfig,
    RetryResult,
    categorize_error,
    retry_with_backoff,
)


class TestErrorCategorization:
    """错误分类测试."""

    def test_timeout_is_transient(self):
        error = TimeoutError("Connection timed out")
        assert categorize_error(error) == ErrorCategory.TRANSIENT

    def test_connection_error_is_transient(self):
        error = ConnectionError("Connection reset")
        assert categorize_error(error) == ErrorCategory.TRANSIENT

    def test_rate_limit_detected(self):
        error = Exception("429 Too Many Requests")
        assert categorize_error(error) == ErrorCategory.RATE_LIMITED

    def test_context_overflow_detected(self):
        error = Exception("maximum context length exceeded")
        assert categorize_error(error) == ErrorCategory.CONTEXT_OVERFLOW

    def test_auth_error_is_permanent(self):
        error = Exception("Invalid API key")
        assert categorize_error(error) == ErrorCategory.PERMANENT

    def test_unknown_error(self):
        error = Exception("Something weird happened")
        assert categorize_error(error) == ErrorCategory.UNKNOWN


class TestRetryWithBackoff:
    """指数退避重试测试."""

    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self):
        mock_func = AsyncMock(return_value="success")
        result = await retry_with_backoff(mock_func)

        assert result.success is True
        assert result.result == "success"
        assert result.attempts == 1
        mock_func.assert_called_once()

    @pytest.mark.asyncio
    async def test_retry_on_transient_error(self):
        mock_func = AsyncMock(side_effect=[TimeoutError("timeout"), "success"])
        config = RetryConfig(max_attempts=3, min_delay_ms=10, max_delay_ms=50)

        result = await retry_with_backoff(mock_func, retry_config=config)

        assert result.success is True
        assert result.result == "success"
        assert result.attempts == 2

    @pytest.mark.asyncio
    async def test_no_retry_on_permanent_error(self):
        mock_func = AsyncMock(side_effect=Exception("Invalid API key"))

        result = await retry_with_backoff(mock_func)

        assert result.success is False
        assert result.attempts == 1
        assert result.error_category == ErrorCategory.PERMANENT

    @pytest.mark.asyncio
    async def test_max_attempts_exceeded(self):
        mock_func = AsyncMock(side_effect=TimeoutError("timeout"))
        config = RetryConfig(max_attempts=2, min_delay_ms=10, max_delay_ms=50)

        result = await retry_with_backoff(mock_func, retry_config=config)

        assert result.success is False
        assert result.attempts == 2

    @pytest.mark.asyncio
    async def test_on_retry_callback(self):
        mock_func = AsyncMock(side_effect=[TimeoutError("timeout"), "success"])
        mock_callback = AsyncMock()
        config = RetryConfig(max_attempts=3, min_delay_ms=10, max_delay_ms=50)

        await retry_with_backoff(
            mock_func, retry_config=config, on_retry=mock_callback
        )

        mock_callback.assert_called_once()
        call_args = mock_callback.call_args[0]
        assert call_args[0] == 1  # attempt
        assert isinstance(call_args[1], TimeoutError)  # error
        assert isinstance(call_args[2], float)  # delay
