"""消息配对器 — 以用户消息为边界识别完整对话轮次."""

from __future__ import annotations

from typing import Any


class MessagePairer:
    """将扁平消息列表分割为完整对话轮次."""

    def identify_turns(self, messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        """将消息列表分割为完整的对话轮次，以用户消息为边界.

        Returns:
            轮次列表，每个轮次是一个消息列表
        """
        turns: list[list[dict[str, Any]]] = []
        current_turn: list[dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role", "")

            if role == "system" and not current_turn:
                # 顶层系统消息单独成段
                turns.append([msg])
                continue

            if role == "user" and current_turn:
                # 新用户消息开始，保存当前轮次
                turns.append(current_turn)
                current_turn = []

            current_turn.append(msg)

        if current_turn:
            turns.append(current_turn)

        return turns

    def get_recent_turns(
        self,
        turns: list[list[dict[str, Any]]],
        keep_count: int,
    ) -> tuple[list[list[dict[str, Any]]], list[list[dict[str, Any]]]]:
        """分离旧轮次与最近轮次.

        Returns:
            (旧轮次列表, 最近轮次列表)，最近轮次列表前置系统消息段。
        """
        if len(turns) <= keep_count:
            return [], turns

        system_turns = [t for t in turns if t and t[0].get("role") == "system"]
        dialogue_turns = [t for t in turns if not (t and t[0].get("role") == "system")]

        if len(dialogue_turns) <= keep_count:
            return [], turns

        recent = dialogue_turns[-keep_count:]
        old = dialogue_turns[:-keep_count]
        return old, system_turns + recent
