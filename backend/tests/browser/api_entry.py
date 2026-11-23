"""以真实 main.create_app 和 lifespan 启动浏览器 E2E API。"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from typing import Any

import uvicorn
from sqlalchemy import func, select, text

from browser.database_scope import install_search_path


async def _probe_database(probe_file: Path, expected_password: str) -> None:
    """在真实 Uvicorn 启动前验证测试引擎确实进入隔离 schema。"""
    from adapter.auth.model import UserModel
    from bootstrap.database import build_database
    from infrastructure.security.password import Argon2PasswordHasher

    database = build_database(os.environ["ATHENA_DATABASE_URL"])
    try:
        async with database.session_factory() as session:
            search_path = str(await session.scalar(text("show search_path")))
            users = int(await session.scalar(select(func.count()).select_from(UserModel)) or 0)
            rows = (
                await session.execute(
                    select(UserModel.email, UserModel.status, UserModel.password_hash)
                )
            ).all()
        hasher = Argon2PasswordHasher()
        checks = []
        for email, status, password_hash in rows:
            matched = password_hash is not None and hasher.verify_password(
                expected_password, password_hash
            )
            checks.append(f"{email}:status={status}:password={str(matched).lower()}")
        probe_file.write_text(
            f"search_path={search_path}\nusers={users}\n" + "\n".join(checks) + "\n",
            encoding="utf-8",
        )
        probe_file.chmod(0o600)
    finally:
        await database.dispose()


def _install_auth_trace(path: Path) -> None:
    """在测试进程内记录安全布尔值，定位真实登录链而不暴露密码或摘要。"""
    from adapter.auth.repository import SqlAlchemyAuthRepository
    from infrastructure.security.password import Argon2PasswordHasher

    original_user_lookup = SqlAlchemyAuthRepository.get_user_by_email
    original_verify = Argon2PasswordHasher.verify_password

    async def traced_lookup(self: Any, email: str) -> Any:
        user = await original_user_lookup(self, email)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"user_lookup={email}:found={str(user is not None).lower()}\n")
        path.chmod(0o600)
        return user

    def traced_verify(self: Any, password: str, password_hash: str) -> bool:
        matched = original_verify(self, password, password_hash)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"password_verify={str(matched).lower()}\n")
        path.chmod(0o600)
        return matched

    SqlAlchemyAuthRepository.get_user_by_email = traced_lookup  # type: ignore[method-assign]
    Argon2PasswordHasher.verify_password = traced_verify  # type: ignore[method-assign]


def main() -> None:
    """读取隔离 schema 和端口，启动正常应用组合根。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", required=True)
    parser.add_argument("--port", type=int, default=8011)
    parser.add_argument("--probe-file", type=Path)
    args = parser.parse_args()
    install_search_path(args.schema)
    if args.probe_file is not None:
        expected_password = os.getenv("ATHENA_E2E_PROBE_PASSWORD", "")
        asyncio.run(_probe_database(args.probe_file, expected_password))
        os.environ.pop("ATHENA_E2E_PROBE_PASSWORD", None)
    trace_path = os.getenv("ATHENA_E2E_AUTH_TRACE_FILE")
    if trace_path:
        _install_auth_trace(Path(trace_path))
    from main import create_app

    os.environ["ATHENA_E2E_SCHEMA"] = args.schema
    uvicorn.run(
        create_app(),
        host="127.0.0.1",
        port=args.port,
        lifespan="on",
        access_log=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
