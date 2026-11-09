"""模型版本管理安全异常。"""

from domain.llm.model.errors import LlmError


class ModelProfileError(LlmError):
    """模型版本请求或可用性错误。"""

    code = "llm_model_profile_invalid"


class ModelProfileNotFoundError(ModelProfileError):
    """模型版本不存在或不可用。"""

    code = "llm_model_profile_not_found"


class ModelProfileAuthorizationError(ModelProfileError):
    """操作者已失去管理员权限。"""

    code = "llm_model_profile_forbidden"


class ModelProfileConflictError(ModelProfileError):
    """模型配置发生唯一性或默认指向冲突。"""

    code = "llm_model_profile_conflict"


__all__ = [
    "ModelProfileAuthorizationError",
    "ModelProfileConflictError",
    "ModelProfileError",
    "ModelProfileNotFoundError",
]
