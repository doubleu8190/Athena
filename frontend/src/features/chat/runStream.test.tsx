import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  FakeEventSource,
  fakeEventSourceFactory,
} from "../../test/fakeEventSource";
import { getRequestUrl, makeMessage } from "../../test/fixtures";
import { jsonResponse } from "../../test/testUtils";
import type {
  RunEvent,
  RunSnapshotPayload,
  RunStatus,
} from "./model";
import {
  INITIAL_RUN_STREAM_STATE,
  projectRunStatus,
  reconcileFailedRun,
  runStreamReducer,
  type RunStreamAction,
} from "./runStream";
import { useRunStream } from "./useRunStream";

beforeEach(() => {
  FakeEventSource.instances = [];
});

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

describe("原生 EventSource Run hook", () => {
  it("失败快照解析并保留权威报告 ID", () => {
    const { result } = renderHook(() =>
      useRunStream({
        eventSourceFactory: fakeEventSourceFactory,
        runId: "run-1",
        sessionId: "session-1",
        status: "running",
      }),
    );
    const source = FakeEventSource.instances.at(-1);

    act(() => {
      source?.emit("snapshot", {
        payload: snapshot("failed", "", "msg-report-snapshot"),
      });
    });

    expect(result.current.state.status).toBe("failed");
    expect(result.current.state.reportMessageId).toBe(
      "msg-report-snapshot",
    );
    expect(source?.closed).toBe(true);
  });

  it("running 时收到中断会主动换新连接，由新快照校正文本并阻止终态重连", async () => {
    const { result } = renderHook(() =>
      useRunStream({
        eventSourceFactory: fakeEventSourceFactory,
        runId: "run-1",
        sessionId: "session-1",
        status: "running",
      }),
    );
    const firstSource = FakeEventSource.instances.at(-1);
    expect(firstSource?.url).toBe(
      "/api/v1/sessions/session-1/runs/run-1/stream",
    );

    act(() => {
      firstSource?.emitOpen();
      firstSource?.emit("snapshot", {
        payload: snapshot("running", "初始权威回答"),
      });
      firstSource?.emit("text_delta", {
        payload: { round: 0, text: "临时" },
      });
    });
    expect(result.current.state.transientText).toBe("临时");

    act(() => {
      firstSource?.emit("stream_interrupted", {
        payload: { reason: "stream_gap" },
      });
    });
    expect(result.current.state.transientText).toBe("临时");
    expect(result.current.state.notice).toContain("重新同步");
    await waitFor(() => {
      expect(FakeEventSource.instances).toHaveLength(2);
    });
    expect(firstSource?.closed).toBe(true);
    const replacement = FakeEventSource.instances.at(-1);
    expect(replacement).not.toBe(firstSource);
    expect(replacement?.url).toBe(firstSource?.url);

    act(() => {
      // 中断后的旧连接事件必须忽略，不能污染新连接等待快照的文本。
      firstSource?.emit("text_delta", {
        payload: { text: "旧连接迟到文本" },
      });
      replacement?.emit("snapshot", {
        payload: snapshot("running", "权威回答"),
      });
    });
    expect(result.current.state.transientText).toBe("");
    expect(result.current.state.messages[0].text).toBe("权威回答");
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
    expect(replacement?.closed).toBe(true);
    expect(FakeEventSource.instances).toHaveLength(2);
    expect(result.current.state.connection).toBe("closed");
    expect(result.current.state.notice).toBeNull();
  });

  it("新连接首个快照前收到中断不会再次换线，避免重连风暴", () => {
    const { result } = renderHook(() =>
      useRunStream({
        eventSourceFactory: fakeEventSourceFactory,
        runId: "run-1",
        sessionId: "session-1",
        status: "running",
      }),
    );
    const source = FakeEventSource.instances.at(-1);

    act(() => {
      source?.emit("stream_interrupted", {
        payload: { reason: "redis_unavailable" },
      });
      source?.emit("stream_interrupted", {
        payload: { reason: "redis_unavailable" },
      });
    });

    expect(FakeEventSource.instances).toHaveLength(1);
    expect(source?.closed).toBe(false);
    expect(result.current.state.notice).toContain("重新同步");

    act(() => {
      source?.emit("snapshot", {
        payload: snapshot("running", "重连后的权威回答"),
      });
    });
    expect(result.current.state.notice).toBeNull();
    expect(result.current.state.messages[0].text).toBe("重连后的权威回答");
  });

  it("快照等待态关闭后不重连，重新 queued 会重连；终态和卸载不再重连", () => {
    const { rerender, unmount } = renderHook(
      ({ status }) =>
        useRunStream({
          eventSourceFactory: fakeEventSourceFactory,
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
    const first = FakeEventSource.instances.at(-1);
    act(() => {
      first?.emit("snapshot", {
        payload: snapshot("waiting_for_input"),
      });
    });
    expect(first?.closed).toBe(true);

    rerender({ status: "waiting_for_input" });
    expect(FakeEventSource.instances).toHaveLength(1);

    rerender({ status: "queued" });
    expect(FakeEventSource.instances).toHaveLength(2);
    const second = FakeEventSource.instances.at(-1);

    rerender({ status: "completed" });
    expect(second?.closed).toBe(true);
    expect(FakeEventSource.instances).toHaveLength(2);

    unmount();
    expect(second?.closed).toBe(true);
  });

  it("heartbeat 只保活，run_event 触发对账", () => {
    const onReconcile = vi.fn();
    const { result } = renderHook(() =>
      useRunStream({
        eventSourceFactory: fakeEventSourceFactory,
        onReconcile,
        runId: "run-1",
        sessionId: "session-1",
        status: "queued",
      }),
    );
    const source = FakeEventSource.instances.at(-1);

    act(() => {
      source?.emit("heartbeat", {
        payload: { at: "2026-11-10T03:01:00Z" },
      });
    });
    expect(result.current.state.status).toBe("queued");
    expect(result.current.state.lastHeartbeatAt).toBe(
      "2026-11-10T03:01:00Z",
    );
    expect(onReconcile).not.toHaveBeenCalled();

    act(() => {
      source?.emit("run_event", {
        payload: { event: event("started", 1) },
      });
    });
    expect(result.current.state.status).toBe("running");
    expect(onReconcile).toHaveBeenCalledTimes(1);
  });

  it("EventSource error 显示断线提示但保留当前文本", () => {
    const { result } = renderHook(() =>
      useRunStream({
        eventSourceFactory: fakeEventSourceFactory,
        runId: "run-1",
        sessionId: "session-1",
        status: "running",
      }),
    );
    const source = FakeEventSource.instances.at(-1);

    act(() => {
      source?.emit("text_delta", {
        payload: { text: "断线前文本" },
      });
      source?.emitError();
    });

    expect(result.current.state.transientText).toBe("断线前文本");
    expect(result.current.state.connection).toBe("error");
    expect(result.current.state.notice).toContain("实时连接中断");
  });
});

