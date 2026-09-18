"""Cheap trigger for deciding whether a completed turn merits extraction."""

from __future__ import annotations

from .contracts import CompletedTurn, MemoryWriteTriggerResult


class MemoryWriteTrigger:
    """Conservative rule-based gate; it never calls an LLM."""

    _positive = (
        "记住",
        "以后",
        "默认",
        "统一",
        "不要",
        "我喜欢",
        "我习惯",
        "我的",
        "一直",
        "通常",
        "采用",
        "改成",
        "项目决定",
        "prefer",
        "always",
        "default",
    )
    _negative = (
        "你好",
        "谢谢",
        "感谢",
        "帮我算",
        "请帮我算",
        "计算",
        "请帮我计算",
        "翻译",
        "请翻译",
        "天气",
    )

    def evaluate(self, turn: CompletedTurn) -> MemoryWriteTriggerResult:
        text = (turn.user_text or "").strip()
        if not text:
            return MemoryWriteTriggerResult(should_extract=False, reason="empty_turn")
        signals = [
            signal for signal in self._positive if signal.lower() in text.lower()
        ]
        # Negative signals are deliberately conservative.  A substring such
        # as “计算” or “翻译” is not enough to skip extraction because the
        # same turn may contain an implicit durable decision.  Only a short,
        # stand-alone low-value request is safe to discard here; everything
        # else remains eligible for semantic review by the extractor.
        compact = "".join(text.split()).rstrip("。！？!?．.")
        negative = any(compact == signal for signal in self._negative)
        if negative and len(compact) <= 12 and not signals:
            return MemoryWriteTriggerResult(
                should_extract=False, reason="low_value_turn"
            )
        if signals:
            return MemoryWriteTriggerResult(
                should_extract=True,
                reason="explicit_long_term_signal",
            )
        # Weak signals are allowed through: the extractor, not the trigger,
        # owns semantic judgement. This avoids missing statements such as
        # “我一直都是用 Python 写后端的”.
        return MemoryWriteTriggerResult(
            should_extract=True,
            reason="semantic_review",
        )
