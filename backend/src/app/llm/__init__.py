"""LLM 应用服务及部署配置。"""

from app.llm.config import LlmProfile, parse_profiles
from app.llm.service import LlmService

__all__ = ["LlmProfile", "LlmService", "parse_profiles"]
