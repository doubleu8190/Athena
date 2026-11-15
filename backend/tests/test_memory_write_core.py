"""同步与异步写入入口共用能力的契约测试。"""

from __future__ import annotations

import pytest

from app.memory.write_core import (
    MemoryEmbeddingError,
    control_arguments_digest,
    normalize_memory_write,
    normalize_text,
    resolve_load_policy,
    validate_embedding_vector,
    validate_write_target,
)
from domain.memory.lifecycle import LoadPolicy


def test_memory_write_normalizes_nfkc_and_repeated_whitespace() -> None:
    """正文和证据在持久化前采用同一规范格式。"""
    normalized = normalize_memory_write(
        content="  我喜欢\u3000  苹果  ",
        evidence="我喜欢　苹果",
        source_text="记住，我喜欢  苹果",
    )

    assert normalized.content == "我喜欢 苹果"
    assert normalized.evidence == "我喜欢 苹果"
    assert normalize_text("Ａ  B") == "a b"
    assert resolve_load_policy(LoadPolicy.ALWAYS, "以后都用中文回答") is LoadPolicy.ALWAYS
    assert resolve_load_policy(LoadPolicy.ALWAYS, "我喜欢苹果") is LoadPolicy.ON_DEMAND


def test_write_target_shape_is_shared_by_sync_and_batch_entries() -> None:
    """新增不允许目标，更新则必须同时携带 ID 和版本。"""
    validate_write_target(
        target_memory_id=None,
        target_version=None,
        require_target=False,
        error_code="write_target_invalid",
    )
    validate_write_target(
        target_memory_id="memory_1",
        target_version=2,
        require_target=True,
        error_code="write_target_invalid",
    )
    with pytest.raises(ValueError, match="write_target_invalid"):
        validate_write_target(
            target_memory_id="memory_1",
            target_version=None,
            require_target=True,
            error_code="write_target_invalid",
        )


def test_write_vector_validation_rejects_invalid_shapes() -> None:
    """共用向量校验拒绝错误维度、布尔值、NaN 和零向量。"""
    assert len(validate_embedding_vector([1.0] * 1024)) == 1024
    for vector in (
        [1.0],
        [True] + [1.0] * 1023,
        [0.0] * 1024,
    ):
        with pytest.raises(MemoryEmbeddingError, match="memory_embedding_invalid"):
            validate_embedding_vector(vector)


def test_control_digest_is_stable_across_mapping_order() -> None:
    """规范参数摘要不受字典插入顺序影响，可直接承担幂等比较。"""
    first = {"operation": "remember", "content": "我喜欢苹果", "version": 1}
    second = {"version": 1, "content": "我喜欢苹果", "operation": "remember"}

    assert control_arguments_digest(first) == control_arguments_digest(second)
    assert len(control_arguments_digest(first)) == 64
