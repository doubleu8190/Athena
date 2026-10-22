"""领域层的编排命令拒绝类型。"""

from __future__ import annotations


class PlanCommandRejected(ValueError):
    """记录无法按当前 Plan 状态执行的命令及稳定错误码。"""

    def __init__(self, code: str) -> None:
        """保存稳定拒绝码，供 Root 按业务结果有限恢复。

        参数：code 是非空的机器可读拒绝原因。
        返回：无；空 code 会抛出 ValueError。
        """
        if not code.strip():
            raise ValueError("PlanCommandRejected requires a non-empty code")
        self.code = code
        super().__init__(code)


__all__ = ["PlanCommandRejected"]
