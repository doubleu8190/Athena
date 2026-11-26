import { describe, expect, it } from "vitest";

import { makeMessage, makeRun } from "../../test/fixtures";
import {
  mergeConversationMessages,
  shouldApplyStreamSnapshot,
  type RunSnapshotPayload,
} from "./model";
import { upsertMessagesIntoInfiniteData } from "./useSessionChatController";

describe("会话消息合并", () => {
  it("按 sequence 跨页去重，并让当前 Run 快照覆盖同序号历史文本", () => {
    const older = {
      items: [
        makeMessage({
          id: "m1",
          sequence: 1,
          session_id: "s1",
          parts: [{ text: "第一条", type: "text" }],
        }),
        makeMessage({
          id: "m2",
          sequence: 2,
          session_id: "s1",
          parts: [{ text: "第二条", type: "text" }],
        }),
      ],
      next_before_sequence: null,
    };
    const latest = {
      items: [
        makeMessage({
          id: "m3",
          sequence: 3,
          session_id: "s1",
          parts: [{ text: "历史第三", type: "text" }],
        }),
        makeMessage({
          id: "m4",
          sequence: 4,
          session_id: "s1",
          parts: [{ text: "历史第四", type: "text" }],
        }),
      ],
      next_before_sequence: 3,
    };

    const merged = mergeConversationMessages([latest, older], "run-1", [
      {
        created_at: "2026-11-10T03:00:00Z",
        id: "m4",
        parts: [{ text: "快照第四", type: "text" }],
        role: "assistant",
        sequence: 4,
        session_id: "s1",
        status: "completed",
        text: "快照第四",
      },
    ]);

    expect(merged.map((message) => message.sequence)).toEqual([1, 2, 3, 4]);
    expect(merged).toHaveLength(4);
    expect(merged[3].parts).toEqual([
      { text: "快照第四", type: "text" },
    ]);
  });

  it("快照携带完整结构化 parts 覆盖同序号历史消息，并将 exclude_none 省略的可空字段归零为 null", () => {
    const filePart = {
      file_id: "file-1",
      filename: "季度报告.pdf",
      mime_type: "application/pdf",
      size_bytes: 2048,
      type: "file_ref" as const,
    };
    const callPart = {
      arguments: { query: "季度数据" },
      name: "web_search",
      tool_call_id: "tool-call-1",
      type: "tool_call" as const,
    };
    const resultPart = {
      artifacts: [],
      output: "查询完成",
      status: "completed",
      tool_call_id: "tool-call-1",
      type: "tool_result" as const,
    };
    const history = makeMessage({
      completed_at: "2026-11-10T03:02:00Z",
      id: "msg-full",
      parts: [
        filePart,
        { text: "旧的历史文本", type: "text" },
        callPart,
        resultPart,
      ],
      reply_to_message_id: "msg-user",
      role: "assistant",
      run_id: "history-run",
      sequence: 5,
      session_id: "s1",
      status: "completed",
    });

    const merged = mergeConversationMessages(
      [
        {
          items: [history],
          next_before_sequence: null,
        },
      ],
      "stream-run",
      [
        {
          created_at: "2026-11-10T03:03:00Z",
          id: "msg-full",
          parts: [
            filePart,
            { text: "快照权威文本", type: "text" },
            callPart,
            resultPart,
          ],
          role: "assistant",
          sequence: 5,
          session_id: "s1",
          status: "completed",
          text: "快照权威文本",
        },
      ],
    );

    expect(merged).toHaveLength(1);
    expect(merged[0]).toMatchObject({
      completed_at: null,
      created_at: "2026-11-10T03:03:00Z",
      id: "msg-full",
      reply_to_message_id: null,
      role: "assistant",
      run_id: "history-run",
      sequence: 5,
      session_id: "s1",
      status: "completed",
    });
    expect(merged[0].parts).toEqual([
      filePart,
      { text: "快照权威文本", type: "text" },
      callPart,
      resultPart,
    ]);
    expect(
      merged[0].parts.some(
        (part) => part.type === "text" && part.text === "旧的历史文本",
      ),
    ).toBe(false);
  });

  it("自包含快照携带完整 parts 与关联字段时直接保留附件块与元数据，无需历史缓存兜底", () => {
    const filePart = {
      file_id: "file-inline-1",
      filename: "季度汇总.xlsx",
      mime_type:
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      size_bytes: 4096,
      type: "file_ref" as const,
    };
    const merged = mergeConversationMessages([], "run-inline", [
      {
        completed_at: "2026-11-10T03:05:00Z",
        created_at: "2026-11-10T03:04:00Z",
        id: "msg-inline-final",
        parts: [
          { text: "这是完整的最终回复。", type: "text" },
          filePart,
        ],
        reply_to_message_id: "msg-user-1",
        role: "assistant",
        run_id: "run-inline",
        sequence: 2,
        session_id: "s-inline",
        status: "completed",
        text: "这是完整的最终回复。",
      },
    ]);

    expect(merged).toEqual([
      {
        completed_at: "2026-11-10T03:05:00Z",
        created_at: "2026-11-10T03:04:00Z",
        id: "msg-inline-final",
        parts: [
          { text: "这是完整的最终回复。", type: "text" },
          filePart,
        ],
        reply_to_message_id: "msg-user-1",
        role: "assistant",
        run_id: "run-inline",
        sequence: 2,
        session_id: "s-inline",
        status: "completed",
      },
    ]);
  });

  it("upsertMessagesIntoInfiniteData 已有 seq 1..3 且 next_before_sequence 为 null 时合入 seq 4 绝不覆写游标", () => {
    const existing = {
      pageParams: [undefined],
      pages: [
        {
          items: [
            makeMessage({ id: "m1", sequence: 1, session_id: "s1" }),
            makeMessage({ id: "m2", sequence: 2, session_id: "s1" }),
            makeMessage({ id: "m3", sequence: 3, session_id: "s1" }),
          ],
          next_before_sequence: null,
        },
      ],
    };
    const incoming = [makeMessage({ id: "m4", sequence: 4, session_id: "s1" })];

    const updated = upsertMessagesIntoInfiniteData(existing, incoming, 4);
    expect(updated?.pages).toHaveLength(1);
    expect(updated?.pages[0]?.items.map((m) => m.sequence)).toEqual([
      1, 2, 3, 4,
    ]);
    // 历史已触顶（next_before_sequence === null）时，裁剪快照传入的 fallbackNextBeforeSequence=4 不得覆写为非空。
    expect(updated?.pages[0]?.next_before_sequence).toBeNull();

    // 冷缓存初始化与空占位页首次填充仍采用 fallbackNextBeforeSequence。
    const cold = upsertMessagesIntoInfiniteData(undefined, incoming, 4);
    expect(cold?.pages[0]?.next_before_sequence).toBe(4);

    const emptyPlaceholder = upsertMessagesIntoInfiniteData(
      {
        pageParams: [undefined],
        pages: [{ items: [], next_before_sequence: null }],
      },
      incoming,
      4,
    );
    expect(emptyPlaceholder?.pages[0]?.next_before_sequence).toBe(4);
  });
});

function makeSnapshot(
  overrides: Partial<RunSnapshotPayload> = {},
): RunSnapshotPayload {
  return {
    assistant_message_id: null,
    event_sequence: 5,
    events: [],
    events_after_sequence: null,
    events_complete: true,
    messages: [],
    messages_before_sequence: null,
    messages_complete: true,
    report_message_id: null,
    status: "running",
    ...overrides,
  };
}

describe("SSE 快照单调性保护", () => {
  it("已有终态缓存时拒绝任何后续快照回退覆盖", () => {
    const completed = makeRun({
      event_sequence: 5,
      id: "run-monotonic-1",
      session_id: "session-1",
      status: "completed",
    });
    expect(
      shouldApplyStreamSnapshot(
        completed,
        makeSnapshot({ event_sequence: 6, status: "running" }),
      ),
    ).toBe(false);
    expect(
      shouldApplyStreamSnapshot(
        completed,
        makeSnapshot({ event_sequence: 6, status: "completed" }),
      ),
    ).toBe(false);
  });

  it("进入 cancelling 后拒绝旧的 running/queued 快照，但允许进入终态", () => {
    const cancelling = makeRun({
      event_sequence: 5,
      id: "run-monotonic-2",
      session_id: "session-1",
      status: "cancelling",
    });
    expect(
      shouldApplyStreamSnapshot(
        cancelling,
        makeSnapshot({ event_sequence: 6, status: "running" }),
      ),
    ).toBe(false);
    expect(
      shouldApplyStreamSnapshot(
        cancelling,
        makeSnapshot({ event_sequence: 6, status: "queued" }),
      ),
    ).toBe(false);
    expect(
      shouldApplyStreamSnapshot(
        cancelling,
        makeSnapshot({ event_sequence: 6, status: "cancelled" }),
      ),
    ).toBe(true);
    expect(
      shouldApplyStreamSnapshot(
        cancelling,
        makeSnapshot({ event_sequence: 5, status: "cancelling" }),
      ),
    ).toBe(true);
  });

  it("同代次事件序号倒退时拒绝覆盖，单调推进时放行", () => {
    const running = makeRun({
      event_sequence: 5,
      id: "run-monotonic-3",
      session_id: "session-1",
      status: "running",
    });
    expect(
      shouldApplyStreamSnapshot(
        running,
        makeSnapshot({ event_sequence: 4, status: "running" }),
      ),
    ).toBe(false);
    expect(
      shouldApplyStreamSnapshot(
        running,
        makeSnapshot({ event_sequence: 5, status: "waiting_for_subagents" }),
      ),
    ).toBe(true);
    expect(
      shouldApplyStreamSnapshot(
        running,
        makeSnapshot({ event_sequence: 6, status: "completed" }),
      ),
    ).toBe(true);
  });
});

