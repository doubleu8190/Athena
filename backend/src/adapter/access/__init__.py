"""统一权限引擎 SQLAlchemy 适配层导出。"""

from adapter.access.model import ResourceVisibilityGrantModel
from adapter.access.repository import (
    SqlAlchemyAccessControlRepository,
    build_readable_resource_clause,
    delete_resource_grants_in_session,
    grant_to_domain,
    list_active_department_ids_in_session,
    load_grants_map_in_session,
    load_user_access_snapshot_in_session,
    sync_resource_grants_in_session,
)

__all__ = [
    "ResourceVisibilityGrantModel",
    "SqlAlchemyAccessControlRepository",
    "build_readable_resource_clause",
    "delete_resource_grants_in_session",
    "grant_to_domain",
    "list_active_department_ids_in_session",
    "load_grants_map_in_session",
    "load_user_access_snapshot_in_session",
    "sync_resource_grants_in_session",
]
