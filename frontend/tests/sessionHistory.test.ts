import assert from "node:assert/strict"
import test from "node:test"
import { useChatStore } from "../src/store/chatStore.ts"
import {
  backgroundSessionCanRelease,
  cacheBackgroundLlmStep,
  cacheSessionTimelineProjection,
  filterChatTimelineForLoadedRuns,
  hasCompletedAssistant,
  historyMessages,
  historyTimeline,
  mergeHistoryMessages,
  projectLlmValidationFailureTimeline,
  sessionEventResumeCursor,
  sessionEventStreamUrl,
} from "../src/utils/sessionHistory.ts"
import { SESSION_EVENT_NAMES } from "../src/utils/sessionEventCatalog.ts"
import { upsertTimelineEntry } from "../src/utils/executionTimeline.ts"
import type { Message, Session, SessionHistoryTurn } from "../src/types/index.ts"

function message(
  id: string,
  role: Message["role"],
  content: string,
  runId: string,
  timestamp: string,
  streamStatus?: Message["stream_status"],
): Message {
  return {
    id,
    role,
    content,
    run_id: runId,
    timestamp,
    session_id: "session-a",
    stream_status: streamStatus,
  }
}

function session(id: string): Session {
  return {
    id,
    title: id,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  }
}

test("history and live messages merge by run and never overwrite persisted final text", () => {
  const persisted = [
    message("user-server", "user", "问题", "run-1", "2026-01-01T00:00:00Z"),
    message(
      "assistant:run-1",
      "assistant",
      "持久最终答复",
      "run-1",
      "2026-01-01T00:00:01Z",
      "completed",
    ),
  ]
  const liveStartedFirst = [
    message("user-receipt", "user", "问题", "run-1", "2026-01-01T00:00:00Z"),
    message(
      "assistant:run-1",
      "assistant",
      "",
      "run-1",
      "2026-01-01T00:00:01Z",
      "streaming",
    ),
  ]

  const merged = mergeHistoryMessages(persisted, liveStartedFirst)

  assert.equal(merged.length, 2)
  assert.equal(merged.find((item) => item.role === "user")?.id, "user-server")
  assert.equal(
    merged.find((item) => item.role === "assistant")?.content,
    "持久最终答复",
  )
  assert.equal(
    merged.find((item) => item.role === "assistant")?.stream_status,
    "completed",
  )
  assert.equal(hasCompletedAssistant(merged, "run-1"), true)
})

test("history first and interleaved live messages remain unique", () => {
  const history = [
    message("user-1", "user", "第一问", "run-1", "2026-01-01T00:00:00Z"),
    message(
      "assistant:run-1",
      "assistant",
      "第一答",
      "run-1",
      "2026-01-01T00:00:01Z",
      "completed",
    ),
  ]
  const interleaved = [
    message("pending", "user", "第二问", "run-2", "2026-01-01T00:01:00Z"),
    message(
      "assistant:run-2",
      "assistant",
      "第二答",
      "run-2",
      "2026-01-01T00:01:01Z",
      "completed",
    ),
    message(
      "assistant:run-1",
      "assistant",
      "",
      "run-1",
      "2026-01-01T00:00:01Z",
      "streaming",
    ),
  ]

  const merged = mergeHistoryMessages(history, interleaved)

  assert.deepEqual(
    merged.map((item) => `${item.role}:${item.run_id}`),
    ["user:run-1", "assistant:run-1", "user:run-2", "assistant:run-2"],
  )
  assert.equal(merged[1]?.content, "第一答")
  assert.equal(merged[3]?.content, "第二答")
})

test("history and live projections of the same durable event do not duplicate the timeline", () => {
  const turn: SessionHistoryTurn = {
    run_id: "run-1",
    user: {
      time: "2026-01-01T00:00:00Z",
      content: "问题",
      id: "message-1",
    },
    root_process: [],
    tasks: [{
      time: "2026-01-01T00:00:02Z",
      process: [
        {
          time: "2026-01-01T00:00:01Z",
          content: "任务已开始",
          session_seq: 6,
        },
        {
          time: "2026-01-01T00:00:02Z",
          content: "任务执行成功",
          session_seq: 7,
        },
      ],
      content: "完成结果",
      session_seq: 7,
    }],
    assistant: null,
  }
  const historyEntries = historyTimeline([turn])
  const liveEntry = {
    id: "task:plan-1:task-1:exec-1:task.completed",
    run_id: "run-1",
    event_type: "task.completed",
    label: "任务执行成功：检索资料",
    detail: "完成结果",
    status: "completed" as const,
    timestamp: "2026-01-01T00:00:02Z",
    session_seq: 7,
  }

  const merged = upsertTimelineEntry(historyEntries, {}, liveEntry)

  assert.equal(merged.length, 2)
  assert.equal(merged.find((entry) => entry.session_seq === 7)?.id, liveEntry.id)

  useChatStore.setState({
    executionTimeline: [{
      ...liveEntry,
      label: "实时任务结果",
    }],
    executionTimelineTerminalMarkers: {},
  })
  useChatStore.getState().replaceRunExecutionTimeline("run-1", historyEntries)
  assert.equal(
    useChatStore.getState().executionTimeline
      .find((entry) => entry.session_seq === 7)?.label,
    "实时任务结果",
  )
})

test("llm.validation_failed is subscribed, projected, cached, and restored on session switch", () => {
  assert.equal(SESSION_EVENT_NAMES.includes("llm.validation_failed"), true)
  const envelope = {
    session_seq: 41,
    event_type: "llm.validation_failed",
    run_id: "run-b",
    occurred_at: "2026-01-01T00:02:00Z",
    payload: {
      call_id: "llm-call-1",
      parent_activity_id: "task:task-1:exec:0",
      error: {
        message: "结构化输出不符合约定",
        stack: "private stack",
      },
    },
  }
  const entry = projectLlmValidationFailureTimeline(
    envelope,
    "run-b",
    "2026-01-01T00:02:00Z",
  )
  assert.equal(entry.label, "模型输出校验失败")
  assert.equal(entry.detail, "结构化输出不符合约定")
  assert.equal(entry.detail?.includes("private stack"), false)

  const store = useChatStore.getState()
  store.setSessions([session("session-a"), session("session-b")])
  useChatStore.setState({
    activeSessionId: "session-a",
    messages: [],
    executionTimeline: [],
    runTimelineEvents: [],
    sessionViewCache: {},
  })
  useChatStore.setState((state) => ({
    sessionViewCache: {
      ...state.sessionViewCache,
      "session-b": cacheSessionTimelineProjection(
        {},
        envelope,
        entry,
      ),
    },
  }))

  store.setActiveSession("session-b")
  assert.equal(
    useChatStore.getState().executionTimeline.some((item) =>
      item.event_type === "llm.validation_failed"
    ),
    true,
  )
  assert.equal(
    useChatStore.getState().runTimelineEvents.some((item) =>
      item.event_type === "llm.validation_failed"
    ),
    true,
  )
  useChatStore.getState().setActiveSession("session-a")
  assert.equal(
    useChatStore.getState().executionTimeline.some((item) =>
      item.event_type === "llm.validation_failed"
    ),
    false,
  )
  useChatStore.getState().setActiveSession("session-b")
  assert.equal(
    useChatStore.getState().executionTimeline.some((item) =>
      item.event_type === "llm.validation_failed"
    ),
    true,
  )

  useChatStore.getState().upsertExecutionTimelineEntry({
    ...entry,
    id: "foreground-validation",
    session_seq: 42,
  })
  useChatStore.getState().setActiveSession("session-a")
  useChatStore.getState().setActiveSession("session-b")
  assert.equal(
    useChatStore.getState().executionTimeline.some((item) =>
      item.id === "foreground-validation"
    ),
    true,
  )
})

test("background LLM events cache a deduplicated step only for the target session", () => {
  const store = useChatStore.getState()
  store.setSessions([session("session-a"), session("session-b")])
  useChatStore.setState({
    activeSessionId: "session-a",
    steps: [],
    sessionViewCache: {},
  })

  const started = {
    session_seq: 51,
    event_type: "llm.started",
    run_id: "run-b",
    occurred_at: "2026-01-01T00:03:00Z",
    parent_run_id: "parent-run",
    payload: {
      call_id: "llm-call-background",
      started_at: "2026-01-01T00:03:00Z",
    },
  }
  useChatStore.setState((state) => ({
    sessionViewCache: {
      ...state.sessionViewCache,
      "session-b": {
        ...state.sessionViewCache["session-b"],
        steps: cacheBackgroundLlmStep(
          state.sessionViewCache["session-b"]?.steps,
          "session-b",
          started,
        ),
      },
    },
  }))
  const running = useChatStore.getState().sessionViewCache["session-b"]?.steps?.[0]
  assert.equal(running?.status, "running")
  assert.equal(running?.session_id, "session-b")
  assert.equal(running?.run_id, "run-b")
  assert.equal(running?.parent_run_id, "parent-run")
  assert.equal(running?.step_type, "llm_call")
  assert.equal(running?.started_at, "2026-01-01T00:03:00Z")

  useChatStore.setState((state) => ({
    sessionViewCache: {
      ...state.sessionViewCache,
      "session-b": {
        ...state.sessionViewCache["session-b"],
        steps: cacheBackgroundLlmStep(
          state.sessionViewCache["session-b"]?.steps,
          "session-b",
          {
            session_seq: 52,
            event_type: "llm.validation_failed",
            run_id: "run-b",
            occurred_at: "2026-01-01T00:03:02Z",
            payload: {
              call_id: "llm-call-background",
              error: { message: "schema mismatch" },
              duration_ms: 2000,
            },
          },
        ),
      },
    },
  }))
  const failedSteps = useChatStore.getState().sessionViewCache["session-b"]?.steps || []
  assert.equal(failedSteps.length, 1)
  assert.equal(failedSteps[0]?.status, "failed")
  assert.equal(failedSteps[0]?.completed_at, "2026-01-01T00:03:02Z")
  assert.equal(failedSteps[0]?.duration_ms, 2000)
  assert.equal(failedSteps[0]?.error_message, "schema mismatch")

  store.setActiveSession("session-b")
  assert.equal(useChatStore.getState().steps[0]?.status, "failed")
  useChatStore.getState().setActiveSession("session-a")
  assert.deepEqual(useChatStore.getState().steps, [])
  useChatStore.getState().setActiveSession("session-b")
  assert.equal(useChatStore.getState().steps.length, 1)
  assert.equal(useChatStore.getState().steps[0]?.status, "failed")
})

test("session view cache restores only the selected session state", () => {
  const store = useChatStore.getState()
  store.setSessions([session("session-a"), session("session-b")])
  useChatStore.setState({
    activeSessionId: null,
    messages: [],
    sessionViewCache: {},
    sessionHistoryLoaded: {},
    sessionEventCursors: {},
    sessionRunStatuses: {},
    sessionRunStatusRevisions: {},
  })

  store.setActiveSession("session-a")
  store.addMessage(message(
    "message-a",
    "user",
    "A",
    "run-a",
    "2026-01-01T00:00:00Z",
  ))
  store.setActiveSession("session-b")
  store.addMessage(message(
    "message-b",
    "user",
    "B",
    "run-b",
    "2026-01-01T00:01:00Z",
  ))

  assert.deepEqual(
    useChatStore.getState().messages.map((item) => item.id),
    ["message-b"],
  )
  useChatStore.getState().setActiveSession("session-a")
  assert.deepEqual(
    useChatStore.getState().messages.map((item) => item.id),
    ["message-a"],
  )
  useChatStore.getState().setActiveSession("session-b")
  assert.deepEqual(
    useChatStore.getState().messages.map((item) => item.id),
    ["message-b"],
  )
})

test("background output is cached without touching the active session on first switch", () => {
  const store = useChatStore.getState()
  store.setSessions([session("session-a"), session("session-b")])
  useChatStore.setState({
    activeSessionId: null,
    messages: [],
    sessionViewCache: {},
    sessionHistoryLoaded: {},
    sessionEventCursors: {},
    sessionRunStatuses: {},
    sessionRunStatusRevisions: {},
  })

  store.setActiveSession("session-a")
  store.addMessage(message(
    "message-a",
    "user",
    "当前会话",
    "run-a",
    "2026-01-01T00:00:00Z",
  ))
  useChatStore.setState((state) => ({
    sessionViewCache: {
      ...state.sessionViewCache,
      "session-b": {
        ...state.sessionViewCache["session-b"],
        messages: [
          {
            ...message(
              "assistant:run-b",
              "assistant",
              "后台最终内容",
              "run-b",
              "2026-01-01T00:01:01Z",
              "streaming",
            ),
            session_id: "session-b",
          },
        ],
      },
    },
  }))

  assert.deepEqual(
    useChatStore.getState().messages.map((item) => item.content),
    ["当前会话"],
  )
  useChatStore.getState().setActiveSession("session-b")
  const backgroundMessage = useChatStore.getState().messages.find((item) =>
    item.role === "assistant"
  )
  assert.equal(backgroundMessage?.content, "后台最终内容")
  assert.equal(backgroundMessage?.run_id, "run-b")
})

test("initial SSE resumes after the history snapshot without losing later events", () => {
  const oldHistoryEvents = [12, 18, 25]
  const pendingEvents = [26, 27, 28]
  const snapshotEventSeq = 25

  const resumeAfter = sessionEventResumeCursor(snapshotEventSeq)

  assert.equal(
    oldHistoryEvents.some((sequence) => sequence > resumeAfter),
    false,
  )
  assert.deepEqual(
    pendingEvents.filter((sequence) => sequence > resumeAfter),
    pendingEvents,
  )
  assert.equal(
    sessionEventStreamUrl("/api-base/", "session/a", resumeAfter),
    "/api-base/api/sessions/session%2Fa/events?after=25",
  )
  assert.equal(
    sessionEventStreamUrl("/api-base", "session/a", 0),
    "/api-base/api/sessions/session%2Fa/events",
  )
})

test("safe snapshot watermark replays active events and deduplicates a later terminal outbox", () => {
  const snapshotEventSeq = 2
  const persistedTerminal = {
    id: "history:terminal:4",
    run_id: "run-old",
    event_type: "run.completed",
    label: "任务已完成",
    status: "completed" as const,
    timestamp: "2026-01-01T00:00:04Z",
    session_seq: 4,
  }
  const replayedEvents = [
    {
      id: "task:run-active:task.started:3",
      run_id: "run-active",
      event_type: "task.started",
      label: "任务已开始",
      status: "running" as const,
      timestamp: "2026-01-01T00:00:03Z",
      session_seq: 3,
    },
    {
      ...persistedTerminal,
      id: "run:run-old:run.completed:4",
      label: "迟到终态 outbox",
    },
    {
      id: "task:run-active:task.completed:5",
      run_id: "run-active",
      event_type: "task.completed",
      label: "任务执行成功",
      status: "completed" as const,
      timestamp: "2026-01-01T00:00:05Z",
      session_seq: 5,
    },
  ]

  assert.equal(sessionEventResumeCursor(snapshotEventSeq), 2)
  assert.deepEqual(
    replayedEvents
      .filter((event) => (event.session_seq || 0) > sessionEventResumeCursor(snapshotEventSeq))
      .map((event) => event.session_seq),
    [3, 4, 5],
  )

  const merged = replayedEvents.reduce(
    (entries, event) => upsertTimelineEntry(entries, {}, event),
    [persistedTerminal],
  )
  assert.equal(
    merged.filter((entry) => entry.session_seq === 4).length,
    1,
  )
  assert.equal(
    merged.some((entry) =>
      entry.run_id === "run-active"
      && entry.session_seq === 3
      && entry.status === "running"
    ),
    true,
  )
  assert.equal(
    merged.some((entry) =>
      entry.run_id === "run-active"
      && entry.session_seq === 5
      && entry.status === "completed"
    ),
    true,
  )
})

test("replayed timeline waits for its terminal run to be loaded by history pagination", () => {
  const firstPage: SessionHistoryTurn = {
    run_id: "run-page-1",
    user: {
      time: "2026-01-01T00:00:02Z",
      content: "第一页问题",
      id: "message-page-1",
    },
    root_process: [],
    tasks: [],
    assistant: {
      time: "2026-01-01T00:00:03Z",
      content: "第一页答复",
    },
  }
  const secondPage: SessionHistoryTurn = {
    run_id: "run-page-2",
    user: {
      time: "2026-01-01T00:00:00Z",
      content: "第二页问题",
      id: "message-page-2",
    },
    root_process: [],
    tasks: [],
    assistant: {
      time: "2026-01-01T00:00:01Z",
      content: "第二页答复",
    },
  }
  const replayedEntries = [
    {
      id: "task:run-page-1",
      run_id: "run-page-1",
      event_type: "task.started",
      label: "第一页任务",
      status: "completed" as const,
      timestamp: "2026-01-01T00:00:03Z",
      session_seq: 4,
    },
    {
      id: "task:run-page-2",
      run_id: "run-page-2",
      event_type: "task.completed",
      label: "低水位重放的旧终态任务",
      status: "completed" as const,
      timestamp: "2026-01-01T00:00:01Z",
      session_seq: 3,
    },
    {
      id: "task:run-active",
      run_id: "run-active",
      event_type: "task.started",
      label: "活动任务",
      status: "running" as const,
      timestamp: "2026-01-01T00:00:05Z",
      session_seq: 5,
    },
  ]
  const firstPageMessages = mergeHistoryMessages(
    historyMessages([firstPage], "session-a"),
    [],
  )

  const beforePagination = filterChatTimelineForLoadedRuns(
    replayedEntries,
    firstPageMessages,
    ["run-active"],
  )
  assert.deepEqual(
    beforePagination.map((entry) => entry.run_id),
    ["run-page-1", "run-active"],
  )

  const afterPaginationMessages = mergeHistoryMessages(
    firstPageMessages,
    historyMessages([secondPage], "session-a"),
  )
  const afterPagination = filterChatTimelineForLoadedRuns(
    replayedEntries,
    afterPaginationMessages,
    ["run-active"],
  )
  assert.deepEqual(
    afterPagination.map((entry) => entry.run_id),
    ["run-page-1", "run-page-2", "run-active"],
  )
  assert.equal(
    afterPaginationMessages.find((item) =>
      item.run_id === "run-page-2" && item.role === "assistant"
    )?.content,
    "第二页答复",
  )
})

test("an existing background or processing cursor wins over a stale snapshot", () => {
  assert.equal(sessionEventResumeCursor(25, 0, 28), 28)
  assert.equal(sessionEventResumeCursor(25, 31, 28), 31)
  assert.equal(sessionEventResumeCursor(40, 31, 28), 31)
  assert.equal(sessionEventResumeCursor(undefined, 0, 0), 0)
})

test("a late history snapshot cannot advance the first SSE baseline", () => {
  useChatStore.setState({ sessionEventCursors: {} })
  const store = useChatStore.getState()

  store.setSessionEventCursor("session-a", 25)
  store.setSessionEventCursor("session-a", 40)

  assert.equal(useChatStore.getState().sessionEventCursors["session-a"], 25)
})

test("a completed background run waits for the final answer in either event order", () => {
  assert.equal(backgroundSessionCanRelease("running", false), false)
  assert.equal(backgroundSessionCanRelease("completed", false), false)
  assert.equal(backgroundSessionCanRelease("completed", true), true)
  assert.equal(backgroundSessionCanRelease("failed", false), true)
})
