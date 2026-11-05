"""项目适配器。"""

from adapter.project.model import ProjectModel
from adapter.project.repository import SqlAlchemyProjectRepository

__all__ = ["ProjectModel", "SqlAlchemyProjectRepository"]
