"""LLM 调用使用的安全领域异常。"""


class LlmError(Exception):
    """不携带上游正文或凭据的 LLM 异常。"""

    code = "llm_error"

    def __init__(self, code: str | None = None) -> None:
        """创建固定错误码异常。"""
        self.code = code or type(self).code
        super().__init__(self.code)


class LlmConfigurationError(LlmError):
    """部署配置无效。"""

    code = "llm_configuration_invalid"


class LlmCapabilityError(LlmError):
    """Profile 未声明请求所需能力。"""

    code = "llm_capability_unsupported"


class LlmRequestError(LlmError):
    """调用前请求校验失败。"""

    code = "llm_request_invalid"


class LlmProviderError(LlmError):
    """上游 SDK 调用失败。"""

    code = "llm_provider_error"


class LlmProtocolError(LlmError):
    """上游响应或事件不符合 Responses 协议。"""

    code = "llm_protocol_error"


class LlmRefusalError(LlmError):
    """模型返回拒答。"""

    code = "llm_refusal"


class LlmIncompleteError(LlmError):
    """模型未正常完成响应。"""

    code = "llm_incomplete"


class LlmToolArgumentsError(LlmError):
    """模型生成的工具参数未通过 JSON Schema 校验。"""

    code = "llm_tool_arguments_invalid"


class LlmStructuredOutputError(LlmError):
    """结构化输出未通过解析或 Pydantic 校验。"""

    code = "llm_structured_output_invalid"


class LlmStructuredJsonError(LlmStructuredOutputError):
    """结构化输出不是有效 JSON。"""

    code = "llm_structured_json_invalid"


class LlmStructuredValidationError(LlmStructuredOutputError):
    """结构化 JSON 未通过 Pydantic 验证。"""

    code = "llm_structured_validation_failed"
