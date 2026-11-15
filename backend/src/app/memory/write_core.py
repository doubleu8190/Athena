"""同步控制和异步提取共用的记忆写入基础能力。"""

from __future__ import annotations

import hashlib
import math
import unicodedata
from dataclasses import dataclass

from app.memory.sensitive import raise_for_sensitive_texts
from common.json import dumps
from domain.llm.ports import EmbeddingPort
from domain.memory.lifecycle import LoadPolicy

_PERSISTENT_POLICY_MARKERS = (
    "以后",
    "总是",
    "每次",
    "始终",
    "本项目",
    "项目内",
    "默认",
    "always",
    "whenever",
)


class MemoryEmbeddingError(ValueError):
    """向量数量或内容不满足写入契约。"""


@dataclass(frozen=True, slots=True)
class NormalizedMemoryWrite:
    """两个写入入口共同需要的规范正文和证据。"""

    content: str
    evidence: str


def normalize_text(value: str) -> str:
    """按 Unicode、空白和大小写规则生成稳定比较文本。

    参数：
    - `value`：用户原话、证据或正文。

    返回：
    - str：适合比较的规范文本。

    异常：
    - TypeError：输入不是字符串时由 `unicodedata.normalize` 抛出。
    """
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def normalized_persisted_text(value: str) -> str:
    """规范化需要保留显示大小的正文和证据文本。

    参数：
    - `value`：待规范化正文或证据。

    返回：
    - str：保留大小写、折叠 Unicode 和空白的文本。

    异常：
    - TypeError：输入不是字符串时由 `unicodedata.normalize` 抛出。
    """
    return " ".join(unicodedata.normalize("NFKC", value).split())


def control_arguments_digest(arguments: dict[str, object]) -> str:
    """计算同步控制规范参数的稳定 SHA-256 摘要。

    参数：
    - `arguments`：只包含控制身份、规范正文、范围和目标的 JSON 对象。

    返回：
    - str：按键排序、无多余分隔空白的 UTF-8 JSON 的 SHA-256 十六进制摘要。

    异常：
    - TypeError、ValueError：参数不能稳定序列化为严格 JSON 时传递。
    """
    canonical = dumps(arguments, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def evidence_is_grounded(evidence: str, source_text: str) -> bool:
    """确认规范化后的证据能在同一可信来源中连续定位。

    参数：
    - `evidence`：待校验证据。
    - `source_text`：入口读取的可信用户原文。

    返回：
    - bool：证据非空且被来源包含时为 True。

    异常：
    - 无预期业务异常。
    """
    normalized_evidence = normalize_text(evidence)
    return bool(normalized_evidence) and normalized_evidence in normalize_text(source_text)


def resolve_load_policy(
    requested: LoadPolicy,
    source_text: str,
) -> LoadPolicy:
    """只允许直接持续适用证据把请求提升为每轮加载。

    参数：
    - `requested`：模型或工具建议的加载策略。
    - `source_text`：入口已授权并绑定的事实证据。

    返回：
    - LoadPolicy：有直接持续适用证据时为 always，否则为 on_demand。

    异常：
    - 无预期业务异常。
    """
    if requested is LoadPolicy.ON_DEMAND:
        return LoadPolicy.ON_DEMAND
    normalized = normalize_text(source_text)
    if any(marker.casefold() in normalized for marker in _PERSISTENT_POLICY_MARKERS):
        return LoadPolicy.ALWAYS
    return LoadPolicy.ON_DEMAND


def normalize_memory_write(
    *,
    content: str,
    evidence: str,
    source_text: str,
    sensitive_values: tuple[str, ...] = (),
) -> NormalizedMemoryWrite:
    """执行两个写入入口共用的长度、证据、敏感和规范化校验。

    参数：
    - `content`：待持久化正文。
    - `evidence`：正文对应的直接用户原文。
    - `source_text`：入口已授权的完整用户来源。
    - `sensitive_values`：入口额外需要检查的持久化文本。

    返回：
    - NormalizedMemoryWrite：规范正文和证据。

    异常：
    - ValueError：正文或证据为空、超长、敏感或不包含证据。
    """
    normalized_content = normalized_persisted_text(content)
    normalized_evidence = normalized_persisted_text(evidence)
    if (
        not 1 <= len(normalized_content) <= 2_000
        or not 1 <= len(normalized_evidence) <= 2_000
    ):
        raise ValueError("memory_write_content_invalid")
    raise_for_sensitive_texts((normalized_content, normalized_evidence, *sensitive_values))
    if not evidence_is_grounded(normalized_evidence, source_text):
        raise ValueError("memory_write_evidence_invalid")
    return NormalizedMemoryWrite(
        content=normalized_content,
        evidence=normalized_evidence,
    )


def validate_write_target(
    *,
    target_memory_id: str | None,
    target_version: int | None,
    require_target: bool,
    error_code: str,
) -> None:
    """校验新增或更新决策是否携带完整且一致的目标身份。

    参数：
    - `target_memory_id`：待更新记忆 ID。
    - `target_version`：待更新记忆版本。
    - `require_target`：当前决策是否必须更新已有记忆。
    - `error_code`：入口使用的稳定业务错误码。

    返回：
    - None。

    异常：
    - ValueError：目标要求与提供字段不一致时抛出指定错误码。
    """
    has_target = target_memory_id is not None or target_version is not None
    complete_target = target_memory_id is not None and target_version is not None
    if (require_target and not complete_target) or (not require_target and has_target):
        raise ValueError(error_code)


def validate_embedding_vector(vector: list[float]) -> tuple[float, ...]:
    """校验写入向量维度、有限值和非零范数。

    参数：
    - `vector`：嵌入模型返回的向量。

    返回：
    - tuple[float, ...]：可持久化的不可变向量。

    异常：
    - MemoryEmbeddingError：向量维度、数值或范数无效。
    """
    if (
        len(vector) != 1_024
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in vector
        )
        or not any(value != 0 for value in vector)
    ):
        raise MemoryEmbeddingError("memory_embedding_invalid")
    return tuple(float(value) for value in vector)


async def prepare_write_embeddings(
    embedder: EmbeddingPort,
    texts: list[str],
) -> tuple[tuple[float, ...], ...]:
    """一次生成与正文顺序一致且已校验的写入向量。

    参数：
    - `embedder`：向量模型端口。
    - `texts`：待写入正文。

    返回：
    - tuple[tuple[float, ...], ...]：与输入一一对应的向量。

    异常：
    - MemoryEmbeddingError：向量数量、维度、数值或范数无效。
    """
    if not texts:
        return ()
    embedded = await embedder.embed(texts)
    if len(embedded) != len(texts):
        raise MemoryEmbeddingError("memory_embedding_invalid")
    return tuple(validate_embedding_vector(vector) for vector in embedded)


__all__ = [
    "MemoryEmbeddingError",
    "NormalizedMemoryWrite",
    "control_arguments_digest",
    "evidence_is_grounded",
    "normalize_memory_write",
    "normalize_text",
    "normalized_persisted_text",
    "prepare_write_embeddings",
    "resolve_load_policy",
    "validate_embedding_vector",
    "validate_write_target",
]
