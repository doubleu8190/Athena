"""跨层共用的秘密用途枚举、密文大小上限与加解密异常基类。"""

from __future__ import annotations

from enum import StrEnum

MAX_SECRET_BYTES = 16 * 1024


class SecretPurpose(StrEnum):
    """秘密集合绑定的业务用途（用于构造不可跨用途复用的 AAD）。"""

    HTTP_HEADERS = "http_headers"
    STDIO_ENVIRONMENT = "stdio_environment"
    LLM_API_KEY = "llm_api_key"


class SecretCipherError(RuntimeError):
    """秘密封装、密钥配置或解封失败（异常文本仅包含固定安全错误码）。"""


__all__ = [
    "MAX_SECRET_BYTES",
    "SecretCipherError",
    "SecretPurpose",
]
