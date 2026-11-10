import { describe, expect, it } from "vitest";

import { makeMessage } from "../../test/fixtures";
import { mergeConversationMessages } from "./model";

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
        role: "assistant",
        sequence: 4,
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

  it("快照只权威替换文本，保留同一条消息的非文本 parts 和关联字段", () => {
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
      artifact_id: "artifact-1",
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
          role: "assistant",
          sequence: 5,
          status: "completed",
          text: "快照权威文本",
        },
      ],
    );

    expect(merged).toHaveLength(1);
    expect(merged[0]).toMatchObject({
      completed_at: "2026-11-10T03:02:00Z",
      created_at: "2026-11-10T03:03:00Z",
      id: "msg-full",
      reply_to_message_id: "msg-user",
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
});

