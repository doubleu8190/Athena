import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  FakeEventSource,
  fakeEventSourceFactory,
} from "../../test/fakeEventSource";
import type {
  RunEvent,
  RunSnapshotPayload,
  RunStatus,
} from "./model";
import {
  INITIAL_RUN_STREAM_STATE,
  runStreamReducer,
  type RunStreamAction,
} from "./runStream";
import { useRunStream } from "./useRunStream";

beforeEach(() => {
  FakeEventSource.instances = [];
});

function event(type: string, sequence: number): RunEvent {
  return {
    created_at: "2026-11-10T03:00:00Z",
    id: `event-${sequence}`,
    payload: {},
    run_id: "run-1",
    sequence,
    type,
  };
}

function snapshot(
  status: RunSnapshotPayload["status"],
  text = "",
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

  it("snapshot 权威覆盖 text_delta，interrupted 保留临时文本", () => {
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
      payload: snapshot("completed", "正式完整回答"),
      type: "snapshot",
    });
    expect(corrected.transientText).toBe("");
    expect(corrected.messages[0].text).toBe("正式完整回答");
    expect(corrected.connection).toBe("closed");
    expect(corrected.notice).toBeNull();
  });
});

describe("原生 EventSource Run hook", () => {
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

