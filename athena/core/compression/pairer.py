"""消息配对器 — 识别完整对话轮次，保护 assistant-tool 消息对的完整性.

一个完整轮次（turn）：
- 以 user 消息开始
- 包含 assistant 响应（含/不含工具调用）
- 若有工具调用，则包含对应的 tool 消息
"""

from __future__ import annotations

from athena.types import JSONValue


class MessagePairer:
    """将扁平消息列表分割为完整对话轮次."""

    def identify_turns(self, messages: list[dict[str, JSONValue]]) -> list[list[dict[str, JSONValue]]]:
        """将消息列表分割为完整的对话轮次.

        Returns:
            轮次列表，每个轮次是一个消息列表
        """
        turns: list[list[dict[str, JSONValue]]] = []
        current_turn: list[dict[str, JSONValue]] = []

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

            if self._is_turn_complete(current_turn):
                turns.append(current_turn)
                current_turn = []

        if current_turn:
            turns.append(current_turn)

        return turns

    def _is_turn_complete(self, turn: list[dict[str, JSONValue]]) -> bool:
        """判断当前轮次是否完整."""
        if len(turn) < 2:
            return False

        last_msg = turn[-1]
        last_role = last_msg.get("role", "")

        if last_role == "assistant":
            # 仅在没有任何待处理 tool_calls 时才算完成
            if not self._has_pending_tool_calls(turn):
                # assistant 消息 content 为空但无 tool_calls，视为未完成占位消息
                if last_msg.get("content", "") == "":
                    return False
                return True
            return False

        # tool 消息本身不直接结束轮次，需要等待 assistant 后续总结消息
        return False

    def _has_pending_tool_calls(self, turn: list[dict[str, JSONValue]]) -> bool:
        """检查当前轮次是否存在未处理完成的 tool_calls."""
        tool_call_ids: set[str] = set()
        tool_result_ids: set[str] = set()

        for msg in turn:
            role = msg.get("role", "")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    tc_id = tc.get("id", "")
                    if tc_id:
                        tool_call_ids.add(tc_id)
            if role == "tool":
                tc_id = msg.get("tool_call_id", "")
                if tc_id:
                    tool_result_ids.add(tc_id)

        return bool(tool_call_ids - tool_result_ids)

    def get_recent_turns(
        self,
        turns: list[list[dict[str, JSONValue]]],
        keep_count: int,
    ) -> tuple[list[list[dict[str, JSONValue]]], list[list[dict[str, JSONValue]]]]:
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
