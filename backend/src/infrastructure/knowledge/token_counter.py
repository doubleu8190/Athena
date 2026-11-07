"""Local tokenizer-backed knowledge chunk budget counter."""

from __future__ import annotations

import asyncio
from typing import Any


class LocalKnowledgeTokenCounter:
    """Count model tokens with a pinned, locally cached Hugging Face tokenizer."""

    def __init__(self, model: str, revision: str) -> None:
        self.model = model
        self.revision = revision
        self._tokenizer: Any = None

    async def start(self) -> None:
        await asyncio.to_thread(self._load)

    def _load(self) -> None:
        from transformers import AutoTokenizer

        self._tokenizer = AutoTokenizer.from_pretrained(
            self.model, revision=self.revision, local_files_only=True
        )  # type: ignore[no-untyped-call]

    def count(self, text: str) -> int:
        if self._tokenizer is None:
            self._load()
        return len(self._tokenizer.encode(text, add_special_tokens=False))

    def close(self) -> None:
        self._tokenizer = None
