"""创建或重置本地应急 super_admin 超级管理员账号。"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from adapter.auth.repository.auth import SqlAlchemyAuthRepository
from app.auth.service import AuthService
from domain.auth import (
    AdminPromotionError,
    InvalidDisplayNameError,
    InvalidEmailError,
    InvalidPasswordError,
    User,
)
from infrastructure.database import Database
from infrastructure.security.password import Argon2PasswordHasher


async def bootstrap_super_admin(
    *,
    email: str,
    password: str,
    database_url: str,
    display_name: str | None = None,
) -> tuple[User, bool]:
    """组装认证服务并幂等创建或重置本地应急 super_admin 账号。"""
    database = Database(database_url)
    try:
        service = AuthService(
            SqlAlchemyAuthRepository(database.session_factory),
            Argon2PasswordHasher(),
        )
        return await service.bootstrap_super_admin(
            email,
            password,
            display_name=display_name,
        )
    finally:
        await database.dispose()


def _resolve_password(arguments: argparse.Namespace) -> str:
    """按参数、标准输入或环境变量解析应急超管密码，不在任何输出中回显明文。"""
    if arguments.password_stdin:
        return sys.stdin.read().rstrip("\r\n")
    if arguments.password is not None:
        return str(arguments.password)
    env_password = os.getenv("ATHENA_SUPER_ADMIN_PASSWORD")
    if env_password is not None and env_password:
        return env_password
    raise ValueError("password_required")


def main(argv: list[str] | None = None) -> int:
    """读取目标邮箱与新密码并创建或重置本地 super_admin 应急账号。"""
    parser = argparse.ArgumentParser(
        description="创建或重置本地应急 Athena super_admin 账号（重置时自动撤销其全部历史会话）"
    )
    parser.add_argument("--email", required=True, help="应急超级管理员邮箱")
    parser.add_argument(
        "--password",
        default=None,
        help="新密码（至少 12 位；也可通过 ATHENA_SUPER_ADMIN_PASSWORD 环境变量提供）",
    )
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="从标准输入读取密码，避免明文密码出现在命令行历史中",
    )
    parser.add_argument("--display-name", default=None, help="可选显示名（默认 Super Admin）")
    arguments = parser.parse_args(argv)

    database_url = os.getenv("ATHENA_DATABASE_URL", "").strip()
    if not database_url:
        parser.error("ATHENA_DATABASE_URL is required")
    try:
        password = _resolve_password(arguments)
    except ValueError:
        parser.error(
            "password is required via --password, --password-stdin, or ATHENA_SUPER_ADMIN_PASSWORD"
        )

    try:
        user, created = asyncio.run(
            bootstrap_super_admin(
                email=arguments.email,
                password=password,
                database_url=database_url,
                display_name=arguments.display_name,
            )
        )
    except (
        InvalidEmailError,
        InvalidPasswordError,
        InvalidDisplayNameError,
        AdminPromotionError,
    ) as error:
        parser.exit(1, f"{error}\n")

    action = "super_admin_created" if created else "super_admin_reset"
    print(f"{action} user_id={user.id} email={user.email}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
