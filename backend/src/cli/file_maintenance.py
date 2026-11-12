"""停写窗口内执行文件对象引用复核和离线回收。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict

from adapter.file import MinioFileObjectStore, SqlAlchemySessionFileRepository
from app.file import FileObjectMaintenance, ObjectReclamationResult
from infrastructure.database import Database


async def _maintain(
    *,
    database_url: str,
    execute: bool,
    writers_stopped: bool,
    retention_seconds: int,
    page_size: int,
    delete_batch_size: int,
) -> ObjectReclamationResult:
    """组装数据库和对象存储并执行一次从桶起点开始的维护。

    参数:
        database_url: 目标 Athena 业务数据库。
        execute: 是否真正删除无引用候选。
        writers_stopped: 操作者是否已确认全部业务写入者停止。
        retention_seconds: 候选对象的最小年龄。
        page_size: 单页列举对象数量。
        delete_batch_size: 单批最多删除数量。

    返回:
        稳定且不含对象键的对象回收汇总。

    异常:
        配置、数据库或对象存储失败时向调用方传播；普通引用和单项删除失败
        会计入结果中的 ``failed_count``。
    """
    database = Database(database_url)
    store = MinioFileObjectStore.from_environment(deferred=True)
    try:
        await store.start()
        maintenance = FileObjectMaintenance(
            store=store,
            references=SqlAlchemySessionFileRepository(database.session_factory),
            retention_seconds=retention_seconds,
            page_size=page_size,
            delete_batch_size=delete_batch_size,
        )
        return await maintenance.run(
            execute=execute,
            writers_stopped=writers_stopped,
        )
    finally:
        await store.aclose()
        await database.dispose()


def main() -> int:
    """解析离线维护参数并输出不含凭据或对象键的汇总。

    参数:
        无参数。

    返回:
        扫描和删除均无失败时返回 0，否则返回 1。

    异常:
        命令行参数错误由 ``argparse`` 以非零状态退出；运行失败由入口转换为
        稳定的非零状态码。
    """
    parser = argparse.ArgumentParser(
        description="停写窗口内回收未被数据库引用的 Athena 文件对象"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="显式删除无引用候选；默认只做 dry-run",
    )
    parser.add_argument(
        "--confirm-all-writers-stopped",
        action="store_true",
        help="承诺 API、Worker、管理脚本和遗留脚本均已停止写入",
    )
    parser.add_argument("--retention-seconds", type=int, default=86400)
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--delete-batch-size", type=int, default=100)
    arguments = parser.parse_args()

    database_url = os.getenv("ATHENA_DATABASE_URL", "").strip()
    if not database_url:
        parser.error("ATHENA_DATABASE_URL is required")
    if arguments.execute and not arguments.confirm_all_writers_stopped:
        parser.error(
            "--execute requires --confirm-all-writers-stopped"
        )
    try:
        result = asyncio.run(
            _maintain(
                database_url=database_url,
                execute=arguments.execute,
                writers_stopped=arguments.confirm_all_writers_stopped,
                retention_seconds=arguments.retention_seconds,
                page_size=arguments.page_size,
                delete_batch_size=arguments.delete_batch_size,
            )
        )
    except Exception:
        parser.exit(1, "file_maintenance_failed\n")
    summary = {
        "mode": "execute" if arguments.execute else "dry-run",
        **asdict(result),
    }
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 1 if result.failed_count else 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
