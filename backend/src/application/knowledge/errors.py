"""知识库应用层错误。"""


class KnowledgeBaseNotFoundError(LookupError):
    """知识库不存在。"""


class KnowledgeDocumentNotFoundError(LookupError):
    """知识库文档不存在。"""


__all__ = ["KnowledgeBaseNotFoundError", "KnowledgeDocumentNotFoundError"]
