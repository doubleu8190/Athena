"""检索通道状态聚合规则。"""

from __future__ import annotations


def aggregate_query_status(*statuses: str) -> str:
    """把多个检索通道状态聚合为一个 Provider 状态。

    至少一个通道成功且至少一个通道失败或超时时返回 `partial`；所有
    参与通道都超时才返回 `timeout`；否则返回 `failed`。

    参数：
    - `statuses`（str）：关键词、向量、重排等通道状态。

    返回：
    - str：`succeeded`、`partial`、`timeout`、`failed` 或 `skipped`。

    异常：
    - 无预期业务异常。
    """
    active = [status for status in statuses if status != "skipped"]
    if not active:
        return "skipped"
    succeeded = any(status == "succeeded" for status in active)
    degraded = any(status in {"failed", "timeout"} for status in active)
    if succeeded and degraded:
        return "partial"
    if succeeded:
        return "succeeded"
    if all(status == "timeout" for status in active):
        return "timeout"
    return "failed"


__all__ = ["aggregate_query_status"]
