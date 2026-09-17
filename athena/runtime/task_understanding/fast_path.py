"""确定性任务理解规则，仅覆盖高置信度的常见请求。"""

from __future__ import annotations

from athena.runtime.task_understanding.contracts import (
    ContextRequirement,
    TaskType,
    UserTaskSpec,
)


_GREETING_MARKERS = ("你好", "您好", "谢谢", "感谢", "好的", "收到", "明白")
_MEMORY_MARKERS = ("之前", "上次", "刚才", "我们讨论过", "我说过", "记忆", "历史偏好")
_ACTION_MARKERS = ("运行", "执行", "修复", "修改", "删除", "打开", "创建")


def _matches(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def build_fast_path_task(user_message: str) -> UserTaskSpec | None:
    """按确定性规则推断简单请求的任务理解。

    参数：
        user_message (str): 当前用户消息文本。

    返回值：
        UserTaskSpec | None: 高置信度请求返回任务描述；否则返回 ``None``。

    异常：
        不主动抛出异常。
    """
    text = user_message.strip()
    if not text:
        return UserTaskSpec(
            goal="respond to the empty input",
            task_type="answer",
            context_requirements=["conversation"],
        )
    if _matches(text, _MEMORY_MARKERS):
        requirements: list[ContextRequirement] = ["memory", "conversation"]
        task_type: TaskType = "retrieve"
    elif _matches(text, _GREETING_MARKERS) and len(text) <= 16:
        requirements = ["conversation"]
        task_type = "answer"
    elif _matches(text, _ACTION_MARKERS):
        requirements = ["conversation"]
        task_type = "act"
    else:
        return None
    return UserTaskSpec(
        goal=text[:1000],
        task_type=task_type,
        context_requirements=requirements,
    )
