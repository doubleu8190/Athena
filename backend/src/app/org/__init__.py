"""组织架构、通讯录同步与非生产 Mock 种子应用层导出。"""

from app.org.feishu_sync import FeishuOrgSyncService, validate_and_order_departments
from app.org.mock_seed import (
    MOCK_ADMIN_SCOPES,
    MOCK_DEPARTMENTS,
    MOCK_DEPT_FIN_ID,
    MOCK_DEPT_HR_ID,
    MOCK_DEPT_PLATFORM_ID,
    MOCK_DEPT_RD_ID,
    MOCK_DEPT_ROOT_ID,
    MOCK_USER_SPECS,
    build_mock_seed_data,
    seed_mock_organization,
)
from app.org.service import OrgService

__all__ = [
    "FeishuOrgSyncService",
    "MOCK_ADMIN_SCOPES",
    "MOCK_DEPARTMENTS",
    "MOCK_DEPT_FIN_ID",
    "MOCK_DEPT_HR_ID",
    "MOCK_DEPT_PLATFORM_ID",
    "MOCK_DEPT_RD_ID",
    "MOCK_DEPT_ROOT_ID",
    "MOCK_USER_SPECS",
    "OrgService",
    "build_mock_seed_data",
    "seed_mock_organization",
    "validate_and_order_departments",
]
