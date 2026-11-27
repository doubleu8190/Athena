"""进程存活检查。"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class HealthResponse(BaseModel):
    """存活检查响应，不代表外部依赖已就绪。"""

    # 响应 DTO 不设置默认值，确保 OpenAPI 导出为必填属性而非可选属性。
    status: Literal["ok"]


@router.get("/health", response_model=HealthResponse, tags=["health"])
def check_health() -> HealthResponse:
    """检查 HTTP 进程是否能够响应。

    参数：无。
    返回：HealthResponse，表示进程存活。
    异常：不主动抛出业务异常。
    """
    return HealthResponse(status="ok")
