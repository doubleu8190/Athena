"""唯一生产入口。"""

from __future__ import annotations

from bootstrap import create_app


app = create_app()


def run() -> None:
    """Run the target ASGI application with configured host and port."""
    import uvicorn

    from bootstrap.config import get_settings

    settings = get_settings()
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    run()
