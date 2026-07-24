"""Dynamic prompt loader — reads system prompts from Markdown files.

All prompts are stored under ``<DATA_DIR>/prompt/`` as ``.md`` files.
Loaded content is cached in-process after the first read.
"""

from __future__ import annotations

import os
from pathlib import Path

from athena.logging_config import get_logger

logger = get_logger(__name__)

PROMPT_DIR = Path(os.environ.get("DATA_DIR", "/data")) / "prompt"

_cache: dict[str, str] = {}


def load_prompt(filename: str) -> str:
    """Load a prompt from *filename* under :data:`PROMPT_DIR`.

    Results are cached in-memory after the first successful read.
    Returns an empty string and logs an error if the file is missing
    or cannot be decoded.
    """
    if filename in _cache:
        return _cache[filename]

    path = PROMPT_DIR / filename
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.error("prompt_file_not_found", path=str(path))
        return ""
    except Exception as exc:
        logger.error("prompt_file_read_error", path=str(path), error=str(exc))
        return ""

    if not text.strip():
        logger.warning("prompt_file_empty", path=str(path))

    _cache[filename] = text
    return text


def clear_cache() -> None:
    """Evict all cached prompts (useful for testing)."""
    _cache.clear()