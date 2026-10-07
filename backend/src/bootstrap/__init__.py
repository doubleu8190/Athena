"""应用启动和依赖组装。"""

from .app import create_app
from .dependencies import ApplicationDependencies, build_default_dependencies, build_dependencies
from .lifespan import lifespan

__all__ = ["ApplicationDependencies", "build_default_dependencies", "build_dependencies", "create_app", "lifespan"]
