"""唯一生产入口。"""

from __future__ import annotations

from bootstrap import build_default_dependencies, create_app
import configuration.settings as runtime_config
from interfaces.http.auth import AuthOptions


_settings = runtime_config.get_settings()
app = create_app(
    dependencies=build_default_dependencies(),
    auth_options=AuthOptions.from_settings(_settings),
)


def run() -> None:
    """Run the target ASGI application with configured host and port."""
    import uvicorn

    settings = runtime_config.get_settings()
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    run()
