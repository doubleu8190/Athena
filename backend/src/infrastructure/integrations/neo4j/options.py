"""Neo4j adapter options."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Neo4jOptions:
    uri: str
    username: str
    password: str
    database: str = "neo4j"
    max_connection_pool_size: int = 20
    connection_timeout_seconds: float = 5.0


__all__ = ["Neo4jOptions"]
