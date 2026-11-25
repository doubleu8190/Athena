import { createParser } from "eventsource-parser";

import {
  getAuthIdentityEpoch,
  isAuthIdentityCurrent,
  publishAdminForbidden,
  publishUnauthorized,
  readErrorDetail,
} from "./client";

export const DEFAULT_SSE_INITIAL_RETRY_MS = 1_000;
export const DEFAULT_SSE_MIN_RETRY_MS = 500;
export const DEFAULT_SSE_MAX_RETRY_MS = 10_000;

export interface SseMessage {
  data: string;
  event: string;
  id?: string;
}

export type SseErrorReason =
  | "unauthorized"
  | "forbidden"
  | "client_error"
  | "http_error"
  | "invalid_content_type"
  | "network_error"
  | "stream_closed";

export interface SseErrorInfo {
  /** 为 true 时表示不可恢复的终止错误（如 401/403/404/非瞬态 4xx），连接已永久停止且不再重连。 */
  fatal: boolean;
  reason: SseErrorReason;
  status?: number;
}

export type SseFetch = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

export interface ConnectEventStreamOptions {
  /** 自定义 fetch 实现；缺省时每次建连动态读取 globalThis.fetch。 */
  fetch?: SseFetch;
  /** 附加请求头；Accept 会固定设置为 text/event-stream。 */
  headers?: HeadersInit;
  /** 指数退避初始间隔（毫秒），默认 1000ms。 */
  initialRetryMs?: number;
  /** 指数退避上限与服务端 retry: 上界（毫秒），默认 10000ms。 */
  maxRetryMs?: number;
  /** 服务端 retry: 下界（毫秒），默认 500ms，防止非法小值引发重连风暴。 */
  minRetryMs?: number;
  onError?: (error: SseErrorInfo) => void;
  onEvent: (message: SseMessage) => void;
  onOpen?: () => void;
  /** 外部中止信号；中止后立即关闭在途请求和退避定时器。 */
  signal?: AbortSignal;
}

export interface EventStreamConnection {
  readonly closed: boolean;
  close(): void;
}

function clampRetryMs(value: number, minMs: number, maxMs: number): number {
  const safeMin = Math.max(0, minMs);
  const safeMax = Math.max(safeMin, maxMs);
  if (!Number.isFinite(value)) {
    return safeMin;
  }
  return Math.min(safeMax, Math.max(safeMin, Math.trunc(value)));
}

function isNonTransientClientStatus(status: number): boolean {
  // 408 Request Timeout 与 429 Too Many Requests 属于可退避重试的瞬态限流/超时，其余 4xx 一律停流。
  return status >= 400 && status < 500 && status !== 408 && status !== 429;
}

function cancelResponseBody(response: Response): void {
  if (!response.body || response.bodyUsed) {
    return;
  }
  void response.body.cancel().catch(() => undefined);
}

/**
 * 基于原生 fetch + eventsource-parser 的 SSE 传输客户端：
 * - 直接依据 HTTP 状态码处理 401/403/404 等错误，彻底消除原生 EventSource 无法感知 401 的缺陷；
 * - 绑定建连时的 authIdentityEpoch，在握手、错误体读取与每帧派发前持续隔离跨账号脏事件；
 * - 瞬态故障（5xx/408/429/网络中断/非预期断流）执行有界指数退避重连，支持 AbortSignal 立即中止。
 */
export function connectEventStream(
  url: string,
  options: ConnectEventStreamOptions,
): EventStreamConnection {
  const minRetryMs = options.minRetryMs ?? DEFAULT_SSE_MIN_RETRY_MS;
  const maxRetryMs = options.maxRetryMs ?? DEFAULT_SSE_MAX_RETRY_MS;
  let baseRetryMs = clampRetryMs(
    options.initialRetryMs ?? DEFAULT_SSE_INITIAL_RETRY_MS,
    minRetryMs,
    maxRetryMs,
  );
  let retryAttempt = 0;
  let lastEventId: string | null = null;

  // 锁定发起建连时的认证身份代次；后续登录/切号/登出将令旧流自动失效且不派发任何事件。
  const identityEpoch = getAuthIdentityEpoch();
  const controller = new AbortController();
  let closed = false;
  let retryTimerId: ReturnType<typeof globalThis.setTimeout> | null = null;
  let activeReader: ReadableStreamDefaultReader<Uint8Array> | null = null;

  const clearRetryTimer = () => {
    if (retryTimerId !== null) {
      globalThis.clearTimeout(retryTimerId);
      retryTimerId = null;
    }
  };

  const isActive = (): boolean =>
    !closed &&
    !controller.signal.aborted &&
    isAuthIdentityCurrent(identityEpoch);

  const close = (): void => {
    if (closed) {
      return;
    }
    closed = true;
    clearRetryTimer();
    options.signal?.removeEventListener("abort", close);
    controller.abort();
    const reader = activeReader;
    activeReader = null;
    if (reader) {
      void reader.cancel().catch(() => undefined);
    }
  };

  if (options.signal) {
    if (options.signal.aborted) {
      close();
      return {
        get closed() {
          return closed;
        },
        close,
      };
    }
    options.signal.addEventListener("abort", close, { once: true });
  }

  const scheduleReconnect = () => {
    if (!isActive()) {
      close();
      return;
    }
    clearRetryTimer();
    const delay = Math.min(
      maxRetryMs,
      Math.max(minRetryMs, baseRetryMs * Math.pow(2, retryAttempt)),
    );
    retryAttempt += 1;
    retryTimerId = globalThis.setTimeout(() => {
      retryTimerId = null;
      if (!isActive()) {
        close();
        return;
      }
      void connectOnce();
    }, delay);
  };

  const connectOnce = async (): Promise<void> => {
    if (!isActive()) {
      close();
      return;
    }

    const headers = new Headers(options.headers);
    headers.set("Accept", "text/event-stream");
    if (lastEventId !== null && lastEventId !== "") {
      headers.set("Last-Event-ID", lastEventId);
    }

    const fetchFn: SseFetch =
      options.fetch ??
      ((input: RequestInfo | URL, init?: RequestInit) =>
        globalThis.fetch(input, init));

    let response: Response;
    try {
      response = await fetchFn(url, {
        cache: "no-store",
        credentials: "same-origin",
        headers,
        method: "GET",
        signal: controller.signal,
      });
    } catch (error) {
      if (
        !isActive() ||
        (error instanceof Error && error.name === "AbortError")
      ) {
        close();
        return;
      }
      options.onError?.({ fatal: false, reason: "network_error" });
      if (isActive()) {
        scheduleReconnect();
      }
      return;
    }

    // 握手响应到达后立即复核身份代次，避免旧账号响应触发 401/403 或脏事件。
    if (!isActive()) {
      cancelResponseBody(response);
      close();
      return;
    }

    if (response.status === 401) {
      cancelResponseBody(response);
      // 直接命中 401：复用 client.ts 未认证广播并永久停流，彻底告别断流轮询 /auth/me。
      publishUnauthorized();
      options.onError?.({
        fatal: true,
        reason: "unauthorized",
        status: 401,
      });
      close();
      return;
    }

    if (response.status === 403) {
      const detail = await readErrorDetail(response);
      // 错误体读取可能跨越账号切换边界，必须再次确认仍属于当前身份。
      if (!isActive()) {
        close();
        return;
      }
      if (detail.code === "admin_required") {
        publishAdminForbidden(url);
      }
      options.onError?.({
        fatal: true,
        reason: "forbidden",
        status: 403,
      });
      close();
      return;
    }

    if (isNonTransientClientStatus(response.status)) {
      cancelResponseBody(response);
      options.onError?.({
        fatal: true,
        reason: "client_error",
        status: response.status,
      });
      close();
      return;
    }

    if (response.status !== 200) {
      cancelResponseBody(response);
      options.onError?.({
        fatal: false,
        reason: "http_error",
        status: response.status,
      });
      if (isActive()) {
        scheduleReconnect();
      }
      return;
    }

    const contentType = response.headers.get("content-type") ?? "";
    if (
      !contentType.toLowerCase().includes("text/event-stream") ||
      !response.body
    ) {
      cancelResponseBody(response);
      options.onError?.({
        fatal: false,
        reason: "invalid_content_type",
        status: response.status,
      });
      if (isActive()) {
        scheduleReconnect();
      }
      return;
    }

    // 握手成功，重置指数退避计数。
    retryAttempt = 0;
    options.onOpen?.();
    if (!isActive()) {
      cancelResponseBody(response);
      close();
      return;
    }

    const decoder = new TextDecoder("utf-8");
    const parser = createParser({
      onEvent(event) {
        // 每一帧派发前校验身份与关闭状态，防止同分片后续事件在终态关流或切号后继续派发。
        if (!isActive()) {
          return;
        }
        if (event.id !== undefined) {
          lastEventId = event.id;
        }
        options.onEvent({
          data: event.data,
          event: event.event ?? "message",
          id: event.id,
        });
      },
      onId(id) {
        if (!isActive()) {
          return;
        }
        lastEventId = id;
      },
      onRetry(retryMs) {
        if (!isActive()) {
          return;
        }
        if (Number.isFinite(retryMs) && retryMs >= 0) {
          baseRetryMs = clampRetryMs(retryMs, minRetryMs, maxRetryMs);
        }
      },
    });

    const reader = response.body.getReader();
    activeReader = reader;
    try {
      while (isActive()) {
        const { done, value } = await reader.read();
        if (!isActive()) {
          break;
        }
        if (done) {
          // 刷出 UTF-8 流式解码器尾部残留字节，确保最后一个完整事件不丢失。
          const trailing = decoder.decode();
          if (trailing) {
            parser.feed(trailing);
          }
          break;
        }
        if (value && value.byteLength > 0) {
          const chunk = decoder.decode(value, { stream: true });
          if (chunk) {
            parser.feed(chunk);
          }
        }
      }
    } catch (error) {
      if (
        !isActive() ||
        (error instanceof Error && error.name === "AbortError")
      ) {
        close();
        return;
      }
      options.onError?.({ fatal: false, reason: "network_error" });
      if (isActive()) {
        scheduleReconnect();
      }
      return;
    } finally {
      if (activeReader === reader) {
        activeReader = null;
      }
      try {
        reader.releaseLock();
      } catch {
        // 主动 cancel 期间释放锁可能受流状态影响，安全忽略。
      }
    }

    // 业务层主动关闭（例如终态/等待态快照）或身份已切换时，直接安静结束，不报断线也不重连。
    if (!isActive()) {
      close();
      return;
    }

    // 服务端在活跃阶段非预期 EOF 断流，通知上层保留当前文本并进入有界指数退避重连。
    options.onError?.({ fatal: false, reason: "stream_closed" });
    if (isActive()) {
      scheduleReconnect();
    }
  };

  void connectOnce();

  return {
    get closed() {
      return closed;
    },
    close,
  };
}
