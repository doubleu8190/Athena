"""记忆事实组并发写入错误。"""


class MemoryStateChangedError(RuntimeError):
    """Resolver 读取后事实组发生变化，需要重新判定。"""


class MemorySuppressedError(RuntimeError):
    """候选来源已被用户遗忘，不得再次写入。"""


__all__ = ["MemoryStateChangedError", "MemorySuppressedError"]
