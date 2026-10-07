"""Bootstrap dependency graph and lifecycle tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from bootstrap import build_dependencies, create_app


class Resource:
    def __init__(self, name: str, events: list[str], *, fail: bool = False) -> None:
        self.name = name
        self.events = events
        self.fail = fail

    async def start(self) -> None:
        self.events.append(f"start:{self.name}")
        if self.fail:
            raise RuntimeError(self.name)

    async def stop(self) -> None:
        self.events.append(f"stop:{self.name}")


def test_lifespan_starts_and_stops_resources_in_dependency_order() -> None:
    events: list[str] = []
    first = Resource("first", events)
    second = Resource("second", events)
    app = create_app(dependencies=build_dependencies(resources=[first, second]))

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert app.state.startup_complete is True

    assert events == ["start:first", "start:second", "stop:second", "stop:first"]
    assert app.state.startup_complete is False


def test_lifespan_cleans_up_resources_when_startup_fails() -> None:
    events: list[str] = []
    first = Resource("first", events)
    second = Resource("second", events, fail=True)
    app = create_app(dependencies=build_dependencies(resources=[first, second]))

    try:
        with TestClient(app):
            raise AssertionError("startup should fail")
    except RuntimeError as exc:
        assert str(exc) == "second"

    assert events == ["start:first", "start:second", "stop:first"]


def test_dependency_graph_can_supply_service_factory() -> None:
    marker = object()
    app = create_app(
        dependencies=build_dependencies(
            factories={"session_service_factory": lambda: marker},
        )
    )

    assert app.state.dependencies.provider("session_service_factory")() is marker
