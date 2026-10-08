"""PostgreSQL adapter options."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PostgresOptions:
    url: str
    pool_size: int = 10
    max_overflow: int = 10
    create_schema: bool = True


__all__ = ["PostgresOptions"]
