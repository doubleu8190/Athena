"""Explicit dependency graph for the restructured application.

The bootstrap layer owns assembly.  Domain and application packages only see
ports, while this module keeps service instances and process resources in one place.
The default graph is intentionally empty so importing the application never
opens a database connection or starts a worker.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ApplicationDependencies:
    """The process-level dependency graph consumed by ``bootstrap.app``.

    ``services`` contains application dependencies keyed by the names
    accepted by :func:`create_app`.  ``resources`` are started before the app
    accepts requests and stopped in reverse order during shutdown.
    """

    services: Mapping[str, Any] = field(default_factory=dict)
    resources: Sequence[Any] = field(default_factory=tuple)
    routers: Sequence[Any] = field(default_factory=tuple)

    def service(self, name: str) -> Any | None:
        """Return a registered dependency, if the graph contains one."""
        return self.services.get(name)


def build_dependencies(
    *,
    services: Mapping[str, Any] | None = None,
    resources: Sequence[Any] = (),
    routers: Sequence[Any] = (),
) -> ApplicationDependencies:
    """Create an immutable dependency graph from bootstrap inputs."""
    return ApplicationDependencies(
        services=dict(services or {}),
        resources=tuple(resources),
        routers=tuple(routers),
    )


def build_default_dependencies() -> ApplicationDependencies:
    """Build the production dependency graph backed by PostgreSQL.

    In-memory adapters are test doubles and are assembled under
    ``backend.tests.fakes``. Production startup must fail through the
    PostgreSQL resource lifecycle when its database is unavailable instead of
    silently losing state in process memory.
    """
    import os

    from .postgres import build_postgres_dependencies
    from configuration import get_settings

    settings = get_settings()
    if os.getenv("ATHENA_WORKERS_ENABLED", "").lower() in {"1", "true", "yes", "on"}:
        settings = settings.model_copy(update={"workers_enabled": True})
    return build_postgres_dependencies(settings)


__all__ = ["ApplicationDependencies", "build_dependencies", "build_default_dependencies"]
