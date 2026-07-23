"""Shared API response helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ApiResponse:
    """Standard API response structure."""
    code: int = 0
    message: str = "success"
    data: Any = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "data": self.data,
        }
        if self.detail:
            result["detail"] = self.detail
        return result


def success(data: Any = None, message: str = "success") -> dict[str, Any]:
    """Build a success response dict."""
    return ApiResponse(code=0, message=message, data=data).to_dict()


def error(code: int, message: str, detail: str = "") -> dict[str, Any]:
    """Build an error response dict."""
    return ApiResponse(code=code, message=message, detail=detail).to_dict()
