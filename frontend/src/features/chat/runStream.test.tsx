import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AUTH_UNAUTHORIZED_EVENT } from "../../shared/api/client";
import {
  FakeSseStream,
  createSseStreamResponse,
} from "../../test/fakeSseStream";
import {
  getRequestUrl,
  makeMessage,
} from "../../test/fixtures";
import { jsonResponse } from "../../test/testUtils";
import type {
  RunEvent,
  RunSnapshotPayload,
  RunStatus,
} from "./model";
import {
  INITIAL_RUN_STREAM_STATE,
  projectRunStatus,
  reconcileCompletedRun,
  reconcileFailedRun,
  runStreamReducer,
  type RunStreamAction,
} from "./runStream";
import { useRunStream } from "./useRunStream";

beforeEach(() => {
  FakeSseStream.instances = [];
});

afterEach(() => {
  FakeSseStream.instances = [];
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function flushStreamTasks(turns = 6): Promise<void> {
  await act(async () => {
    for (let index = 0; index < turns; index += 1) {
      await Promise.resolve();
    }
  });
}

function createFakeSseFetch() {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
    createSseStreamResponse(getRequestUrl(input), init),
  );
}

function event(
  type: string,
  sequence: number,
  payload: Record<string, unknown> = {},
): RunEvent {
  return {
    created_at: "2026-11-10T03:00:00Z",
    id: `event-${sequence}`,
    payload,
    run_id: "run-1",
    sequence,
    type,
  };
}

function snapshot(
  status: RunSnapshotPayload["status"],
  text = "",
  reportMessageId: string | null = null,
): RunSnapshotPayload {
  return {
    events: [],
    event_sequence: 4,
    events_after_sequence: null,
    events_complete: true,
    messages: text
      ? [
          {
            created_at: "2026-11-10T03:00:00Z",
            id: "msg-assistant",
            role: "assistant",
            sequence: 2,
            status: "completed",
            text,
          },
        ]
      : [],
    messages_before_sequence: null,
    messages_complete: true,
    report_message_id: reportMessageId,
    status,
  };
}

function reduce(actions: RunStreamAction[]) {
  return actions.reduce(
    runStreamReducer,
    { ...INITIAL_RUN_STREAM_STATE, runId: "run-1" },
  );
}

describe("Run 状态投影", () => {
  it("覆盖 queued → running → waiting → cancelling → 终态", () => {
    const projected = reduce([
      { event: event("queued", 1), type: "run_event" },
      { event: event("started", 2), type: "run_event" },
      { event: event("waiting_for_input", 3), type: "run_event" },
      { event: event("cancel_requested", 4), type: "run_event" },
    ]);
    expect(projected.status).toBe("cancelling");
    expect(projected.status).not.toBe("cancelled");

    const terminal = runStreamReducer(projected, {
      event: event("cancelled", 5),
      type: "run_event",
    });
    expect(terminal.status).toBe("cancelled");
  });

  it("非终态 snapshot 权威覆盖 text_delta，interrupted 保留临时文本", () => {
    const streaming = reduce([
      { runId: "run-1", status: "running", type: "start" },
      { text: "临时输出", type: "text_delta" },
    ]);
    const interrupted = runStreamReducer(streaming, {
      reason: "stream_gap",
      type: "stream_interrupted",
    });
    expect(interrupted.transientText).toBe("临时输出");
    expect(interrupted.notice).toContain("重新同步");

    const corrected = runStreamReducer(interrupted, {
      payload: snapshot("running", "权威进度回答"),
      type: "snapshot",
    });
    expect(corrected.transientText).toBe("");
    expect(corrected.messages[0].text).toBe("权威进度回答");
    expect(corrected.connection).toBe("open");
    expect(corrected.notice).toBeNull();
  });

  it("终态 snapshot 与先到终态事件都保留 transient，completed 只能显式 settle", () => {
    const streaming = reduce([
      { runId: "run-1", status: "running", type: "start" },
      { text: "不能丢失的未完成回答", type: "text_delta" },
    ]);

    for (const terminalStatus of [
      "completed",
      "failed",
      "cancelled",
    ] as const) {
      const snapshotTerminal = runStreamReducer(streaming, {
        payload: snapshot(terminalStatus, "旧交付消息"),
        type: "snapshot",
      });
      expect(snapshotTerminal.status).toBe(terminalStatus);
      expect(snapshotTerminal.connection).toBe("closed");
      expect(snapshotTerminal.transientText).toBe("不能丢失的未完成回答");
      expect(snapshotTerminal.messages[0].text).toBe("旧交付消息");
    }

    const eventTerminal = runStreamReducer(streaming, {
      event: event("failed", 5),
      type: "run_event",
    });
    const queryTerminal = runStreamReducer(eventTerminal, {
      runId: "run-1",
      status: "failed",
      type: "query_terminal",
    });
    expect(queryTerminal.transientText).toBe("不能丢失的未完成回答");

    const completedSnapshot = runStreamReducer(streaming, {
      payload: snapshot("completed", "旧交付消息"),
      type: "snapshot",
    });
    const settled = runStreamReducer(completedSnapshot, {
      runId: "run-1",
      status: "completed",
      type: "settle_terminal",
    });
    expect(settled.transientText).toBe("");
  });

  it("查询终态覆盖残留 SSE running，非终态仍保留 SSE 实时投影", () => {
    expect(
      projectRunStatus({
        queryStatus: "completed",
        runId: "run-1",
        streamRunId: "run-1",
        streamStatus: "running",
      }),
    ).toBe("completed");
    expect(
      projectRunStatus({
        queryStatus: "running",
        runId: "run-1",
        streamRunId: "run-1",
        streamStatus: "cancelling",
      }),
    ).toBe("cancelling");
    expect(
      projectRunStatus({
        queryStatus: "running",
        runId: "run-2",
        streamRunId: "run-1",
        streamStatus: "completed",
      }),
    ).toBe("running");
  });

  it("failed snapshot 和 failed 事件都保留同一权威报告标识", () => {
    const snapshotState = runStreamReducer(
      { ...INITIAL_RUN_STREAM_STATE, runId: "run-1" },
      {
        payload: snapshot("failed", "", "msg-report"),
        type: "snapshot",
      },
    );
    expect(snapshotState.reportMessageId).toBe("msg-report");

    const eventState = runStreamReducer(
      { ...INITIAL_RUN_STREAM_STATE, runId: "run-1" },
      {
        event: event("failed", 5, {
          report_message_id: "msg-report-event",
          safe_error_code: "run_context_safety_stop",
        }),
        type: "run_event",
      },
    );
    expect(eventState.reportMessageId).toBe("msg-report-event");
  });

  it("查询终态先关闭连接并保留草稿，持久消息对账后才清除 transient", () => {
    const streaming = reduce([
      { runId: "run-1", status: "running", type: "start" },
      { text: "待对账草稿", type: "text_delta" },
    ]);
    const closed = runStreamReducer(streaming, {
      runId: "run-1",
      status: "completed",
      type: "query_terminal",
    });
    expect(closed.connection).toBe("closed");
    expect(closed.transientText).toBe("待对账草稿");

    const settled = runStreamReducer(closed, {
      runId: "run-1",
      status: "completed",
      type: "settle_terminal",
    });
    expect(settled.connection).toBe("closed");
    expect(settled.transientText).toBe("");
  });

  it("切换到新 Run 时清空旧 SSE，并恢复查询等待状态", () => {
    const stale = runStreamReducer(
      { ...INITIAL_RUN_STREAM_STATE, runId: "run-1" },
      {
        payload: snapshot("completed", "旧回答"),
        type: "snapshot",
      },
    );
    const switched = runStreamReducer(stale, {
      runId: "run-2",
      status: "waiting_for_input",
      type: "start",
    });
    expect(switched.runId).toBe("run-2");
    expect(switched.status).toBe("waiting_for_input");
    expect(switched.messages).toEqual([]);
    expect(switched.events).toEqual([]);
  });

  it("runStreamReducer 保留 heartbeat action 的纯函数更新契约", () => {
    const updated = runStreamReducer(
      { ...INITIAL_RUN_STREAM_STATE, runId: "run-1", status: "running" },
      { at: "2026-11-10T03:01:00Z", type: "heartbeat" },
    );
    expect(updated.lastHeartbeatAt).toBe("2026-11-10T03:01:00Z");
    expect(updated.status).toBe("running");
  });
});

describe("完成态精确对账与内存事件复用", () => {
  it("knownEvents 已含当前 Run 的 completed 事件时直接跳过 /events 分页请求", async () => {
    const trigger = makeMessage({
      id: "msg-completed-trigger",
      role: "user",
      run_id: null,
      sequence: 1,
      session_id: "session-1",
    });
    const finalAnswer = makeMessage({
      id: "msg-completed-answer",
      role: "assistant",
      run_id: "run-1",
      sequence: 2,
      session_id: "session-1",
      status: "completed",
    });
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(getRequestUrl(input), "http://localhost");
      if (url.pathname.endsWith("/events")) {
        throw new Error("命中内存 completed 事件时不应请求 /events");
      }
      return jsonResponse({
        items: [finalAnswer, trigger],
        next_before_sequence: null,
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await reconcileCompletedRun(
      "session-1",
      "run-1",
      trigger.id,
      [
        event("started", 1),
        event("completed", 2, { assistant_message_id: finalAnswer.id }),
      ],
    );

    expect(result.kind).toBe("settled");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(
      new URL(getRequestUrl(fetchMock.mock.calls[0][0]), "http://localhost")
        .pathname,
    ).toBe("/api/v1/sessions/session-1/messages");
  });

  it("knownEvents 未命中当前 Run 的 completed 事件时回退分页拉取 /events", async () => {
    const trigger = makeMessage({
      id: "msg-fallback-trigger",
      role: "user",
      run_id: null,
      sequence: 1,
      session_id: "session-1",
    });
    const finalAnswer = makeMessage({
      id: "msg-fallback-answer",
      role: "assistant",
      run_id: "run-1",
      sequence: 2,
      session_id: "session-1",
      status: "completed",
    });
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(getRequestUrl(input), "http://localhost");
      if (url.pathname.endsWith("/events")) {
        return jsonResponse({
          items: [
            event("completed", 3, { assistant_message_id: finalAnswer.id }),
          ],
          next_after_sequence: null,
        });
      }
      return jsonResponse({
        items: [finalAnswer, trigger],
        next_before_sequence: null,
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    // 传入其他 Run 的 completed 事件不应被误复用。
    const otherRunEvent: RunEvent = {
      ...event("completed", 2, { assistant_message_id: "msg-other" }),
      run_id: "run-other",
    };
    const result = await reconcileCompletedRun(
      "session-1",
      "run-1",
      trigger.id,
      [otherRunEvent],
    );

    expect(result.kind).toBe("settled");
    expect(
      fetchMock.mock.calls.filter(([input]) =>
        new URL(getRequestUrl(input), "http://localhost").pathname.endsWith(
          "/events",
        ),
      ),
    ).toHaveLength(1);
  });
});

describe("失败报告精确对账", () => {
  it("跨事件页找到报告 ID，再跨消息页校验 Run、触发关联和序号", async () => {
    const trigger = makeMessage({
      id: "msg-trigger",
      role: "user",
      run_id: null,
      sequence: 5,
      session_id: "session-1",
    });
    const report = makeMessage({
      id: "msg-report",
      reply_to_message_id: trigger.id,
      role: "assistant",
      run_id: "run-1",
      sequence: 6,
      session_id: "session-1",
      status: "completed",
    });
    const unrelatedDelivery = makeMessage({
      id: "msg-delivery",
      reply_to_message_id: null,
      role: "assistant",
      run_id: "run-1",
      sequence: 9,
      session_id: "session-1",
    });
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        if (url.pathname.endsWith("/events")) {
          const after = Number(url.searchParams.get("after_sequence"));
          return after === 0
            ? jsonResponse({
                items: [event("started", 1)],
                next_after_sequence: 1,
              })
            : jsonResponse({
                items: [
                  event("failed", 2, {
                    report_message_id: report.id,
                    safe_error_code: "run_context_safety_stop",
                  }),
                ],
                next_after_sequence: null,
              });
        }
        const before = url.searchParams.get("before_sequence");
        if (before === null) {
          return jsonResponse({
            items: [unrelatedDelivery],
            next_before_sequence: 9,
          });
        }
        if (before === "9") {
          return jsonResponse({
            items: [report],
            next_before_sequence: 6,
          });
        }
        return jsonResponse({
          items: [trigger],
          next_before_sequence: null,
        });
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await reconcileFailedRun(
      "session-1",
      "run-1",
      trigger.id,
      null,
    );

    expect(result.kind).toBe("settled");
    if (result.kind === "settled") {
      expect(result.messages.map((item) => item.page)).toEqual([
        expect.objectContaining({ items: [unrelatedDelivery] }),
        expect.objectContaining({ items: [report] }),
        expect.objectContaining({ items: [trigger] }),
      ]);
    }
  });

  it.each([
    ["非 assistant", { role: "user" }],
    ["错误 Run", { run_id: "run-other" }],
    ["未完成", { status: "streaming" }],
    ["触发关联错误", { reply_to_message_id: "msg-other-trigger" }],
    ["早于触发消息", { sequence: 4 }],
  ])("拒绝%s的报告身份", async (_label, reportOverride) => {
    const trigger = makeMessage({
      id: "msg-trigger",
      role: "user",
      run_id: null,
      sequence: 5,
      session_id: "session-1",
    });
    const report = {
      ...makeMessage({
        id: "msg-report",
        reply_to_message_id: trigger.id,
        role: "assistant",
        run_id: "run-1",
        sequence: 6,
        session_id: "session-1",
        status: "completed",
      }),
      ...reportOverride,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({
          items: [report, trigger],
          next_before_sequence: null,
        }),
      ),
    );

    await expect(
      reconcileFailedRun(
        "session-1",
        "run-1",
        trigger.id,
        report.id,
      ),
    ).resolves.toEqual({
      kind: "not_found",
      reason: "report_message",
    });
  });

  it("普通 failed 没有报告事件时不猜测 latest assistant", async () => {
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL) =>
        jsonResponse({
          items: [event("failed", 2, { safe_error_code: "ordinary_failure" })],
          next_after_sequence: null,
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      reconcileFailedRun("session-1", "run-1", "msg-trigger", null),
    ).resolves.toEqual({
      kind: "not_found",
      reason: "failed_event",
    });
    expect(
      fetchMock.mock.calls.every(([input]) =>
        new URL(getRequestUrl(input), "http://localhost").pathname.endsWith(
          "/events",
        ),
      ),
    ).toBe(true);
  });
});

describe("fetch + eventsource-parser Run SSE hook", () => {
  it("失败快照解析并保留权威报告 ID 且立即关闭流", async () => {
    const sseFetch = createFakeSseFetch();
    const { result } = renderHook(() =>
      useRunStream({
        fetch: sseFetch,
        runId: "run-1",
        sessionId: "session-1",
        status: "running",
      }),
    );
    await flushStreamTasks();
    const source = FakeSseStream.instances.at(-1);

    act(() => {
      source?.emit("snapshot", {
        payload: snapshot("failed", "", "msg-report-snapshot"),
      });
    });

    await waitFor(() => {
      expect(result.current.state.status).toBe("failed");
    });
    expect(result.current.state.reportMessageId).toBe("msg-report-snapshot");
    expect(source?.closed).toBe(true);
  });

  it("running 时收到中断会主动换新连接（无旧 Last-Event-ID），由新快照校正文本并阻止终态重连", async () => {
    const sseFetch = createFakeSseFetch();
    const { result } = renderHook(() =>
      useRunStream({
        fetch: sseFetch,
        runId: "run-1",
        sessionId: "session-1",
        status: "running",
      }),
    );
    await flushStreamTasks();
    const firstSource = FakeSseStream.instances.at(-1);
    expect(firstSource?.url).toBe(
      "/api/v1/sessions/session-1/runs/run-1/stream",
    );

    act(() => {
      firstSource?.emit(
        "snapshot",
        { payload: snapshot("running", "初始权威回答") },
        "10",
      );
      firstSource?.emit("text_delta", { payload: { round: 0, text: "临时" } }, "11");
    });
    await waitFor(() => {
      expect(result.current.state.transientText).toBe("临时");
    });

    act(() => {
      firstSource?.emit("stream_interrupted", {
        payload: { reason: "stream_gap" },
      });
    });
    await waitFor(() => {
      expect(FakeSseStream.instances).toHaveLength(2);
    });
    expect(result.current.state.transientText).toBe("临时");
    expect(result.current.state.notice).toContain("重新同步");
    expect(firstSource?.closed).toBe(true);
    const replacement = FakeSseStream.instances.at(-1);
    expect(replacement).not.toBe(firstSource);
    expect(replacement?.url).toBe(firstSource?.url);
    // 换线重连必须是全新连接，不携带旧 Last-Event-ID 以请求服务端重新推送权威 snapshot。
    expect(
      new Headers(replacement?.init?.headers).get("Last-Event-ID"),
    ).toBeNull();

    act(() => {
      // 中断后的旧连接事件必须忽略，不能污染新连接等待快照的文本。
      firstSource?.emit("text_delta", {
        payload: { text: "旧连接迟到文本" },
      });
      replacement?.emit("snapshot", {
        payload: snapshot("running", "权威回答"),
      });
    });
    await waitFor(() => {
      expect(result.current.state.messages[0]?.text).toBe("权威回答");
    });
    expect(result.current.state.transientText).toBe("");
    expect(result.current.state.notice).toBeNull();
    expect(result.current.state.connection).toBe("open");
    expect(replacement?.closed).toBe(false);

    act(() => {
      replacement?.emit("snapshot", {
        payload: snapshot("completed", "权威回答"),
      });
      replacement?.emit("stream_interrupted", {
        payload: { reason: "stream_gap" },
      });
    });
    await waitFor(() => {
      expect(result.current.state.connection).toBe("closed");
    });
    expect(replacement?.closed).toBe(true);
    expect(FakeSseStream.instances).toHaveLength(2);
    expect(result.current.state.notice).toBeNull();
  });

  it("新连接首个快照前收到中断不会再次换线，避免重连风暴", async () => {
    const sseFetch = createFakeSseFetch();
    const { result } = renderHook(() =>
      useRunStream({
        fetch: sseFetch,
        runId: "run-1",
        sessionId: "session-1",
        status: "running",
      }),
    );
    await flushStreamTasks();
    const source = FakeSseStream.instances.at(-1);

    act(() => {
      source?.emit("stream_interrupted", {
        payload: { reason: "redis_unavailable" },
      });
      source?.emit("stream_interrupted", {
        payload: { reason: "redis_unavailable" },
      });
    });

    await waitFor(() => {
      expect(result.current.state.notice).toContain("重新同步");
    });
    expect(FakeSseStream.instances).toHaveLength(1);
    expect(source?.closed).toBe(false);

    act(() => {
      source?.emit("snapshot", {
        payload: snapshot("running", "重连后的权威回答"),
      });
    });
    await waitFor(() => {
      expect(result.current.state.messages[0]?.text).toBe("重连后的权威回答");
    });
    expect(result.current.state.notice).toBeNull();
  });

  it("快照等待态关闭后不重连，重新 queued 会重连；终态和卸载不再重连", async () => {
    const sseFetch = createFakeSseFetch();
    const { rerender, unmount } = renderHook(
      ({ status }) =>
        useRunStream({
          fetch: sseFetch,
          runId: "run-1",
          sessionId: "session-1",
          status,
        }),
      {
        initialProps: {
          status: "queued" as RunStatus,
        },
      },
    );
    await flushStreamTasks();
    const first = FakeSseStream.instances.at(-1);
    act(() => {
      first?.emit("snapshot", {
        payload: snapshot("waiting_for_input"),
      });
    });
    await waitFor(() => {
      expect(first?.closed).toBe(true);
    });

    rerender({ status: "waiting_for_input" });
    await flushStreamTasks();
    expect(FakeSseStream.instances).toHaveLength(1);

    rerender({ status: "queued" });
    await waitFor(() => {
      expect(FakeSseStream.instances).toHaveLength(2);
    });
    const second = FakeSseStream.instances.at(-1);

    rerender({ status: "completed" });
    await waitFor(() => {
      expect(second?.closed).toBe(true);
    });
    expect(FakeSseStream.instances).toHaveLength(2);

    unmount();
    expect(second?.closed).toBe(true);
  });

  it("heartbeat 写入 ref 保活且不触发重渲染，run_event 携带事件类型触发对账", async () => {
    const sseFetch = createFakeSseFetch();
    const onReconcile = vi.fn();
    let renderCount = 0;
    const { result } = renderHook(() => {
      renderCount += 1;
      return useRunStream({
        fetch: sseFetch,
        onReconcile,
        runId: "run-1",
        sessionId: "session-1",
        status: "queued",
      });
    });
    await waitFor(() => {
      expect(result.current.state.connection).toBe("open");
    });
    const source = FakeSseStream.instances.at(-1);
    const rendersBeforeHeartbeat = renderCount;

    act(() => {
      source?.emit("heartbeat", {
        payload: { at: "2026-11-10T03:01:00Z" },
      });
      source?.emit("heartbeat", {
        payload: { at: "2026-11-10T03:01:15Z" },
      });
    });
    await flushStreamTasks();
    expect(result.current.state.status).toBe("queued");
    expect(result.current.lastHeartbeatAtRef.current).toBe(
      "2026-11-10T03:01:15Z",
    );
    expect(renderCount).toBe(rendersBeforeHeartbeat);
    expect(onReconcile).not.toHaveBeenCalled();

    act(() => {
      source?.emit("run_event", {
        payload: { event: event("started", 1) },
      });
    });
    await waitFor(() => {
      expect(result.current.state.status).toBe("running");
    });
    expect(onReconcile).toHaveBeenCalledTimes(1);
    expect(onReconcile).toHaveBeenLastCalledWith({ eventType: "started" });
  });

  it("同一帧内连续多个 text_delta 合并为 1 次 state 更新，切 Run 与卸载时取消挂起帧", async () => {
    const queuedFrames = new Map<number, FrameRequestCallback>();
    let nextFrameId = 1;
    const rafSpy = vi
      .spyOn(window, "requestAnimationFrame")
      .mockImplementation((callback: FrameRequestCallback) => {
        const id = nextFrameId;
        nextFrameId += 1;
        queuedFrames.set(id, callback);
        return id;
      });
    const cancelRafSpy = vi
      .spyOn(window, "cancelAnimationFrame")
      .mockImplementation((id: number) => {
        queuedFrames.delete(id);
      });

    try {
      const sseFetch = createFakeSseFetch();
      let renderCount = 0;
      const { rerender, result, unmount } = renderHook(
        ({ runId }) => {
          renderCount += 1;
          return useRunStream({
            fetch: sseFetch,
            runId,
            sessionId: "session-1",
            status: "running",
          });
        },
        { initialProps: { runId: "run-1" } },
      );
      await waitFor(() => {
        expect(result.current.state.connection).toBe("open");
      });
      const firstSource = FakeSseStream.instances.at(-1);
      const rendersBeforeBurst = renderCount;

      // 同一帧内收到 4 段增量：仅预约 1 个 rAF，且尚未触发重渲染。
      act(() => {
        firstSource?.emit("text_delta", { payload: { text: "第" } });
        firstSource?.emit("text_delta", { payload: { text: "一" } });
        firstSource?.emit("text_delta", { payload: { text: "帧" } });
        firstSource?.emit("text_delta", { payload: { text: "合并" } });
      });
      await flushStreamTasks();
      expect(rafSpy).toHaveBeenCalledTimes(1);
      expect(renderCount).toBe(rendersBeforeBurst);
      expect(result.current.state.transientText).toBe("");

      // 帧回调触发后，一次性合并为 1 次 React 更新并完整拼接文本。
      act(() => {
        const entries = Array.from(queuedFrames.entries());
        queuedFrames.clear();
        entries.forEach(([, cb]) => cb(16));
      });
      expect(renderCount).toBe(rendersBeforeBurst + 1);
      expect(result.current.state.transientText).toBe("第一帧合并");

      // 切 Run 前有未刷新的缓冲：必须丢弃并取消挂起帧，不能串染到 run-2。
      act(() => {
        firstSource?.emit("text_delta", { payload: { text: "旧Run未刷片段" } });
      });
      await flushStreamTasks();
      expect(queuedFrames.size).toBe(1);
      rerender({ runId: "run-2" });
      await flushStreamTasks();
      expect(cancelRafSpy).toHaveBeenCalled();
      expect(queuedFrames.size).toBe(0);
      expect(result.current.state.runId).toBe("run-2");
      expect(result.current.state.transientText).toBe("");
      expect(firstSource?.closed).toBe(true);

      // 卸载前有挂起帧：卸载必须取消挂起帧，不遗留泄漏回调。
      const secondSource = FakeSseStream.instances.at(-1);
      act(() => {
        secondSource?.emit("text_delta", { payload: { text: "卸载前未刷片段" } });
      });
      await flushStreamTasks();
      expect(queuedFrames.size).toBe(1);
      unmount();
      expect(queuedFrames.size).toBe(0);
      expect(secondSource?.closed).toBe(true);
    } finally {
      rafSpy.mockRestore();
      cancelRafSpy.mockRestore();
    }
  });

  it("text_delta 紧跟 snapshot 或 run_event 时先同步 flush 缓冲并取消挂起帧", async () => {
    const queuedFrames = new Map<number, FrameRequestCallback>();
    let nextFrameId = 1;
    const rafSpy = vi
      .spyOn(window, "requestAnimationFrame")
      .mockImplementation((callback: FrameRequestCallback) => {
        const id = nextFrameId;
        nextFrameId += 1;
        queuedFrames.set(id, callback);
        return id;
      });
    const cancelRafSpy = vi
      .spyOn(window, "cancelAnimationFrame")
      .mockImplementation((id: number) => {
        queuedFrames.delete(id);
      });

    try {
      const sseFetch = createFakeSseFetch();
      const { result } = renderHook(() =>
        useRunStream({
          fetch: sseFetch,
          runId: "run-1",
          sessionId: "session-1",
          status: "running",
        }),
      );
      await waitFor(() => {
        expect(result.current.state.connection).toBe("open");
      });
      const source = FakeSseStream.instances.at(-1);

      // 1. text_delta 后紧跟非终态权威 snapshot：取消挂起帧并被 snapshot 清空，不能迟到回写。
      act(() => {
        source?.emit("text_delta", { payload: { text: "将被快照覆盖的临时文本" } });
        source?.emit("snapshot", {
          payload: snapshot("running", "权威快照正文"),
        });
      });
      await waitFor(() => {
        expect(result.current.state.messages[0]?.text).toBe("权威快照正文");
      });
      expect(cancelRafSpy).toHaveBeenCalled();
      expect(queuedFrames.size).toBe(0);
      expect(result.current.state.transientText).toBe("");

      // 2. text_delta 后紧跟 run_event：先同步刷入缓冲文本，再处理事件。
      act(() => {
        source?.emit("text_delta", { payload: { text: "事件前缓冲A" } });
        source?.emit("text_delta", { payload: { text: "+B" } });
        source?.emit("run_event", {
          payload: { event: event("tool_call_started", 2) },
        });
      });
      await waitFor(() => {
        expect(result.current.state.events.map((item) => item.type)).toEqual([
          "tool_call_started",
        ]);
      });
      expect(queuedFrames.size).toBe(0);
      expect(result.current.state.transientText).toBe("事件前缓冲A+B");

      // 3. text_delta 后紧跟终态 snapshot：先同步 flush 最后一帧文本，由终态 snapshot 完整保留。
      act(() => {
        source?.emit("text_delta", { payload: { text: "+终态前尾帧" } });
        source?.emit("snapshot", {
          payload: snapshot("completed", "旧交付消息"),
        });
      });
      await waitFor(() => {
        expect(result.current.state.status).toBe("completed");
      });
      expect(queuedFrames.size).toBe(0);
      expect(result.current.state.connection).toBe("closed");
      expect(result.current.state.transientText).toBe("事件前缓冲A+B+终态前尾帧");
    } finally {
      rafSpy.mockRestore();
      cancelRafSpy.mockRestore();
    }
  });

  it("无 requestAnimationFrame 环境兜底通过定时合批 flush text_delta", async () => {
    vi.useFakeTimers();
    const originalRaf = window.requestAnimationFrame;
    const originalCancelRaf = window.cancelAnimationFrame;
    Object.defineProperty(window, "requestAnimationFrame", {
      configurable: true,
      value: undefined,
      writable: true,
    });
    Object.defineProperty(window, "cancelAnimationFrame", {
      configurable: true,
      value: undefined,
      writable: true,
    });

    try {
      const sseFetch = createFakeSseFetch();
      const { result } = renderHook(() =>
        useRunStream({
          fetch: sseFetch,
          runId: "run-1",
          sessionId: "session-1",
          status: "running",
        }),
      );
      await flushStreamTasks();
      const source = FakeSseStream.instances.at(-1);

      act(() => {
        source?.emit("text_delta", { payload: { text: "兜底" } });
        source?.emit("text_delta", { payload: { text: "定时合批" } });
      });
      await flushStreamTasks();
      expect(result.current.state.transientText).toBe("");

      act(() => {
        vi.advanceTimersByTime(16);
      });
      expect(result.current.state.transientText).toBe("兜底定时合批");
    } finally {
      Object.defineProperty(window, "requestAnimationFrame", {
        configurable: true,
        value: originalRaf,
        writable: true,
      });
      Object.defineProperty(window, "cancelAnimationFrame", {
        configurable: true,
        value: originalCancelRaf,
        writable: true,
      });
      vi.useRealTimers();
    }
  });

  it("SSE 握手或重连直接返回 401 时立即发布未认证事件、保留当前文本、永久停流且不轮询 /auth/me", async () => {
    vi.useFakeTimers();
    let attempt = 0;
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = getRequestUrl(input);
        if (url.includes("/auth/me")) {
          throw new Error("升级 fetch SSE 后禁止再轮询 /auth/me");
        }
        attempt += 1;
        if (attempt === 1) {
          return createSseStreamResponse(url, init);
        }
        return jsonResponse({ detail: "not_authenticated" }, 401);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const unauthorizedListener = vi.fn();
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, unauthorizedListener);

    try {
      const { result } = renderHook(() =>
        useRunStream({
          runId: "run-1",
          sessionId: "session-1",
          status: "running",
        }),
      );
      await flushStreamTasks();
      const firstStream = FakeSseStream.instances[0];
      expect(firstStream).toBeDefined();

      // 先接收流式文本，随后服务端断流并在退避重连时直接返回 HTTP 401。
      act(() => {
        firstStream.emit("text_delta", { payload: { text: "会话过期前文本" } });
        firstStream.end();
      });
      await flushStreamTasks();
      expect(result.current.state.transientText).toBe("会话过期前文本");
      expect(result.current.state.connection).toBe("error");
      expect(unauthorizedListener).not.toHaveBeenCalled();

      // 触发 1000ms 退避重连，直接命中 401。
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000);
      });
      await flushStreamTasks();

      expect(unauthorizedListener).toHaveBeenCalledTimes(1);
      expect(result.current.state.connection).toBe("error");
      expect(result.current.state.transientText).toBe("会话过期前文本");
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(
        fetchMock.mock.calls.every(
          ([input]) =>
            getRequestUrl(input) ===
            "/api/v1/sessions/session-1/runs/run-1/stream",
        ),
      ).toBe(true);

      // 401 永久停流：即使继续推进 60 秒也绝不再重连。
      await act(async () => {
        await vi.advanceTimersByTimeAsync(60_000);
      });
      expect(fetchMock).toHaveBeenCalledTimes(2);
    } finally {
      window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, unauthorizedListener);
    }
  });

  it("403 / 404 非瞬态错误永久停流不重连；503 / 断流保留文本并指数退避重连恢复", async () => {
    vi.useFakeTimers();
    const unauthorizedListener = vi.fn();
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, unauthorizedListener);

    try {
      // 场景 1：404 永久停流，不触发未认证也不重连。
      const fetch404 = vi.fn(async () =>
        jsonResponse({ detail: "run_not_found" }, 404),
      );
      const hook404 = renderHook(() =>
        useRunStream({
          fetch: fetch404,
          runId: "run-404",
          sessionId: "session-1",
          status: "running",
        }),
      );
      await flushStreamTasks();
      expect(hook404.result.current.state.connection).toBe("error");
      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
      });
      expect(fetch404).toHaveBeenCalledTimes(1);
      expect(unauthorizedListener).not.toHaveBeenCalled();
      hook404.unmount();

      // 场景 2：断流 + 503 退避重连后恢复，保留文本并由新快照同步。
      let attempt = 0;
      const fetchRecover = vi.fn(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          attempt += 1;
          if (attempt === 1) {
            return createSseStreamResponse(getRequestUrl(input), init);
          }
          if (attempt === 2) {
            return jsonResponse({ detail: "service_unavailable" }, 503);
          }
          return createSseStreamResponse(getRequestUrl(input), init);
        },
      );
      const hookRecover = renderHook(() =>
        useRunStream({
          fetch: fetchRecover,
          runId: "run-recover",
          sessionId: "session-1",
          status: "running",
        }),
      );
      await flushStreamTasks();
      const s1 = FakeSseStream.instances.at(-1)!;
      act(() => {
        s1.emit("text_delta", { payload: { text: "断线保留草稿" } });
        s1.error(new TypeError("network reset"));
      });
      await flushStreamTasks();
      expect(hookRecover.result.current.state.transientText).toBe("断线保留草稿");
      expect(hookRecover.result.current.state.connection).toBe("error");
      expect(hookRecover.result.current.state.notice).toContain("实时连接中断");

      // 1s 后重连命中 503 -> 再 2s 后恢复 200。
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000);
      });
      await flushStreamTasks();
      expect(fetchRecover).toHaveBeenCalledTimes(2);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(2_000);
      });
      await flushStreamTasks();
      expect(fetchRecover).toHaveBeenCalledTimes(3);
      expect(hookRecover.result.current.state.connection).toBe("open");
      expect(unauthorizedListener).not.toHaveBeenCalled();
      hookRecover.unmount();
    } finally {
      window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, unauthorizedListener);
    }
  });
});
