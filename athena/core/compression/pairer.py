"""消息配对器 — 按 run_id 识别完整对话轮次."""

from __future__ import annotations

from athena.models import Message, MessageRole


class MessagePairer:
    """将扁平消息列表分割为完整对话轮次."""

    def identify_turns(
        self, messages: list[Message]
    ) -> list[list[Message]]:
        """按 ``run_id``（缺失时按用户消息）分割完整对话轮次.

        返回值：
            轮次列表，每个轮次是一个消息列表
        """
        turns: list[list[Message]] = []
        current_turn: list[Message] = []
        current_run_id: str | None = None

        for msg in messages:
            run_id = msg.run_id
            is_system = msg.role == MessageRole.SYSTEM
            is_user = msg.role == MessageRole.USER
            if is_system and not current_turn:
                turns.append([msg])
                continue
            if (
                current_turn
                and run_id
                and current_run_id
                and str(run_id) != current_run_id
            ):
                turns.append(current_turn)
                current_turn = []
            elif current_turn and not run_id and is_user:
                turns.append(current_turn)
                current_turn = []

            current_turn.append(msg)
            current_run_id = str(run_id) if run_id else None

        if current_turn:
            turns.append(current_turn)

        return turns

    def split_recent_turns(
        self,
        turns: list[list[Message]],
        keep_count: int,
    ) -> tuple[list[list[Message]], list[list[Message]]]:
        """分离旧轮次与最近轮次.

        返回值：
            (旧轮次列表, 最近轮次列表)。
        """
        if keep_count < 0:
            raise ValueError("keep_count 不能为负数")
        if len(turns) <= keep_count:
            return [], turns

        system_indices = {
            index
            for index, turn in enumerate(turns)
            if turn and turn[0].role == MessageRole.SYSTEM
        }
        system_turns = [turns[index] for index in sorted(system_indices)]
        dialogue_turns = [
            turn for index, turn in enumerate(turns) if index not in system_indices
        ]
        if len(dialogue_turns) <= keep_count:
            return [], turns
        if keep_count == 0:
            return dialogue_turns, system_turns
        return dialogue_turns[:-keep_count], system_turns + dialogue_turns[-keep_count:]
