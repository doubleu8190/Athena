import { afterEach, describe, expect, it, vi } from "vitest";

import { FakeSseStream } from "../../test/fakeSseStream";
import {
  ADMIN_FORBIDDEN_EVENT,
  AUTH_UNAUTHORIZED_EVENT,
  advanceAuthIdentityEpoch,
} from "./client";
import {
  connectEventStream,
  type SseErrorInfo,
  type SseMessage,
} from "./sse";

async function flushMicrotasks(turns = 6): Promise<void> {
  for (let index = 0; index < turns; index += 1) {
    await Promise.resolve();
  }
}

afterEach(() => {
  FakeSseStream.instances = [];
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("connectEventStream (fetch + eventsource-parser)", () => {
  it("携带安全请求头与凭证，完整重组跨分片多字节 UTF-8、多行 data、id 与 event", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      return new FakeSseStream(String(input), init).response;
    });
    const events: SseMessage[] = [];
    const onOpen = vi.fn();
    const onError = vi.fn();

    const connection = connectEventStream(
      "/api/v1/sessions/s-1/runs/r-1/stream",
      {
        fetch: fetchMock,
        onError,
        onEvent: (message) => events.push(message),
        onOpen,
      },
    );

    await flushMicrotasks();
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [requestUrl, requestInit] = fetchMock.mock.calls[0];
    expect(requestUrl).toBe("/api/v1/sessions/s-1/runs/r-1/stream");
    expect(requestInit).toMatchObject({
      cache: "no-store",
      credentials: "same-origin",
      method: "GET",
    });
    expect(new Headers(requestInit?.headers).get("Accept")).toBe(
      "text/event-stream",
    );

    const stream = FakeSseStream.instances[0];
    expect(stream).toBeDefined();

    // 构造包含多字节中文字符与 Emoji、且在 UTF-8 字节中间切分的多行 SSE 事件。
    const fullBytes = new TextEncoder().encode(
      "event: text_delta\nid: seq-7\ndata: 第一行中文🚀\ndata: 第二行续写\n\n",
    );
    // 故意在中文与 Emoji 字节边界中间切分分片，验证流式 TextDecoder 不损坏多字节字符。
    const splitPoint = fullBytes.indexOf(0xf0) + 2; // 4 字节 Emoji 🚀 的第 2 字节处
    stream.enqueueBytes(fullBytes.slice(0, splitPoint));
    await flushMicrotasks();
    expect(events).toHaveLength(0);

    stream.enqueueBytes(fullBytes.slice(splitPoint));
    await flushMicrotasks();
    expect(events).toEqual([
      {
        data: "第一行中文🚀\n第二行续写",
        event: "text_delta",
        id: "seq-7",
      },
    ]);
    expect(onError).not.toHaveBeenCalled();

    connection.close();
    expect(connection.closed).toBe(true);
    expect(stream.closed).toBe(true);
  });

  it("401 直接发布 AUTH_UNAUTHORIZED_EVENT 并永久停流、不重连", async () => {
    vi.useFakeTimers();
    const unauthorizedListener = vi.fn();
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, unauthorizedListener);

    const fetchMock = vi.fn(
      async () =>
        new Response(JSON.stringify({ detail: "not_authenticated" }), {
          headers: { "Content-Type": "application/json" },
          status: 401,
        }),
    );
    const errors: SseErrorInfo[] = [];
    const onOpen = vi.fn();

    try {
      const connection = connectEventStream("/api/v1/sessions/s/runs/r/stream", {
        fetch: fetchMock,
        onError: (info) => errors.push(info),
        onEvent: vi.fn(),
        onOpen,
      });

      await flushMicrotasks();
      expect(unauthorizedListener).toHaveBeenCalledTimes(1);
      expect(onOpen).not.toHaveBeenCalled();
      expect(errors).toEqual([
        { fatal: true, reason: "unauthorized", status: 401 },
      ]);
      expect(connection.closed).toBe(true);

      // 推进足够长时间，确认绝不再发起重连请求。
      await vi.advanceTimersByTimeAsync(60_000);
      expect(fetchMock).toHaveBeenCalledTimes(1);
    } finally {
      window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, unauthorizedListener);
    }
  });

  it("403 admin_required 发布去前缀管理权限事件并永久停流；404/400 不重连", async () => {
    vi.useFakeTimers();
    const adminListener = vi.fn();
    window.addEventListener(ADMIN_FORBIDDEN_EVENT, adminListener);

    try {
      const fetch403 = vi.fn(
        async () =>
          new Response(JSON.stringify({ detail: "admin_required" }), {
            headers: { "Content-Type": "application/json" },
            status: 403,
          }),
      );
      const errors403: SseErrorInfo[] = [];
      const conn403 = connectEventStream("/api/v1/admin/stream", {
        fetch: fetch403,
        onError: (info) => errors403.push(info),
        onEvent: vi.fn(),
      });

      await flushMicrotasks();
      expect(conn403.closed).toBe(true);
      expect(errors403).toEqual([
        { fatal: true, reason: "forbidden", status: 403 },
      ]);
      expect(adminListener).toHaveBeenCalledTimes(1);
      expect(
        (adminListener.mock.calls[0]?.[0] as CustomEvent<{ path: string }>)
          .detail,
      ).toEqual({ path: "/admin/stream" });

      const fetch404 = vi.fn(
        async () =>
          new Response(JSON.stringify({ detail: "run_not_found" }), {
            headers: { "Content-Type": "application/json" },
            status: 404,
          }),
      );
      const errors404: SseErrorInfo[] = [];
      const conn404 = connectEventStream("/api/v1/sessions/s/runs/r/stream", {
        fetch: fetch404,
        onError: (info) => errors404.push(info),
        onEvent: vi.fn(),
      });

      await flushMicrotasks();
      expect(conn404.closed).toBe(true);
      expect(errors404).toEqual([
        { fatal: true, reason: "client_error", status: 404 },
      ]);

      await vi.advanceTimersByTimeAsync(60_000);
      expect(fetch403).toHaveBeenCalledTimes(1);
      expect(fetch404).toHaveBeenCalledTimes(1);
    } finally {
      window.removeEventListener(ADMIN_FORBIDDEN_EVENT, adminListener);
    }
  });

  it("5xx、网络故障与非预期断流执行有界指数退避重连，尊重 retry: 提示并携带 Last-Event-ID", async () => {
    vi.useFakeTimers();
    let attempt = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      attempt += 1;
      if (attempt === 1) {
        return new Response("upstream down", { status: 503 });
      }
      if (attempt === 2) {
        throw new TypeError("Failed to fetch");
      }
      if (attempt === 3) {
        return new Response("too many requests", { status: 429 });
      }
      if (attempt === 4) {
        return new FakeSseStream(String(input), init).response;
      }
      if (attempt === 5) {
        return new Response("gateway error", { status: 502 });
      }
      return new FakeSseStream(String(input), init).response;
    });

    const errors: SseErrorInfo[] = [];
    const events: SseMessage[] = [];
    const onOpen = vi.fn();

    const connection = connectEventStream("/api/v1/sessions/s/runs/r/stream", {
      fetch: fetchMock,
      initialRetryMs: 1_000,
      maxRetryMs: 10_000,
      minRetryMs: 500,
      onError: (info) => errors.push(info),
      onEvent: (message) => events.push(message),
      onOpen,
    });

    // 第 1 次：503 -> 退避 1000ms
    await flushMicrotasks();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(errors[0]).toEqual({ fatal: false, reason: "http_error", status: 503 });

    await vi.advanceTimersByTimeAsync(999);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    await flushMicrotasks();
    // 第 2 次：network_error -> 退避 2000ms
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(errors[1]).toEqual({ fatal: false, reason: "network_error" });

    await vi.advanceTimersByTimeAsync(1_999);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    await flushMicrotasks();
    // 第 3 次：429 -> 退避 4000ms
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(errors[2]).toEqual({ fatal: false, reason: "http_error", status: 429 });

    await vi.advanceTimersByTimeAsync(3_999);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(1);
    await flushMicrotasks();
    // 第 4 次：200 打开，退避计数归零；服务端下发 retry: 2500 与 id: 42 后非预期 EOF
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(onOpen).toHaveBeenCalledTimes(1);

    const firstLiveStream = FakeSseStream.instances[0];
    firstLiveStream.enqueueText(
      "retry: 2500\nevent: text_delta\nid: 42\ndata: {\"ok\":true}\n\n",
    );
    await flushMicrotasks();
    expect(events).toHaveLength(1);

    firstLiveStream.end();
    await flushMicrotasks();
    expect(errors[3]).toEqual({ fatal: false, reason: "stream_closed" });

    // 尊重服务端 retry: 2500（归零后的首次重连延迟 2500ms）
    await vi.advanceTimersByTimeAsync(2_499);
    expect(fetchMock).toHaveBeenCalledTimes(4);
    await vi.advanceTimersByTimeAsync(1);
    await flushMicrotasks();
    expect(fetchMock).toHaveBeenCalledTimes(5);
    // 重连请求携带 Last-Event-ID
    expect(
      new Headers(fetchMock.mock.calls[4]?.[1]?.headers).get("Last-Event-ID"),
    ).toBe("42");

    // 第 5 次返回 502 -> 下一次退避为 2500 * 2 = 5000ms
    await vi.advanceTimersByTimeAsync(4_999);
    expect(fetchMock).toHaveBeenCalledTimes(5);
    await vi.advanceTimersByTimeAsync(1);
    await flushMicrotasks();
    expect(fetchMock).toHaveBeenCalledTimes(6);
    expect(onOpen).toHaveBeenCalledTimes(2);

    connection.close();
  });

  it("服务端 retry 提示受上下界约束，连续失败退避不超过 maxRetryMs", async () => {
    vi.useFakeTimers();
    let attempt = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      attempt += 1;
      if (attempt === 1) {
        return new FakeSseStream(String(input), init).response;
      }
      return new Response("err", { status: 500 });
    });

    const connection = connectEventStream("/api/v1/sessions/s/runs/r/stream", {
      fetch: fetchMock,
      initialRetryMs: 1_000,
      maxRetryMs: 10_000,
      minRetryMs: 500,
      onEvent: vi.fn(),
    });

    await flushMicrotasks();
    const stream = FakeSseStream.instances[0];
    // 服务端下发极端过小值 retry: 10，应被下界钳位为 500ms。
    stream.enqueueText("retry: 10\n\n");
    await flushMicrotasks();
    stream.end();
    await flushMicrotasks();

    await vi.advanceTimersByTimeAsync(499);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    await flushMicrotasks();
    expect(fetchMock).toHaveBeenCalledTimes(2);

    // 连续失败：1000 -> 2000 -> 4000 -> 8000 -> 10000（封顶）
    for (const expectedDelay of [1_000, 2_000, 4_000, 8_000, 10_000]) {
      const before = fetchMock.mock.calls.length;
      await vi.advanceTimersByTimeAsync(expectedDelay - 1);
      expect(fetchMock).toHaveBeenCalledTimes(before);
      await vi.advanceTimersByTimeAsync(1);
      await flushMicrotasks();
      expect(fetchMock).toHaveBeenCalledTimes(before + 1);
    }

    connection.close();
  });

  it("身份代次切换时立刻丢弃旧流事件，不派发 401/403 也不重连", async () => {
    vi.useFakeTimers();
    const unauthorizedListener = vi.fn();
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, unauthorizedListener);

    try {
      // 场景 1：fetch 握手期间切换身份，随后旧连接返回 401 不得触发全局未认证事件。
      const resolvers: Array<(response: Response) => void> = [];
      const fetchPending = vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            resolvers.push(resolve);
          }),
      );
      const onError1 = vi.fn();
      connectEventStream("/api/v1/sessions/s/runs/r/stream", {
        fetch: fetchPending,
        onError: onError1,
        onEvent: vi.fn(),
      });
      await flushMicrotasks();
      advanceAuthIdentityEpoch();
      resolvers.shift()?.(
        new Response(JSON.stringify({ detail: "not_authenticated" }), {
          headers: { "Content-Type": "application/json" },
          status: 401,
        }),
      );
      await flushMicrotasks();
      expect(unauthorizedListener).not.toHaveBeenCalled();
      expect(onError1).not.toHaveBeenCalled();

      // 场景 2：流式推送期间切换身份，后续数据帧与断流均静默丢弃且不再重连。
      const fetchLive = vi.fn(
        async (input: RequestInfo | URL, init?: RequestInit) =>
          new FakeSseStream(String(input), init).response,
      );
      const events2: SseMessage[] = [];
      const onError2 = vi.fn();
      connectEventStream("/api/v1/sessions/s/runs/r/stream", {
        fetch: fetchLive,
        onError: onError2,
        onEvent: (msg) => events2.push(msg),
      });
      await flushMicrotasks();
      const liveStream = FakeSseStream.instances.at(-1)!;
      liveStream.emit("text_delta", { payload: { text: "切号前" } });
      await flushMicrotasks();
      expect(events2).toHaveLength(1);

      advanceAuthIdentityEpoch();
      liveStream.emit("text_delta", { payload: { text: "切号后脏数据" } });
      liveStream.end();
      await flushMicrotasks();
      await vi.advanceTimersByTimeAsync(20_000);

      expect(events2).toHaveLength(1);
      expect(onError2).not.toHaveBeenCalled();
      expect(fetchLive).toHaveBeenCalledTimes(1);
    } finally {
      window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, unauthorizedListener);
    }
  });

  it("外部 AbortSignal 可立即中止在途握手与挂起退避重连", async () => {
    vi.useFakeTimers();
    const capturedSignals: AbortSignal[] = [];
    let attempt = 0;
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      attempt += 1;
      if (init?.signal) {
        capturedSignals.push(init.signal);
      }
      if (attempt === 1) {
        return new Response("err", { status: 503 });
      }
      return new Promise<Response>(() => {});
    });

    const controller = new AbortController();
    const connection = connectEventStream("/api/v1/sessions/s/runs/r/stream", {
      fetch: fetchMock,
      onEvent: vi.fn(),
      signal: controller.signal,
    });

    await flushMicrotasks();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // 在退避等待期间中止：定时器必须立刻清除，绝不发起第 2 次请求。
    controller.abort();
    expect(connection.closed).toBe(true);
    expect(capturedSignals[0]?.aborted).toBe(true);

    await vi.advanceTimersByTimeAsync(30_000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
