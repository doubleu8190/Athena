"""将一个已有且可用的账号提升为管理员。"""

from __future__ import annotations

import argparse
import asyncio
import os

from adapter.auth.repository.auth import SqlAlchemyAuthRepository
from app.auth.service import AuthService
from domain.auth import AdminPromotionError
from infrastructure.database import Database
from infrastructure.security.password import Argon2PasswordHasher


async def _promote(email: str, database_url: str) -> None:
    """组装认证用例并执行本地管理员提权。

    参数：email 是目标账号邮箱；database_url 是部署配置的数据库连接。
    返回：成功时不返回值。
    异常：目标账号不可用或数据库操作失败时抛出异常。
    """
    database = Database(database_url)
    try:
        service = AuthService(
            SqlAlchemyAuthRepository(database.session_factory),
            Argon2PasswordHasher(),
        )
        await service.promote_admin_by_email(email)
    finally:
        await database.dispose()


def main() -> int:
    """读取唯一目标邮箱并执行管理员提权。

    参数：无。
    返回：成功返回 0；配置或目标账号无效时返回 1。
    异常：命令行参数错误由 argparse 显示并以非零状态退出。
    """
    parser = argparse.ArgumentParser(description="提升现有 Athena 账号为管理员")
    parser.add_argument("--email", required=True, help="已有账号的邮箱")
    arguments = parser.parse_args()
    database_url = os.getenv("ATHENA_DATABASE_URL", "").strip()
    if not database_url:
        parser.error("ATHENA_DATABASE_URL is required")
    try:
        asyncio.run(_promote(arguments.email, database_url))
    except AdminPromotionError:
        parser.exit(1, "admin_promotion_unavailable\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
