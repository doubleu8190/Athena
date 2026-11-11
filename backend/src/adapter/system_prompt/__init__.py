"""系统提示词适配器。"""

from adapter.system_prompt.repository import (
    SqlAlchemySystemPromptRepository,
    current_system_prompts,
)

__all__ = ["SqlAlchemySystemPromptRepository", "current_system_prompts"]
