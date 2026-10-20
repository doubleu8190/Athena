"""嵌入模型设备回退用例。"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from infrastructure.integrations.llm.embedding_provider import ConfiguredEmbeddingProvider
from infrastructure.integrations.llm.options import EmbeddingOptions


@pytest.mark.asyncio
async def test_sentence_transformer_falls_back_to_cpu_when_mps_is_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    devices: list[str] = []

    class FakeSentenceTransformer:
        def __init__(self, _model: str, *, device: str) -> None:
            devices.append(device)
            if device == "mps":
                raise RuntimeError(
                    "The MPS backend is supported on macOS 14.0+ only"
                )

        def encode(self, _texts: list[str], **_kwargs: object) -> list[object]:
            class Vector:
                def astype(self, _dtype: type[float]) -> "Vector":
                    return self

                def tolist(self) -> list[float]:
                    return [0.1, 0.2]

            return [Vector()]

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=FakeSentenceTransformer),
    )
    provider = ConfiguredEmbeddingProvider(
        EmbeddingOptions(device="mps", dimension=2)
    )

    embedding = await provider.embed_query("苹果")

    assert devices == ["mps", "cpu"]
    assert embedding == [0.1, 0.2]
