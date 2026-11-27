"""组织架构适配器层公共导出。"""

from adapter.org.model import DepartmentModel, DeptAdminScopeModel, UserDepartmentModel
from adapter.org.repository import SqlAlchemyOrgRepository, department_to_domain

__all__ = [
    "DepartmentModel",
    "DeptAdminScopeModel",
    "SqlAlchemyOrgRepository",
    "UserDepartmentModel",
    "department_to_domain",
]
