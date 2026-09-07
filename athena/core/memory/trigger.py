"""Cheap trigger for deciding whether a completed turn merits extraction."""

from __future__ import annotations

from .contracts import CompletedTurn, MemoryTriggerResult


class MemoryTrigger:
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
        "你好", "谢谢", "感谢", "帮我算", "请帮我算", "计算", "请帮我计算",
        "翻译", "请翻译", "天气",
    )

    def evaluate(self, turn: CompletedTurn) -> MemoryTriggerResult:
        text = (turn.user_text or "").strip()
        if not text:
            return MemoryTriggerResult(should_extract=False, reason="empty_turn")
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
            return MemoryTriggerResult(
                should_extract=False, signals=["low_value"], reason="low_value_turn"
            )
        if signals:
            return MemoryTriggerResult(
                should_extract=True,
                score=1.0,
                signals=signals,
                reason="explicit_long_term_signal",
            )
        # Weak signals are allowed through: the extractor, not the trigger,
        # owns semantic judgement. This avoids missing statements such as
        # “我一直都是用 Python 写后端的”.
        return MemoryTriggerResult(
            should_extract=True,
            score=0.25,
            signals=["weak_signal"],
            reason="semantic_review",
        )
