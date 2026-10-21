"""PostgreSQL-backed adapters for application ports."""

from .mcp import PostgresMCPAdapter

__all__ = [
    "PostgresMCPAdapter",
]
