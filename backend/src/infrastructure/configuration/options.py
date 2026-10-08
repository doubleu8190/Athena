"""Public configuration snapshots exposed through query ports."""

from dataclasses import dataclass, field

from infrastructure.integrations.llm.options import LLMProviderOptions


@dataclass(frozen=True, slots=True)
class PublicRuntimeOptions:
    host: str
    port: int
    debug: bool
    database_backend: str
    workers_enabled: bool


@dataclass(frozen=True, slots=True)
class PublicConfiguration:
    providers: tuple[LLMProviderOptions, ...] = field(default_factory=tuple)
    runtime: PublicRuntimeOptions = field(
        default_factory=lambda: PublicRuntimeOptions(
            host="127.0.0.1", port=8000, debug=True,
            database_backend="postgres", workers_enabled=False,
        )
    )


__all__ = ["PublicConfiguration", "PublicRuntimeOptions"]
