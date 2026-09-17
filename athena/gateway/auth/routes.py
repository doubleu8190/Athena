from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from fastapi import APIRouter, HTTPException, Response, Request
from pydantic import BaseModel
from athena.config.settings import get_settings
from athena.contracts.errors import ErrorDetail

router = APIRouter(prefix="/auth", tags=["auth"])
SESSION_COOKIE_NAME = "athena_session"


class LoginRequest(BaseModel):
    """登录请求体。"""

    username: str
    password: str


def _sign_session_token_payload(payload: str, secret: str) -> str:
    """使用服务端密钥为令牌载荷生成 HMAC-SHA256 签名。

    参数:
        payload (str): 待签名的令牌正文。
        secret (str): 服务端签名密钥。
    返回值:
        str: 小写十六进制签名。
    异常:
        不抛出业务异常。
    """
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def _create_session_token(username: str, secret: str, ttl: int) -> str:
    """生成包含用户名和过期时间的无状态会话令牌。

    参数:
        username (str): 登录用户名。
        secret (str): 服务端签名密钥。
        ttl (int): 有效期，单位为秒，必须为正数。
    返回值:
        str: ``正文.签名`` 格式的 URL 安全令牌。
    异常:
        JSON 序列化失败时传播 ``TypeError``。
    """
    body = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"u": username, "exp": int(time.time()) + ttl}, separators=(",", ":")
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    return body + "." + _sign_session_token_payload(body, secret)


def is_authenticated(request: Request) -> bool:
    """校验请求是否携带有效的认证 Cookie。

    参数:
        request (Request): 当前 HTTP 请求。
    返回值:
        bool: 未启用认证或令牌有效时返回 ``True``，否则返回 ``False``。
    异常:
        令牌解析错误会被视为认证失败，不向调用方抛出。
    """
    settings = get_settings()
    if not settings.auth_enabled:
        return True
    raw = request.cookies.get(SESSION_COOKIE_NAME, "")
    if "." not in raw or not settings.auth_session_secret:
        return False
    body, sig = raw.rsplit(".", 1)
    if not hmac.compare_digest(
        sig, _sign_session_token_payload(body, settings.auth_session_secret)
    ):
        return False
    try:
        padded = body + "=" * (-len(body) % 4)
        return json.loads(base64.urlsafe_b64decode(padded)).get("exp", 0) > time.time()
    except Exception:
        return False


@router.post("/login")
async def login(
    req: LoginRequest, response: Response, request: Request
) -> dict[str, str]:
    """验证凭据并写入 HttpOnly 会话 Cookie。

    参数:
        req (LoginRequest): 用户名和密码。
        response (Response): 用于写入 Cookie 的响应对象。
        request (Request): 用于判断 HTTPS 的当前请求。
    返回值:
        dict[str, str]: 成功时返回 ``{"status": "ok"}``。
    异常:
        HTTP异常: 凭据错误时返回 401，认证密钥不安全时返回 503。
    """
    settings = get_settings()
    if not hmac.compare_digest(
        req.username, settings.auth_username
    ) or not hmac.compare_digest(req.password, settings.auth_password):
        raise HTTPException(status_code=401, detail=ErrorDetail.INVALID_CREDENTIALS)
    if settings.auth_enabled and len(settings.auth_session_secret) < 32:
        raise HTTPException(
            status_code=503, detail=ErrorDetail.AUTH_SECRET_NOT_CONFIGURED
        )
    response.set_cookie(
        SESSION_COOKIE_NAME,
        _create_session_token(
            req.username,
            settings.auth_session_secret or "disabled",
            settings.auth_session_ttl_hours * 3600,
        ),
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        max_age=settings.auth_session_ttl_hours * 3600,
        path="/",
    )
    return {"status": "ok"}


@router.post("/logout")
async def logout(response: Response) -> dict[str, str]:
    """删除当前会话 Cookie。

    参数:
        response (Response): 用于清除 Cookie 的响应对象。
    返回值:
        dict[str, str]: 返回 ``{"status": "ok"}``。
    异常:
        不抛出业务异常。
    """
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"status": "ok"}
