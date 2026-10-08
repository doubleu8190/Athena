"""Small configuration contract for HTTP authentication."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AuthOptions:
    enabled: bool = False
    username: str = "athena"
    password: str = "change-me"
    session_secret: str = ""
    session_ttl_seconds: int = 24 * 3600
    host: str = "127.0.0.1"
    port: int = 8000

    @classmethod
    def from_settings(cls, settings: Any) -> "AuthOptions":
        return cls(
            enabled=settings.auth_enabled,
            username=settings.auth_username,
            password=settings.auth_password,
            session_secret=settings.auth_session_secret,
            session_ttl_seconds=settings.auth_session_ttl_hours * 3600,
            host=settings.host,
            port=settings.port,
        )


__all__ = ["AuthOptions"]
