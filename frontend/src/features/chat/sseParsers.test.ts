import { describe, expect, it } from "vitest";

import {
  isRecord,
  parseEventData,
  parseRunEvent,
  parseSnapshotPayload,
} from "./sseParsers";

describe("SSE 纯解析层（sseParsers）边界与容错", () => {
  it("isRecord 与 parseEventData 拒绝非对象、数组与损坏 JSON", () => {
    expect(isRecord({ ok: true })).toBe(true);
    expect(isRecord(null)).toBe(false);
    expect(isRecord([])).toBe(false);
    expect(isRecord("text")).toBe(false);

    expect(parseEventData('{"payload":{"text":"你好"}}')).toEqual({
      payload: { text: "你好" },
    });
    expect(parseEventData("[1,2,3]")).toBeNull();
    expect(parseEventData('"plain"')).toBeNull();
    expect(parseEventData("null")).toBeNull();
    expect(parseEventData("{broken-json")).toBeNull();
  });

  it("parseRunEvent 校验必填字段并拒绝缺键或非对象 payload", () => {
    expect(
      parseRunEvent({
        created_at: "2026-11-27T00:00:00Z",
        id: "evt-1",
        payload: { assistant_message_id: "msg-1" },
        run_id: "run-1",
        sequence: 3,
        type: "completed",
      }),
    ).toEqual({
      created_at: "2026-11-27T00:00:00Z",
      id: "evt-1",
      payload: { assistant_message_id: "msg-1" },
      run_id: "run-1",
      sequence: 3,
      type: "completed",
    });

    expect(
      parseRunEvent({
        created_at: "2026-11-27T00:00:00Z",
        id: "evt-2",
        payload: ["not-record"],
        run_id: "run-1",
        sequence: 4,
        type: "started",
      }),
    ).toBeNull();
    expect(
      parseRunEvent({
        created_at: "2026-11-27T00:00:00Z",
        id: "evt-3",
        payload: {},
        run_id: "run-1",
        sequence: Number.NaN,
        type: "started",
      }),
    ).toBeNull();
  });

  it("parseSnapshotPayload 拒绝非法根结构与未知 status", () => {
    expect(parseSnapshotPayload(null)).toBeNull();
    expect(parseSnapshotPayload([])).toBeNull();
    expect(parseSnapshotPayload({ status: "unknown_status" })).toBeNull();
  });

  it("非法消息状态、非法 tool_result.status 或损坏 artifact 均触发 has_unparseable_messages 且强制 messages_complete=false", () => {
    const parsed = parseSnapshotPayload({
      event_sequence: 5,
      events: [
        {
          created_at: "2026-11-27T00:00:00Z",
          id: "evt-ok",
          payload: {},
          run_id: "run-1",
          sequence: 1,
          type: "started",
        },
        { id: "evt-broken", sequence: "bad" },
      ],
      events_complete: true,
      messages: [
        {
          created_at: "2026-11-27T00:00:00Z",
          id: "msg-bad-status",
          parts: [{ text: "异常状态", type: "text" }],
          role: "assistant",
          sequence: 1,
          session_id: "session-1",
          status: "invalid_status",
          text: "异常状态",
        },
        {
          created_at: "2026-11-27T00:00:01Z",
          id: "msg-bad-tool-result-status",
          parts: [
            {
              artifacts: [],
              output: "ok",
              status: "completed",
              tool_call_id: "tc-1",
              type: "tool_result",
            },
          ],
          role: "assistant",
          sequence: 2,
          session_id: "session-1",
          status: "completed",
          text: "",
        },
        {
          created_at: "2026-11-27T00:00:02Z",
          id: "msg-bad-artifact",
          parts: [
            {
              artifacts: [{ file_id: "file-1", filename: "bad.bin" }],
              output: null,
              status: "success",
              tool_call_id: "tc-2",
              type: "tool_result",
            },
          ],
          role: "assistant",
          sequence: 3,
          session_id: "session-1",
          status: "completed",
          text: "",
        },
      ],
      messages_complete: true,
      status: "running",
    });

    expect(parsed).not.toBeNull();
    expect(parsed?.messages).toEqual([]);
    expect(parsed?.has_unparseable_messages).toBe(true);
    expect(parsed?.messages_complete).toBe(false);
    // 损坏 event 被安全过滤，合法 event 完整保留。
    expect(parsed?.events).toHaveLength(1);
    expect(parsed?.events[0]?.id).toBe("evt-ok");
  });

  it("safe_skill_projection 缺省 can_approve 默认 fail-closed 为 false，tool_kind 仅接受合法枚举", () => {
    const parsed = parseSnapshotPayload({
      event_sequence: 8,
      events: [],
      events_complete: true,
      messages: [],
      messages_complete: true,
      pending_subagent_approvals: [
        {
          batch_message_id: "msg-batch-1",
          call_id: "call-1",
          model_name: "skill_execute",
          ordinal: 1,
          requested_at: "2026-11-27T00:00:00Z",
          safe_skill_projection: {
            call_id: "call-1",
            purpose: "执行核算脚本",
            script_label: "calc.py",
            skill_id: "skill-1",
            skill_name: "核算",
            skill_version_id: "sv-1",
            skill_version_label: "v1",
          },
          sub_run_id: "sub-1",
          subagent_name: "财务助手",
          subagent_slug: "finance",
          tool_kind: "builtin",
        },
        {
          batch_message_id: "msg-batch-2",
          call_id: "call-2",
          model_name: "mcp_tool",
          ordinal: 2,
          requested_at: "2026-11-27T00:00:01Z",
          safe_skill_projection: {
            call_id: "call-2",
            input_files: [{ bad: true }],
            purpose: "损坏文件项应导致投影整体降级为 null",
            script_label: "bad.py",
            skill_id: "skill-2",
            skill_name: "异常技能",
            skill_version_id: "sv-2",
            skill_version_label: "v1",
          },
          sub_run_id: "sub-2",
          subagent_name: "外联助手",
          subagent_slug: "ext",
          tool_kind: "unknown_kind",
        },
      ],
      status: "waiting_for_subagents",
    });

    expect(parsed?.pending_subagent_approvals).toHaveLength(2);
    expect(parsed?.pending_subagent_approvals?.[0]?.tool_kind).toBe("builtin");
    expect(
      parsed?.pending_subagent_approvals?.[0]?.safe_skill_projection?.can_approve,
    ).toBe(false);
    expect(parsed?.pending_subagent_approvals?.[1]?.tool_kind).toBeNull();
    expect(
      parsed?.pending_subagent_approvals?.[1]?.safe_skill_projection,
    ).toBeNull();
  });
});
