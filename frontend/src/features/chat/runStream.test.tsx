import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AUTH_UNAUTHORIZED_EVENT } from "../../shared/api/client";
import {
  FakeEventSource,
  fakeEventSourceFactory,
} from "../../test/fakeEventSource";
import {
  TEST_USER,
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
  reconcileFailedRun,
  runStreamReducer,
  type RunStreamAction,
} from "./runStream";
import { AUTH_PROBE_COOLDOWN_MS, useRunStream } from "./useRunStream";

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

  it("EventSource error 后探测 /auth/me 返回 401 时派发未认证事件并立刻关闭连接", async () => {
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        jsonResponse({ detail: "authentication_required" }, 401),
    );
    vi.stubGlobal("fetch", fetchMock);
    const unauthorizedListener = vi.fn();
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, unauthorizedListener);

    try {
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
          payload: { text: "会话过期前文本" },
        });
        source?.emitError();
      });

      await waitFor(() => {
        expect(source?.closed).toBe(true);
      });
      expect(unauthorizedListener).toHaveBeenCalledTimes(1);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      const [requestInput, requestInit] = fetchMock.mock.calls[0];
      expect(getRequestUrl(requestInput)).toBe("/api/v1/auth/me");
      expect(requestInit?.method).toBe("GET");
      expect(requestInit?.cache).toBe("no-store");
      expect(requestInit?.signal).toBeInstanceOf(AbortSignal);
      expect(result.current.state.connection).toBe("error");
      expect(result.current.state.transientText).toBe("会话过期前文本");

      // 关闭连接后继续收到迟到 error 或数据片段，不应重复探测或污染文本。
      act(() => {
        source?.emitError();
        source?.emit("text_delta", {
          payload: { text: "迟到片段" },
        });
      });
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(result.current.state.transientText).toBe("会话过期前文本");
    } finally {
      window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, unauthorizedListener);
    }
  });

  it.each([
    [
      "200 身份仍有效",
      async () => jsonResponse(TEST_USER, 200),
    ],
    [
      "网络错误",
      async () => {
        throw new TypeError("Failed to fetch");
      },
    ],
    [
      "503 服务端临时不可用",
      async () => jsonResponse({ detail: "service_unavailable" }, 503),
    ],
  ])(
    "EventSource error 后探测 /auth/me 返回%s时不误登出并保留文本与断线提示",
    async (_label, respond) => {
      let resolveProbeRequest: (() => void) | null = null;
      const probeDone = new Promise<void>((resolve) => {
        resolveProbeRequest = resolve;
      });
      const fetchMock = vi.fn(async () => {
        try {
          return await respond();
        } finally {
          resolveProbeRequest?.();
        }
      });
      vi.stubGlobal("fetch", fetchMock);
      const unauthorizedListener = vi.fn();
      window.addEventListener(AUTH_UNAUTHORIZED_EVENT, unauthorizedListener);

      try {
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
            payload: { text: "瞬态断网保留文本" },
          });
          source?.emitError();
        });

        await probeDone;
        // 等待探测 catch/finally 微任务结算完毕后再断言副作用。
        await act(async () => {
          await Promise.resolve();
          await Promise.resolve();
        });

        expect(fetchMock).toHaveBeenCalledTimes(1);
        expect(unauthorizedListener).not.toHaveBeenCalled();
        expect(source?.closed).toBe(false);
        expect(result.current.state.transientText).toBe("瞬态断网保留文本");
        expect(result.current.state.connection).toBe("error");
        expect(result.current.state.notice).toContain("实时连接中断");
      } finally {
        window.removeEventListener(
          AUTH_UNAUTHORIZED_EVENT,
          unauthorizedListener,
        );
      }
    },
  );

  it("连续高频触发 error 时在途去重与冷却窗口生效，onopen 恢复后可重新探测", async () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-11-23T00:00:00Z"));
      const resolvers: (() => void)[] = [];
      const probeAuth = vi.fn(
        () =>
          new Promise<void>((resolve) => {
            resolvers.push(resolve);
          }),
      );

      renderHook(() =>
        useRunStream({
          eventSourceFactory: fakeEventSourceFactory,
          probeAuth,
          runId: "run-1",
          sessionId: "session-1",
          status: "running",
        }),
      );
      const source = FakeEventSource.instances.at(-1);

      // 首次探测在途期间，重复触发 error 必须被去重。
      act(() => {
        source?.emitError();
        source?.emitError();
        source?.emitError();
      });
      expect(probeAuth).toHaveBeenCalledTimes(1);

      // 首次探测完成后进入冷却窗口，冷却期内不会重复刷 /auth/me。
      await act(async () => {
        resolvers.shift()?.();
        await Promise.resolve();
      });
      act(() => {
        source?.emitError();
      });
      expect(probeAuth).toHaveBeenCalledTimes(1);

      act(() => {
        vi.advanceTimersByTime(AUTH_PROBE_COOLDOWN_MS - 1);
        source?.emitError();
      });
      expect(probeAuth).toHaveBeenCalledTimes(1);

      // 超过冷却窗口后再次出错，允许发起下一轮探测。
      act(() => {
        vi.advanceTimersByTime(1);
        source?.emitError();
      });
      expect(probeAuth).toHaveBeenCalledTimes(2);
      await act(async () => {
        resolvers.shift()?.();
        await Promise.resolve();
      });

      // 冷却期内连接重新 onopen 恢复，会清空冷却并在后续出错时立即探测。
      act(() => {
        source?.emitOpen();
        source?.emitError();
      });
      expect(probeAuth).toHaveBeenCalledTimes(3);

      // 在途探测期间如果连接先恢复 onopen，探测结束后不得重新套用旧冷却锁死后续探测。
      act(() => {
        source?.emitOpen();
      });
      await act(async () => {
        resolvers.shift()?.();
        await Promise.resolve();
      });
      act(() => {
        source?.emitError();
      });
      expect(probeAuth).toHaveBeenCalledTimes(4);
      await act(async () => {
        resolvers.shift()?.();
        await Promise.resolve();
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("组件卸载时通过 AbortSignal 取消在途 /auth/me 探测", () => {
    const signals: AbortSignal[] = [];
    const fetchMock = vi.fn(
      (_input: RequestInfo | URL, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          const signal = init?.signal;
          if (signal) {
            signals.push(signal);
            signal.addEventListener("abort", () => {
              reject(new DOMException("Aborted", "AbortError"));
            });
          }
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { unmount } = renderHook(() =>
      useRunStream({
        eventSourceFactory: fakeEventSourceFactory,
        runId: "run-1",
        sessionId: "session-1",
        status: "running",
      }),
    );
    const source = FakeEventSource.instances.at(-1);

    act(() => {
      source?.emitError();
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(signals).toHaveLength(1);
    expect(signals[0].aborted).toBe(false);

    unmount();

    expect(signals[0].aborted).toBe(true);
    expect(source?.closed).toBe(true);
  });

  it("切换 Run、换线重连或收到终态快照时取消旧连接的在途 /auth/me 探测", async () => {
    const signals: AbortSignal[] = [];
    const probeAuth = vi.fn(
      (signal: AbortSignal) =>
        new Promise<void>((_resolve, reject) => {
          signals.push(signal);
          signal.addEventListener("abort", () => {
            reject(new DOMException("Aborted", "AbortError"));
          });
        }),
    );

    const { rerender } = renderHook(
      ({ runId }) =>
        useRunStream({
          eventSourceFactory: fakeEventSourceFactory,
          probeAuth,
          runId,
          sessionId: "session-1",
          status: "running",
        }),
      {
        initialProps: { runId: "run-1" },
      },
    );
    const firstSource = FakeEventSource.instances.at(-1);

    act(() => {
      firstSource?.emitError();
    });
    expect(signals).toHaveLength(1);
    expect(signals[0].aborted).toBe(false);

    // 切换 Run 时必须关闭旧连接并中止旧探测。
    rerender({ runId: "run-2" });
    expect(signals[0].aborted).toBe(true);
    expect(firstSource?.closed).toBe(true);
    expect(FakeEventSource.instances).toHaveLength(2);

    const secondSource = FakeEventSource.instances.at(-1);
    act(() => {
      secondSource?.emit("snapshot", {
        payload: snapshot("running", "进行中"),
      });
      secondSource?.emitError();
    });
    expect(signals).toHaveLength(2);
    expect(signals[1].aborted).toBe(false);

    // 收到 stream_interrupted 主动换线时，旧连接的在途探测必须立刻中止。
    act(() => {
      secondSource?.emit("stream_interrupted", {
        payload: { reason: "stream_gap" },
      });
    });
    expect(signals[1].aborted).toBe(true);
    expect(secondSource?.closed).toBe(true);
    await waitFor(() => {
      expect(FakeEventSource.instances).toHaveLength(3);
    });

    const thirdSource = FakeEventSource.instances.at(-1);
    act(() => {
      thirdSource?.emitError();
    });
    expect(signals).toHaveLength(3);
    expect(signals[2].aborted).toBe(false);

    // 收到终态快照主动关流时，也必须中止在途身份探测。
    act(() => {
      thirdSource?.emit("snapshot", {
        payload: snapshot("completed", "最终回答"),
      });
    });
    expect(signals[2].aborted).toBe(true);
    expect(thirdSource?.closed).toBe(true);
  });
});

