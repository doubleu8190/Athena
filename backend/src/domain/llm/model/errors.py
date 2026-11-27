"""LLM 调用使用的安全领域异常。"""

import math


class LlmError(Exception):
    """不携带上游正文或凭据的 LLM 异常。"""

    code = "llm_error"

    def __init__(self, code: str | None = None) -> None:
        """创建固定错误码异常。"""
        self.code = code or type(self).code
        super().__init__(self.code)


class LlmCapabilityError(LlmError):
    """Profile 未声明请求所需能力。"""

    code = "llm_capability_unsupported"


class LlmRequestError(LlmError):
    """调用前请求校验失败。"""

    code = "llm_request_invalid"


class LlmProviderError(LlmError):
    """上游 SDK 调用失败，并可携带安全的重试等待提示。"""

    code = "llm_provider_error"

    def __init__(
        self,
        code: str | None = None,
        *,
        retry_after_seconds: float | None = None,
        retry_safe: bool = False,
    ) -> None:
        """保存固定错误码及可选重试等待秒数。"""
        if retry_after_seconds is not None and (
            not math.isfinite(retry_after_seconds) or retry_after_seconds < 0
        ):
            raise ValueError("llm_retry_after_invalid")
        self.retry_after_seconds = retry_after_seconds
        self.retry_safe = retry_safe
        super().__init__(code)


class LlmProtocolError(LlmError):
    """上游响应或事件不符合当前 LLM 协议。"""

    code = "llm_protocol_error"


class LlmRefusalError(LlmError):
    """模型返回拒答。"""

    code = "llm_refusal"


class LlmIncompleteError(LlmError):
    """模型未正常完成响应。"""

    code = "llm_incomplete"
