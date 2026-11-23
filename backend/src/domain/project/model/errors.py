"""项目管理业务异常。"""


class ProjectNotFoundError(LookupError):
    """项目不存在或已经软删除。"""


class InvalidProjectNameError(ValueError):
    """项目名称不满足展示名称约束。"""


class InvalidProjectInputError(ValueError):
    """项目简介、背景指令、默认技能或局部更新参数不满足业务约束。"""


class ProjectHasSessionsError(ValueError):
    """项目仍有关联的未删除会话。"""


__all__ = [
    "InvalidProjectInputError",
    "InvalidProjectNameError",
    "ProjectHasSessionsError",
    "ProjectNotFoundError",
]
