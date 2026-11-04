import assert from "node:assert/strict"
import test from "node:test"
import {
  filterChatTimelineForLoadedRuns,
  historyMessages,
  historyTimeline,
  mergeHistoryMessages,
  mergeHistoryTimeline,
  sessionEventStreamUrl,
  timelineEntriesMatch,
} from "../src/utils/sessionHistory.ts"
import { SESSION_EVENT_NAMES } from "../src/utils/sessionEventCatalog.ts"
import type {
  ExecutionTimelineEntry,
  Message,
  SessionHistoryTurn,
} from "../src/types/index.ts"
import type { SessionSnapshotPayload } from "../src/types/events.ts"
import { applySessionSnapshot } from "../src/utils/sessionSnapshot.ts"


const turn: SessionHistoryTurn = {
  user: { time: "2026-01-01T00:00:00Z", content: "问题" },
  root_process: [
    { time: "2026-01-01T00:00:01Z", content: "正在理解问题" },
  ],
  tasks: [
    {
      worker_name: "Worker A",
      task_title: "整理资料",
      status: "completed",
      process: [
        { time: "2026-01-01T00:00:02Z", content: "开始执行" },
      ],
      result: { time: "2026-01-01T00:00:03Z", content: "任务完成" },
    },
    {
      worker_name: "Worker A",
      task_title: "整理资料",
      status: "completed",
      process: [
        { time: "2026-01-01T00:00:04Z", content: "重试开始" },
      ],
      result: { time: "2026-01-01T00:00:05Z", content: "重试完成" },
    },
  ],
  assistant: { time: "2026-01-01T00:00:06Z", content: "最终答复" },
}


test("history projection exposes only user and final assistant messages", () => {
  const messages = historyMessages([turn], "session-1")
  assert.deepEqual(messages.map((item) => item.role), ["user", "assistant"])
  assert.deepEqual(messages.map((item) => item.content), ["问题", "最终答复"])
  assert.equal(messages.every((item) => item.run_id === undefined), true)
  assert.equal(messages.every((item) => item.session_id === "session-1"), true)
})


test("Root, each Worker execution and result keep history order", () => {
  const timeline = historyTimeline([turn])
  assert.deepEqual(
    timeline.map((item) => item.label),
    [
      "正在理解问题",
      "开始执行",
      "整理资料",
      "重试开始",
      "整理资料",
    ],
  )
  assert.equal(
    timeline.filter((item) => item.event_type === "history.worker.result").length,
    2,
  )
})


test("history task results preserve completed, failed and cancelled terminal states", () => {
  const statuses = ["completed", "failed", "timed_out", "cancelled", "superseded"]
  const statusTurn: SessionHistoryTurn = {
    ...turn,
    tasks: statuses.map((status, index) => ({
      worker_name: "Worker A",
      task_title: `任务 ${status}`,
      status,
      process: [],
      result: {
        time: `2026-01-02T00:00:0${index}Z`,
        content: `${status} 结果`,
      },
    })),
  }

  const results = historyTimeline([statusTurn]).filter((item) => (
    item.event_type === "history.worker.result"
  ))

  assert.deepEqual(results.map((item) => item.status), [
    "completed",
    "failed",
    "failed",
    "cancelled",
    "cancelled",
  ])
})


test("history task result without status keeps the legacy completed default", () => {
  const legacyTask = {
    ...turn.tasks[0]!,
    status: undefined,
  }
  const legacyTurn: SessionHistoryTurn = {
    ...turn,
    tasks: [legacyTask as unknown as SessionHistoryTurn["tasks"][number]],
  }

  const result = historyTimeline([legacyTurn]).find((item) => (
    item.event_type === "history.worker.result"
  ))

  assert.equal(result?.status, "completed")
})


test("live completion cannot overwrite a complete local stream", () => {
  const complete: Message = {
    id: "assistant:run-1",
    role: "assistant",
    content: "最终答复",
    timestamp: "2026-01-01T00:00:01Z",
    run_id: "run-1",
    stream_status: "completed",
  }
  const history: Message = {
    id: "history:assistant",
    role: "assistant",
    content: "最终答复",
    timestamp: "2026-01-01T00:00:01Z",
  }
  const merged = mergeHistoryMessages([history], [complete])
  assert.equal(merged.length, 1)
  assert.equal(merged[0]?.content, "最终答复")
})


test("history and live Progress merge by stable local identity", () => {
  const history = historyTimeline([turn])
  const live: ExecutionTimelineEntry[] = history.map((entry) => ({
    ...entry,
    label: `${entry.label}（实时）`,
  }))
  const merged = mergeHistoryTimeline(history, live)
  assert.equal(merged.length, history.length)
  assert.equal(merged.every((entry) => entry.label.endsWith("（实时）")), true)
})

test("history and snapshot deduplicate when backend omits history IDs", () => {
  const history = historyTimeline([turn])
  const snapshot: ExecutionTimelineEntry[] = history.map((entry, index) => ({
    ...entry,
    id: `${entry.event_type}:${index}`,
    run_id: "run-1",
    event_type: entry.event_type === "history.worker.result"
      ? "task.result"
      : entry.event_type.replace("history.", "progress."),
  }))
  const merged = mergeHistoryTimeline(history, snapshot)
  assert.equal(merged.length, history.length)
  assert.equal(merged.every((entry) => entry.run_id === "run-1"), true)
  assert.equal(timelineEntriesMatch(history[0]!, snapshot[0]!), true)
})


test("chat timeline hides cached runs outside the loaded user-message pages", () => {
  const entries: ExecutionTimelineEntry[] = [
    {
      id: "history",
      event_type: "history.root",
      label: "历史",
      status: "info",
      timestamp: "2026-01-01T00:00:00Z",
    },
    {
      id: "loaded",
      run_id: "run-loaded",
      event_type: "progress.root",
      label: "已加载",
      status: "info",
      timestamp: "2026-01-01T00:00:01Z",
    },
    {
      id: "other-page",
      run_id: "run-other-page",
      event_type: "progress.root",
      label: "未加载",
      status: "info",
      timestamp: "2026-01-01T00:00:02Z",
    },
  ]
  const visible = filterChatTimelineForLoadedRuns(
    entries,
    [{
      id: "message-1",
      role: "user",
      content: "已加载",
      timestamp: "2026-01-01T00:00:00Z",
      run_id: "run-loaded",
    }],
    [],
  )
  assert.deepEqual(visible.map((entry) => entry.id), ["history", "loaded"])
})


test("SSE URL is created from the history sync token on the stream route", () => {
  assert.equal(
    sessionEventStreamUrl("/api-base/", "session/a", "opaque-token"),
    "/api-base/api/sessions/session%2Fa/stream?sync_token=opaque-token",
  )
})


test("public stream subscribes to exactly nine user-visible events", () => {
  assert.deepEqual([...SESSION_EVENT_NAMES], [
    "session.snapshot",
    "turn.started",
    "progress.created",
    "task.result",
    "assistant.started",
    "assistant.delta",
    "assistant.completed",
    "turn.failed",
    "turn.cancelled",
  ])
})


test("multi-turn snapshot merges every recovered turn without replacing current", () => {
  const payload: SessionSnapshotPayload = {
    run_id: "run-2",
    run_status: "completed",
    turns: [
      {
        run_id: "run-1",
        run_status: "completed",
        user: {
          message_id: "user-1",
          time: "2026-01-01T00:00:00Z",
          content: "第一问",
        },
        root_process: [{
          progress_id: "progress-1",
          time: "2026-01-01T00:00:01Z",
          content: "第一轮过程",
        }],
        tasks: [],
        assistant: {
          message_id: "assistant:run-1",
          time: "2026-01-01T00:00:02Z",
          content: "第一答",
          completed: true,
          last_chunk_id: 0,
        },
      },
      {
        run_id: "run-2",
        run_status: "completed",
        user: {
          message_id: "user-2",
          time: "2026-01-01T00:00:03Z",
          content: "第二问",
        },
        root_process: [{
          progress_id: "progress-2",
          time: "2026-01-01T00:00:04Z",
          content: "第二轮过程",
        }],
        tasks: [],
        assistant: {
          message_id: "assistant:run-2",
          time: "2026-01-01T00:00:05Z",
          content: "第二答",
          completed: true,
          last_chunk_id: 0,
        },
      },
    ],
  }

  const once = applySessionSnapshot({
    messages: [{
      id: "existing",
      role: "system",
      content: "已有内容",
      timestamp: "2025-12-31T23:59:59Z",
    }],
    executionTimeline: [],
    agentStatus: "idle",
    error: null,
    errorDetail: null,
  }, payload)
  const twice = applySessionSnapshot(once, payload)

  assert.deepEqual(
    twice.messages?.map((message) => message.content),
    ["已有内容", "第一问", "第一答", "第二问", "第二答"],
  )
  assert.deepEqual(
    twice.executionTimeline?.map((entry) => entry.label),
    ["第一轮过程", "第二轮过程"],
  )
  assert.equal(twice.agentStatus, "completed")
})


test("failed or cancelled terminal snapshots discard only transient assistant text", () => {
  const baseMessage = {
    role: "assistant" as const,
    timestamp: "2026-01-01T00:00:01Z",
    run_id: "run-1",
  }
  const view = applySessionSnapshot({
    messages: [
      {
        ...baseMessage,
        id: "assistant:run-1",
        content: "尚未完成的半截回答",
        stream_status: "streaming",
      },
      {
        ...baseMessage,
        id: "persisted-assistant",
        content: "另一轮已持久化回答",
        run_id: "run-2",
        stream_status: "completed",
      },
    ],
    executionTimeline: [],
    agentStatus: "running",
    error: null,
    errorDetail: null,
  }, {
    run_id: "run-1",
    run_status: "cancelled",
    turns: [{
      run_id: "run-1",
      run_status: "cancelled",
      user: {
        message_id: "user-1",
        time: "2026-01-01T00:00:00Z",
        content: "问题",
      },
      root_process: [],
      tasks: [],
      assistant: null,
    }],
  })

  assert.deepEqual(
    view.messages?.map((message) => message.id),
    ["user-1", "persisted-assistant"],
  )
  assert.equal(view.agentStatus, "idle")
})


test("sequential snapshot pages merge without replacing earlier recovered turns", () => {
  const page = (sequence: number): SessionSnapshotPayload => ({
    run_id: `run-${sequence}`,
    run_status: "completed",
    turns: [{
      run_id: `run-${sequence}`,
      run_status: "completed",
      user: {
        message_id: `user-${sequence}`,
        time: `2026-01-01T00:00:0${sequence * 3}Z`,
        content: `第${sequence}问`,
      },
      root_process: [{
        progress_id: `progress-${sequence}`,
        time: `2026-01-01T00:00:0${sequence * 3 + 1}Z`,
        content: `第${sequence}轮过程`,
      }],
      tasks: [],
      assistant: {
        message_id: `assistant:run-${sequence}`,
        time: `2026-01-01T00:00:0${sequence * 3 + 2}Z`,
        content: `第${sequence}答`,
        completed: true,
        last_chunk_id: 0,
      },
    }],
  })
  const empty = {
    messages: [],
    executionTimeline: [],
    agentStatus: "idle" as const,
    error: null,
    errorDetail: null,
  }

  const first = applySessionSnapshot(empty, page(1))
  const second = applySessionSnapshot(first, page(2))

  assert.deepEqual(
    second.messages?.map((message) => message.content),
    ["第1问", "第1答", "第2问", "第2答"],
  )
  assert.deepEqual(
    second.executionTimeline?.map((entry) => entry.label),
    ["第1轮过程", "第2轮过程"],
  )
})
