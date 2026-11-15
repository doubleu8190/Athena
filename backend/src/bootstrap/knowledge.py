"""知识库进程共享的模型与存储资源构造。"""

from __future__ import annotations

import os
from dataclasses import dataclass

from adapter.knowledge.storage import MinioOriginalStore, MinioStorageConfig
from infrastructure.embedding import LocalEmbedding, LocalTokenCounter
from infrastructure.embedding.profile import embedding_profile_id
from infrastructure.knowledge import JiebaChineseTokenizer

DEFAULT_EMBEDDING_MODEL = "BAAI/bge-m3"
DEFAULT_EMBEDDING_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"


@dataclass(slots=True)
class KnowledgeModelResources:
    """API 与 Knowledge Worker 共用的知识库模型资源。"""

    embedding: LocalEmbedding
    counter: LocalTokenCounter
    tokenizer: JiebaChineseTokenizer
    profile_id: str


def build_knowledge_models() -> KnowledgeModelResources:
    """按统一环境变量构造 embedding、token 计数与分词资源。

    profile_id 必须由模型与修订版本共同推导，保证写入侧与后台消费侧使用
    同一个向量档案标识，避免混用不同预处理规则的向量。
    """
    model = os.getenv("ATHENA_KNOWLEDGE_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
    revision = os.getenv(
        "ATHENA_KNOWLEDGE_EMBEDDING_REVISION",
        DEFAULT_EMBEDDING_REVISION,
    )
    embedding = LocalEmbedding(
        model,
        revision,
        os.getenv("ATHENA_KNOWLEDGE_EMBEDDING_DEVICE", "cpu"),
    )
    return KnowledgeModelResources(
        embedding=embedding,
        counter=LocalTokenCounter(model, revision),
        tokenizer=JiebaChineseTokenizer(),
        profile_id=embedding_profile_id(model, revision),
    )


def build_knowledge_original_store() -> MinioOriginalStore:
    """构造 MinIO 原件存储；配置缺失或非法时立即失败。"""
    return MinioOriginalStore.from_environment()


def validate_knowledge_storage_configuration() -> None:
    """在创建数据库、Redis 或模型资源前快速校验存储配置。"""
    MinioStorageConfig.from_environment()


__all__ = [
    "KnowledgeModelResources",
    "build_knowledge_models",
    "build_knowledge_original_store",
    "validate_knowledge_storage_configuration",
]
