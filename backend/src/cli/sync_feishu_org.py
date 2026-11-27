"""手动或定时任务执行飞书通讯录组织树原子同步 CLI。"""

from __future__ import annotations

import argparse
import asyncio
import os

from adapter.org import SqlAlchemyOrgRepository
from app.org import FeishuOrgSyncService
from bootstrap.config import env_flag, is_production_env
from domain.org import FeishuSyncSummary, OrgError
from infrastructure.database import Database
from infrastructure.feishu import FeishuClient, FeishuConfig, FeishuError


async def run_feishu_org_sync(
    *,
    database_url: str,
    config: FeishuConfig,
) -> FeishuSyncSummary:
    """组装数据库仓储与飞书客户端，执行一次完整的通讯录组织树原子同步。"""
    database = Database(database_url)
    feishu_client = FeishuClient(config)
    try:
        org_repository = SqlAlchemyOrgRepository(database.session_factory)
        sync_service = FeishuOrgSyncService(feishu_client, org_repository)
        return await sync_service.sync()
    finally:
        await feishu_client.aclose()
        await database.dispose()


def main(argv: list[str] | None = None) -> int:
    """读取数据库与飞书环境配置并执行通讯录同步。"""
    parser = argparse.ArgumentParser(description="同步飞书通讯录部门树与在职/离职员工状态")
    parser.parse_args(argv)

    database_url = os.getenv("ATHENA_DATABASE_URL", "").strip()
    if not database_url:
        parser.error("ATHENA_DATABASE_URL is required")
    if not env_flag("ATHENA_FEISHU_ENABLED", default=False):
        parser.exit(1, "feishu_not_enabled\n")

    try:
        config = FeishuConfig.from_environment(is_production=is_production_env())
        summary = asyncio.run(run_feishu_org_sync(database_url=database_url, config=config))
    except (FeishuError, OrgError) as error:
        parser.exit(1, f"{error.code}\n")

    print(
        "feishu_org_sync_completed "
        f"departments_upserted={summary.departments_upserted} "
        f"departments_archived={summary.departments_archived} "
        f"users_upserted={summary.users_upserted} "
        f"users_disabled={summary.users_disabled} "
        f"sessions_revoked={summary.sessions_revoked}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
