"""本机 Chat Completions 可控 provider，用于真实 HTTP/SSE 协议测试。"""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import socket
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from secrets import token_hex
from typing import Any

_SKILL_ID = re.compile(r'"skill_id":"([^"]+)"')
_INPUT_FILE_SYSTEM_PREFIX = "以下是当前 Run 明确关联、且已按会话权限解析的输入文件。"
_INPUT_FILE_USER_PREFIX = "以下 <untrusted_input_file_metadata> 中的 JSON 是用户可控的"
_INPUT_FILE_METADATA_START = "<untrusted_input_file_metadata>\n"
_INPUT_FILE_METADATA_END = "\n</untrusted_input_file_metadata>"
_SUMMARY_FILENAMES = frozenset({"e2e-summary.txt", "e2e-summary-v2.txt"})
_ARTIFACT_CONTENT_SUFFIX = "\n交付文件："


@dataclass(frozen=True, slots=True)
class _Action:
    """provider 下一轮应返回的工具调用或最终文本。"""

    stage: str
    tool_name: str | None = None
    arguments: dict[str, object] | None = None
    text: str | None = None
    stream_fault: str | None = None


def select_provider_action(payload: dict[str, Any]) -> _Action:
    """根据真实 OpenAI messages 和工具结果选择确定的下一阶段。"""
    messages = payload.get("messages")
    tools = payload.get("tools")
    if not isinstance(messages, list) or not isinstance(tools, list):
        return _Action(stage="invalid", text="E2E provider 收到无效请求。")
    tool_names = {
        str(item.get("function", {}).get("name"))
        for item in tools
        if isinstance(item, dict) and isinstance(item.get("function"), dict)
    }
    latest_user_index = -1
    user_messages: list[str] = []
    for index, message in enumerate(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            latest_user_index = index
            content = message.get("content")
            user_messages.append(content if isinstance(content, str) else "")
    current_messages = messages[latest_user_index + 1 :]
    system_text = "\n".join(
        str(message.get("content") or "")
        for message in messages
        if isinstance(message, dict) and message.get("role") == "system"
    )
    current_text_chunks: list[str] = [system_text]
    called_names: set[str] = set()
    for message in current_messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            current_text_chunks.append(content)
        calls = message.get("tool_calls")
        if isinstance(calls, list):
            for call in calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                if isinstance(function, dict) and isinstance(function.get("name"), str):
                    called_names.add(function["name"])
    current_text = "\n".join(current_text_chunks)
    latest_user_text = user_messages[-1] if user_messages else ""
    input_file_items = _extract_input_file_items(messages)
    # 后续 Run 的普通追问不应从历史投影中重新选择旧技能，直接按普通对话完成。
    if "E2E_" not in latest_user_text:
        return _Action(stage="final", text="已根据本轮输入完成回复。")
    if "E2E_REUSE" in latest_user_text:
        selected_file_id = _selected_summary_file_id(input_file_items)
        exchange = _latest_tool_exchange(current_messages, "session_file_read")
        if exchange is not None:
            _, arguments, result = exchange
            if selected_file_id is not None and _is_reuse_text_result(
                result,
                arguments=arguments,
                selected_file_id=selected_file_id,
            ):
                return _Action(
                    stage="final_reuse",
                    text="已读取当前选择的文本产物：2026-10 总值为 42。",
                )
            result_code = "missing"
            if result is not None and isinstance(result.get("code"), str):
                result_code = result["code"]
            return _Action(
                stage="final_reuse_failed",
                text=f"未取得可校验的文本产物：{result_code}。",
            )
        # 当前 Run 的选中文件只取基础上下文元数据，避免历史 tool result 的旧产物覆盖选择。
        if selected_file_id is not None:
            return _Action(
                stage="reuse_read_file",
                tool_name="session_file_read",
                arguments={
                    "file_id": selected_file_id,
                    "offset": 0,
                    "limit_bytes": 65_536,
                },
            )
        return _Action(
            stage="reuse_missing_file",
            text="当前 Run 没有可读取的已选择文件。",
        )
    skill_match = _SKILL_ID.search(system_text)
    if not skill_match or "skill_load" not in tool_names:
        return _Action(stage="final", text="已收到请求，但当前 Run 没有可执行技能。")
    skill_id = skill_match.group(1)
    if "skill_execute" in called_names:
        if '"status":"error"' in current_text or "approval_rejected" in current_text:
            return _Action(
                stage="final_rejected",
                text="审批已拒绝，技能脚本未执行，也没有生成文件。",
            )
        execute_exchange = _latest_tool_exchange(current_messages, "skill_execute")
        execute_succeeded = (
            execute_exchange is not None
            and execute_exchange[2] is not None
            and execute_exchange[2].get("status") == "ok"
        )
        if execute_succeeded and "E2E_POST_DELIVERY_FAILED" in latest_user_text:
            return _Action(
                stage="final_post_delivery_failed",
                text="POST_DELIVERY_TRANSIENT_FAILED",
                stream_fault="truncate",
            )
        if execute_succeeded and "E2E_POST_DELIVERY_CANCELLED" in latest_user_text:
            return _Action(
                stage="final_post_delivery_cancelled",
                text="POST_DELIVERY_TRANSIENT_CANCELLED",
                stream_fault="wait_for_cancel",
            )
        return _Action(
            stage="final_with_artifacts",
            text="技能执行完成，已交付 E2E Excel 报表和趋势图。",
        )
    if "session_file_read" in called_names:
        mode = "slow" if "E2E_CANCEL" in latest_user_text else "normal"
        selected_file_ids = _input_file_ids(input_file_items)
        return _Action(
            stage="execute",
            tool_name="skill_execute",
            arguments={
                "skill_id": skill_id,
                "script_path": "scripts/build_report.py",
                "parameters": {"mode": mode, "period": "2026-10"},
                "input_file_ids": selected_file_ids,
            },
        )
    if "skill_read_resource" in called_names:
        selected_file_ids = _input_file_ids(input_file_items)
        if selected_file_ids and "session_file_read" in tool_names:
            return _Action(
                stage="read_session_file",
                tool_name="session_file_read",
                arguments={
                    "file_id": selected_file_ids[-1],
                    "offset": 0,
                    "limit_bytes": 65536,
                },
            )
        return _Action(
            stage="list_session_files",
            tool_name="session_file_list",
            arguments={"after_file_id": None, "limit": 50},
        )
    if "skill_load" in called_names:
        return _Action(
            stage="read_skill_resource",
            tool_name="skill_read_resource",
            arguments={
                "skill_id": skill_id,
                "path": "references/e2e-instructions.txt",
                "offset": 0,
                "limit_bytes": 65536,
            },
        )
    return _Action(
        stage="load_skill",
        tool_name="skill_load",
        arguments={"skill_id": skill_id},
    )


def _extract_input_file_items(messages: list[Any]) -> list[dict[str, Any]]:
    """只从紧随固定 SYSTEM 说明后的输入文件元数据 user 消息中解析条目。"""
    for index in range(1, len(messages)):
        previous = messages[index - 1]
        current = messages[index]
        if not isinstance(previous, dict) or not isinstance(current, dict):
            continue
        if previous.get("role") != "system" or current.get("role") != "user":
            continue
        previous_content = previous.get("content")
        content = current.get("content")
        if not isinstance(previous_content, str) or not isinstance(content, str):
            continue
        if not previous_content.startswith(_INPUT_FILE_SYSTEM_PREFIX):
            continue
        if not content.startswith(_INPUT_FILE_USER_PREFIX):
            continue
        start_index = content.find(_INPUT_FILE_METADATA_START)
        if start_index < 0 or not content.endswith(_INPUT_FILE_METADATA_END):
            continue
        # 直接按固定起止标记切片，避免文件名中出现 ]</untrusted_input_file_metadata> 时提前截断。
        raw_json = content[
            start_index + len(_INPUT_FILE_METADATA_START) : -len(_INPUT_FILE_METADATA_END)
        ]
        try:
            parsed = json.loads(raw_json)
        except (TypeError, ValueError, UnicodeError):
            continue
        if not isinstance(parsed, list):
            continue
        return [
            item
            for item in parsed
            if isinstance(item, dict) and isinstance(item.get("file_id"), str)
        ]
    return []


def _input_file_ids(input_file_items: list[dict[str, Any]]) -> list[str]:
    """从输入文件元数据列表中提取去重后的稳定 file_id。"""
    return list(
        dict.fromkeys(
            file_id
            for item in input_file_items
            if isinstance(file_id := item.get("file_id"), str) and file_id
        )
    )


def _selected_summary_file_id(input_file_items: list[dict[str, Any]]) -> str | None:
    """从当前 Run 输入文件元数据选出唯一的确定性摘要文本文件。"""
    file_ids = list(
        dict.fromkeys(
            file_id
            for item in input_file_items
            if isinstance(file_id := item.get("file_id"), str)
            and file_id
            and item.get("filename") in _SUMMARY_FILENAMES
        )
    )
    return file_ids[0] if len(file_ids) == 1 else None


def _parse_tool_outcome(content: str) -> dict[str, Any] | None:
    """读取 tool output 首段 JSON，并接受 Run 上下文追加的交付文件后缀。"""
    try:
        parsed, end = json.JSONDecoder().raw_decode(content.lstrip())
    except (TypeError, ValueError, UnicodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    suffix = content.lstrip()[end:]
    if not suffix.strip():
        return parsed
    if not suffix.startswith(_ARTIFACT_CONTENT_SUFFIX):
        return None
    try:
        artifacts = json.loads(suffix[len(_ARTIFACT_CONTENT_SUFFIX) :])
    except (TypeError, ValueError, UnicodeError):
        return None
    return parsed if isinstance(artifacts, list) and artifacts else None


def _latest_tool_exchange(
    messages: list[Any],
    tool_name: str,
) -> tuple[str, dict[str, Any], dict[str, Any] | None] | None:
    """只在当前用户轮次内按真实 call_id 关联参数与 ToolOutcome。"""
    calls: list[tuple[str, dict[str, Any]]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        raw_calls = message.get("tool_calls")
        if not isinstance(raw_calls, list):
            continue
        for call in raw_calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            call_id = call.get("id")
            if (
                isinstance(function, dict)
                and function.get("name") == tool_name
                and isinstance(call_id, str)
            ):
                raw_arguments = function.get("arguments")
                if not isinstance(raw_arguments, str):
                    arguments: dict[str, Any] = {}
                else:
                    try:
                        parsed_arguments = json.loads(raw_arguments)
                    except (TypeError, ValueError, UnicodeError):
                        parsed_arguments = None
                    arguments = parsed_arguments if isinstance(parsed_arguments, dict) else {}
                calls.append((call_id, arguments))
    if not calls:
        return None
    expected_call_id, arguments = calls[-1]
    for message in reversed(messages):
        if (
            not isinstance(message, dict)
            or message.get("role") != "tool"
            or message.get("tool_call_id") != expected_call_id
        ):
            continue
        content = message.get("content")
        if not isinstance(content, str):
            return None
        result = _parse_tool_outcome(content)
        return expected_call_id, arguments, result
    # 当前轮次已经发过调用但尚无 tool result 时必须失败，不能再次发起读取。
    return expected_call_id, arguments, None


def _is_reuse_text_result(
    result: dict[str, Any] | None,
    *,
    arguments: dict[str, Any],
    selected_file_id: str,
) -> bool:
    """确认本轮真实调用和 ToolOutcome 都指向当前选中的摘要文本。"""
    if arguments.get("file_id") != selected_file_id:
        return False
    if result is None or result.get("status") != "ok":
        return False
    if result.get("code") != "session_file_read":
        return False
    data = result.get("data")
    if not isinstance(data, dict):
        return False
    content = data.get("content")
    file_info = data.get("file")
    if not isinstance(content, str) or not isinstance(file_info, dict):
        return False
    filename = file_info.get("filename")
    file_id = file_info.get("file_id")
    return (
        file_id == selected_file_id
        and filename == "e2e-summary.txt"
        and "E2E_REUSE_SOURCE" in content
        and "month=2026-10" in content
        and "total=42" in content
    )


class _ProviderHandler(BaseHTTPRequestHandler):
    """实现 OpenAI Chat Completions 流式响应的最小协议表面。"""

    server_version = "AthenaE2EProvider/1"

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(HTTPStatus.OK, {"status": "ok"})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found"}})
            return
        try:
            length = int(self.headers.get("content-length", "0"))
            if not 1 <= length <= 2 * 1024 * 1024:
                raise ValueError("body_size_invalid")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("body_invalid")
        except (ValueError, json.JSONDecodeError, UnicodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": {"code": "request_invalid"}})
            return
        action = select_provider_action(payload)
        self._log_action(payload, action)
        if payload.get("stream") is True:
            self._stream(action)
            return
        self._json(
            HTTPStatus.OK,
            {
                "id": f"chatcmpl-{token_hex(8)}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": str(payload.get("model", "e2e-scripted")),
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": action.text or "工具阶段已完成。",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
            },
        )

    def _stream(self, action: _Action) -> None:
        response_id = f"chatcmpl-{token_hex(8)}"
        model = "e2e-scripted"
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "keep-alive")
        self.end_headers()
        if action.stream_fault is not None:
            self._stream_partial_then_fault(action, response_id=response_id, model=model)
            return
        if action.tool_name is None:
            self._event(
                {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": action.text or ""},
                            "finish_reason": "stop",
                        }
                    ],
                }
            )
        else:
            self._event(
                {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": f"call_{token_hex(8)}",
                                        "type": "function",
                                        "function": {
                                            "name": action.tool_name,
                                            "arguments": json.dumps(
                                                action.arguments or {},
                                                ensure_ascii=False,
                                                separators=(",", ":"),
                                            ),
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                }
            )
        self._event(
            {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
            }
        )
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _stream_partial_then_fault(
        self,
        action: _Action,
        *,
        response_id: str,
        model: str,
    ) -> None:
        """先刷新 partial transient，再按场景断流或等待浏览器取消。"""
        self._event(
            {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "content": action.text or "",
                        },
                        "finish_reason": None,
                    }
                ],
            }
        )
        if action.stream_fault == "truncate":
            return
        if action.stream_fault == "wait_for_cancel":
            self._wait_for_client_disconnect(timeout_seconds=30)

    def _wait_for_client_disconnect(self, *, timeout_seconds: float) -> None:
        """等待客户端关闭流或到达上限，避免取消后遗留长时间 sleep。"""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            readable, _, _ = select.select([self.connection], [], [], 0.1)
            if not readable:
                continue
            try:
                pending = self.connection.recv(1, socket.MSG_PEEK)
            except OSError:
                return
            if not pending:
                return
            time.sleep(0.1)

    def _event(self, value: dict[str, object]) -> None:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        self.wfile.write(f"data: {data}\n\n".encode())
        self.wfile.flush()

    def _json(self, status: HTTPStatus, value: dict[str, object]) -> None:
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _log_action(self, payload: dict[str, Any], action: _Action) -> None:
        """只记录脱敏阶段和工具名，不记录完整上下文或凭据。"""
        log_path = Path(str(self.server.audit_path))  # type: ignore[attr-defined]
        with self._audit_lock:
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "stage": action.stage,
                            "tool": action.tool_name,
                            "stream": payload.get("stream") is True,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            log_path.chmod(0o600)

    @property
    def _audit_lock(self) -> threading.Lock:
        return self.server.audit_lock  # type: ignore[attr-defined,no-any-return]

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def main() -> None:
    """从环境读取端口并运行可控 provider。"""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("ATHENA_E2E_PROVIDER_PORT", "8012")),
    )
    parser.add_argument("--audit-file", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path)
    args = parser.parse_args()
    args.audit_file.parent.mkdir(parents=True, exist_ok=True)
    args.audit_file.touch(mode=0o600, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), _ProviderHandler)
    server.audit_path = args.audit_file  # type: ignore[attr-defined]
    server.audit_lock = threading.Lock()  # type: ignore[attr-defined]
    if args.ready_file is not None:
        args.ready_file.write_text("ready\n", encoding="utf-8")
        args.ready_file.chmod(0o600)
    server.serve_forever(poll_interval=0.2)


if __name__ == "__main__":
    main()


__all__ = ["select_provider_action"]
