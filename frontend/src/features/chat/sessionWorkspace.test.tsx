import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as formatLib from "../../shared/lib/format";

import {
  getRequestUrl,
  listResponse,
  makeArtifact,
  makeMessage,
  makeRun,
  makeSession,
  makeSessionFile,
  makeToolCall,
  makeToolCallDetail,
  PROJECT_A,
  TEST_USER,
} from "../../test/fixtures";
import {
  FakeEventSource,
} from "../../test/fakeEventSource";
import { jsonResponse, renderApp } from "../../test/testUtils";
import type { SessionFile } from "../files/model";
import type {
  ConversationMessagePage,
  Run,
  RunEventListResponse,
  RunPendingApproval,
  RunToolCallDetail,
  RunToolCallSummary,
} from "./model";
import { chatKeys } from "./queryKeys";

const INITIAL_WINDOW_WIDTH = window.innerWidth;
const SESSION = makeSession({
  id: "session-1",
  title: "真实前端会话",
});

interface WorkspaceFetchOptions {
  fileContent?: (
    fileId: string,
    signal?: AbortSignal | null,
  ) => Response | Promise<Response>;
  files?: SessionFile[];
  getFiles?: () => SessionFile[];
  getRuns: () => Run[];
  handleWrite: (
    pathname: string,
    method: string,
    init: RequestInit | undefined,
  ) => Promise<Response>;
  messages: (beforeSequence?: number) => ConversationMessagePage;
  messagesResponse?: (beforeSequence?: number) => Promise<Response>;
  runDetails?: (runId: string) => Run | undefined;
  runEvents?: (afterSequence?: number) => RunEventListResponse;
  runEventsResponse?: () => Promise<Response>;
  sessionResponse?: () => Promise<Response>;
  toolCallDetail?: (
    runId: string,
    callId: string,
  ) => RunToolCallDetail | undefined;
  toolCalls?: (runId: string) => RunToolCallSummary[];
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => {
    resolve = next;
  });
  return { promise, resolve };
}

function installWorkspaceFetch(options: WorkspaceFetchOptions) {
  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(getRequestUrl(input), "http://localhost");
      const method = init?.method ?? "GET";
      if (url.pathname.endsWith("/auth/me")) {
        return jsonResponse(TEST_USER);
      }
      if (
        url.pathname === "/api/v1/skills" &&
        method === "GET"
      ) {
        return jsonResponse({ items: [] });
      }
      if (url.pathname === "/api/v1/projects") {
        return jsonResponse(listResponse([PROJECT_A], 1, 100, 1));
      }
      if (url.pathname === `/api/v1/projects/${PROJECT_A.id}`) {
        return jsonResponse(PROJECT_A);
      }
      if (url.pathname === "/api/v1/sessions" && method === "GET") {
        const pageSize = Number(url.searchParams.get("page_size"));
        if (pageSize === 8) {
          return jsonResponse(listResponse([SESSION], 1, 8, 1));
        }
        return jsonResponse(listResponse([]));
      }
      if (
        url.pathname === `/api/v1/sessions/${SESSION.id}` &&
        method === "GET"
      ) {
        return options.sessionResponse
          ? await options.sessionResponse()
          : jsonResponse(SESSION);
      }
      if (
        url.pathname === `/api/v1/sessions/${SESSION.id}/messages` &&
        method === "GET"
      ) {
        const raw = url.searchParams.get("before_sequence");
        if (options.messagesResponse) {
          return await options.messagesResponse(
            raw === null ? undefined : Number(raw),
          );
        }
        return jsonResponse(
          options.messages(raw === null ? undefined : Number(raw)),
        );
      }
      if (
        url.pathname === `/api/v1/sessions/${SESSION.id}/files` &&
        method === "GET"
      ) {
        return jsonResponse({
          items: options.getFiles ? options.getFiles() : (options.files ?? []),
          next_after_file_id: null,
        });
      }
      const fileContentMatch = new RegExp(
        `^/api/v1/sessions/${SESSION.id}/files/([^/]+)/content$`,
      ).exec(url.pathname);
      if (fileContentMatch && method === "GET" && options.fileContent) {
        const fileId = decodeURIComponent(fileContentMatch[1]);
        return await options.fileContent(fileId, init?.signal);
      }
      if (method !== "GET") {
        return options.handleWrite(url.pathname, method, init);
      }

      const listMatch = new RegExp(
        `^/api/v1/sessions/${SESSION.id}/runs$`,
      );
      if (listMatch.test(url.pathname)) {
        const limit = Number(url.searchParams.get("limit") ?? 1);
        const items = options.getRuns().slice(0, limit);
        return jsonResponse({
          items,
          next_cursor: items.length < options.getRuns().length ? "next" : null,
        });
      }
      const runEventsMatch = new RegExp(
        `^/api/v1/sessions/${SESSION.id}/runs/([^/]+)/events$`,
      ).exec(url.pathname);
      if (runEventsMatch) {
        if (options.runEventsResponse) {
          return await options.runEventsResponse();
        }
        const raw = url.searchParams.get("after_sequence");
        return jsonResponse(
          options.runEvents?.(
            raw === null ? undefined : Number(raw),
          ) ?? { items: [], next_after_sequence: null },
        );
      }
      const runMatch = new RegExp(
        `^/api/v1/sessions/${SESSION.id}/runs/([^/]+)$`,
      ).exec(url.pathname);
      if (runMatch) {
        const runId = decodeURIComponent(runMatch[1]);
        const run =
          options.runDetails?.(runId) ??
          options.getRuns().find((item) => item.id === runId);
        return run
          ? jsonResponse(run)
          : jsonResponse({ detail: "run_not_found" }, 404);
      }
      const toolCallsMatch = new RegExp(
        `^/api/v1/sessions/${SESSION.id}/runs/([^/]+)/tool-calls$`,
      ).exec(url.pathname);
      if (toolCallsMatch) {
        const runId = decodeURIComponent(toolCallsMatch[1]);
        return jsonResponse({
          items: options.toolCalls?.(runId) ?? [],
          next_after_ordinal: null,
        });
      }
      const toolDetailMatch = new RegExp(
        `^/api/v1/sessions/${SESSION.id}/runs/([^/]+)/tool-calls/([^/]+)$`,
      ).exec(url.pathname);
      if (toolDetailMatch) {
        const runId = decodeURIComponent(toolDetailMatch[1]);
        const callId = decodeURIComponent(toolDetailMatch[2]);
        const detail = options.toolCallDetail?.(runId, callId);
        return detail
          ? jsonResponse(detail)
          : jsonResponse({ detail: "run_not_found" }, 404);
      }
      throw new Error(`Unexpected request: ${method} ${url}`);
    },
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  window.innerWidth = INITIAL_WINDOW_WIDTH;
  FakeEventSource.instances = [];
  vi.unstubAllGlobals();
});

describe("会话工作台初始加载与历史", () => {
  it("Session 尚未返回时已并行读取消息和最新 Run", async () => {
    let resolveSession: ((response: Response) => void) | null = null;
    const sessionGate = new Promise<Response>((resolve) => {
      resolveSession = resolve;
    });
    const completedRun = makeRun({
      id: "run-parallel",
      session_id: SESSION.id,
      status: "completed",
    });
    const fetchMock = installWorkspaceFetch({
      getRuns: () => [completedRun],
      handleWrite: async () => {
        throw new Error("unexpected write");
      },
      messages: () => ({
        items: [],
        next_before_sequence: null,
      }),
      sessionResponse: () => sessionGate,
    });

    renderApp(`/chat/${SESSION.id}`);

    await waitFor(() => {
      const requestPaths = fetchMock.mock.calls.map(([input]) =>
        new URL(getRequestUrl(input), "http://localhost").pathname,
      );
      expect(requestPaths).toContain(
        `/api/v1/sessions/${SESSION.id}/messages`,
      );
      expect(requestPaths).toContain(
        `/api/v1/sessions/${SESSION.id}/runs`,
      );
    });
    expect(
      screen.queryByRole("heading", { name: "真实前端会话" }),
    ).not.toBeInTheDocument();

    await act(async () => {
      resolveSession?.(jsonResponse(SESSION));
      await sessionGate;
    });
    expect(
      await screen.findByRole("heading", { name: "真实前端会话" }),
    ).toBeInTheDocument();
  });

  it("加载 Session、消息、最新 Run、详情和工具调用，并连接真实 SSE 地址", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const runningRun = makeRun({
      id: "run-1",
      session_id: SESSION.id,
      status: "running",
    });
    const toolCall = makeToolCall({
      id: "call-record-1",
      ordinal: 1,
      tool_call_id: "model-call-1",
    });
    const fetchMock = installWorkspaceFetch({
      getRuns: () => [runningRun],
      handleWrite: async () => {
        throw new Error("unexpected write");
      },
      messages: () => ({
        items: [
          makeMessage({
            id: "msg-1",
            parts: [{ text: "请分析真实会话", type: "text" }],
            role: "user",
            sequence: 1,
            session_id: SESSION.id,
          }),
        ],
        next_before_sequence: null,
      }),
      toolCalls: () => [toolCall],
    });

    renderApp(`/chat/${SESSION.id}`);

    expect(
      await screen.findByRole("heading", { name: "真实前端会话" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("请分析真实会话")).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: "运行中，暂不可发送" }),
    ).toBeDisabled();
    const progress = await screen.findByRole("region", { name: "执行进度" });
    const user = userEvent.setup();
    await user.click(
      await within(progress).findByRole("button", { name: /展开/ }),
    );
    expect(await within(progress).findByText("web_search")).toBeInTheDocument();
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.instances[0].url).toBe(
      `/api/v1/sessions/${SESSION.id}/runs/run-1/stream`,
    );

    const requestPaths = fetchMock.mock.calls.map(([input]) =>
      new URL(getRequestUrl(input), "http://localhost").pathname,
    );
    expect(requestPaths).toContain(
      `/api/v1/sessions/${SESSION.id}/messages`,
    );
    expect(requestPaths).toContain(
      `/api/v1/sessions/${SESSION.id}/runs`,
    );
    expect(requestPaths).toContain(
      `/api/v1/sessions/${SESSION.id}/runs/run-1`,
    );
    expect(requestPaths).toContain(
      `/api/v1/sessions/${SESSION.id}/runs/run-1/tool-calls`,
    );
  });

  it("分页加载更早消息并按 sequence 合并去重", async () => {
    const completedRun = makeRun({
      id: "run-completed",
      session_id: SESSION.id,
      status: "completed",
    });
    const fetchMock = installWorkspaceFetch({
      getRuns: () => [completedRun],
      handleWrite: async () => {
        throw new Error("unexpected write");
      },
      messages: (beforeSequence) => {
        if (beforeSequence === 3) {
          return {
            items: [
              makeMessage({
                id: "msg-1",
                parts: [{ text: "最早第一条", type: "text" }],
                sequence: 1,
                session_id: SESSION.id,
              }),
              makeMessage({
                id: "msg-2",
                parts: [{ text: "较早第二条", type: "text" }],
                sequence: 2,
                session_id: SESSION.id,
              }),
              makeMessage({
                id: "msg-3-duplicate",
                parts: [{ text: "重复旧第三", type: "text" }],
                sequence: 3,
                session_id: SESSION.id,
              }),
            ],
            next_before_sequence: null,
          };
        }
        return {
          items: [
            makeMessage({
              id: "msg-3",
              parts: [{ text: "最新第三", type: "text" }],
              sequence: 3,
              session_id: SESSION.id,
            }),
            makeMessage({
              id: "msg-4",
              parts: [{ text: "最新第四", type: "text" }],
              sequence: 4,
              session_id: SESSION.id,
            }),
          ],
          next_before_sequence: 3,
        };
      },
    });
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);

    expect(await screen.findByText("最新第三")).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "加载更早消息" }),
    );
    expect(await screen.findByText("最早第一条")).toBeInTheDocument();
    expect(screen.getByText("较早第二条")).toBeInTheDocument();
    expect(screen.getByText("最新第四")).toBeInTheDocument();
    expect(screen.queryByText("重复旧第三")).not.toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([input]) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        return (
          url.pathname === `/api/v1/sessions/${SESSION.id}/messages` &&
          url.searchParams.get("before_sequence") === "3"
        );
      }),
    ).toBe(true);
  });
});

describe("Run 命令与真实状态", () => {
  it("普通发送成功后等待目标用户消息进入列表再定位最新", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const triggerMessage = makeMessage({
      id: "message-sent-after-http",
      parts: [{ text: "等待真实消息进入列表再滚动", type: "text" }],
      role: "user",
      sequence: 2,
      session_id: SESSION.id,
    });
    const queuedRun = makeRun({
      id: "run-after-send-scroll",
      session_id: SESSION.id,
      status: "queued",
      trigger_message_id: triggerMessage.id,
    });
    let runs: Run[] = [];
    let serveDelayedPage = false;
    const delayedPageRequested = deferred<void>();
    const delayedPage = deferred<Response>();
    installWorkspaceFetch({
      getRuns: () => runs,
      handleWrite: async (pathname, method) => {
        if (pathname.endsWith("/runs") && method === "POST") {
          runs = [queuedRun];
          serveDelayedPage = true;
          return jsonResponse(queuedRun, 201);
        }
        throw new Error(`Unexpected write: ${pathname}`);
      },
      messages: () => ({
        items: [
          makeMessage({
            id: "message-before-send",
            parts: [{ text: "发送前历史", type: "text" }],
            role: "user",
            sequence: 1,
            session_id: SESSION.id,
          }),
        ],
        next_before_sequence: null,
      }),
      messagesResponse: async () => {
        if (serveDelayedPage) {
          serveDelayedPage = false;
          delayedPageRequested.resolve();
          return delayedPage.promise;
        }
        return jsonResponse({
          items: [
            makeMessage({
              id: "message-before-send",
              parts: [{ text: "发送前历史", type: "text" }],
              role: "user",
              sequence: 1,
              session_id: SESSION.id,
            }),
          ],
          next_before_sequence: null,
        });
      },
    });
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);

    expect(await screen.findByText("发送前历史")).toBeInTheDocument();
    const list = screen.getByRole("region", { name: "消息历史" });
    let scrollHeight = 900;
    Object.defineProperties(list, {
      clientHeight: {
        configurable: true,
        value: 300,
      },
      scrollHeight: {
        configurable: true,
        get: () => scrollHeight,
      },
      scrollTop: {
        configurable: true,
        value: 0,
        writable: true,
      },
    });
    fireEvent.scroll(list);

    const composer = screen.getByLabelText("对话草稿");
    await user.type(composer, "等待真实消息进入列表再滚动");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(
      await screen.findByText("消息已提交，正在按服务端最新运行状态同步。"),
    ).toBeInTheDocument();
    await delayedPageRequested.promise;
    expect(list.scrollTop).toBe(0);

    scrollHeight = 1200;
    await act(async () => {
      delayedPage.resolve(
        jsonResponse({
          items: [
            makeMessage({
              id: "message-before-send",
              parts: [{ text: "发送前历史", type: "text" }],
              role: "user",
              sequence: 1,
              session_id: SESSION.id,
            }),
            triggerMessage,
          ],
          next_before_sequence: null,
        }),
      );
      await delayedPage.promise;
    });

    expect(
      await screen.findByText("等待真实消息进入列表再滚动"),
    ).toBeInTheDocument();
    expect(list.scrollTop).toBe(1200);
  });

  it("普通发送失败时不会把旧消息末尾误当成发送成功", async () => {
    const failedMessage = "失败后保持当前阅读位置";
    installWorkspaceFetch({
      getRuns: () => [],
      handleWrite: async (pathname, method) => {
        if (pathname.endsWith("/runs") && method === "POST") {
          throw new TypeError("Failed to fetch");
        }
        throw new Error(`Unexpected write: ${pathname}`);
      },
      messages: () => ({
        items: [
          makeMessage({
            id: "message-failure-history",
            parts: [{ text: "仍在上方阅读的历史", type: "text" }],
            role: "user",
            sequence: 1,
            session_id: SESSION.id,
          }),
        ],
        next_before_sequence: null,
      }),
    });
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);

    expect(await screen.findByText("仍在上方阅读的历史")).toBeInTheDocument();
    const list = screen.getByRole("region", { name: "消息历史" });
    Object.defineProperties(list, {
      clientHeight: {
        configurable: true,
        value: 300,
      },
      scrollHeight: {
        configurable: true,
        value: 1000,
      },
      scrollTop: {
        configurable: true,
        value: 100,
        writable: true,
      },
    });
    fireEvent.scroll(list);

    const composer = screen.getByLabelText("对话草稿");
    await user.type(composer, failedMessage);
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(
      await screen.findByText("无法连接服务器，请检查网络后重试。"),
    ).toBeInTheDocument();
    expect(composer).toHaveValue(failedMessage);
    expect(list.scrollTop).toBe(100);
    expect(
      screen.getByRole("button", { name: "回到底部" }),
    ).toBeInTheDocument();
  });

  it("latest list 与 detail 同版本时保留 detail 的待审批投影", async () => {
    const pending: RunPendingApproval = {
      batch_message_id: "batch-1",
      call_id: "call-1",
      model_name: "approval_tool",
      ordinal: 2,
      requested_at: "2026-11-10T03:00:02Z",
      safe_skill_projection: null,
      tool_kind: "builtin",
    };
    const latest = makeRun({
      event_sequence: 3,
      id: "run-pending-detail",
      pending_approvals: [],
      session_id: SESSION.id,
      status: "waiting_for_approval",
    });
    const detail = { ...latest, pending_approvals: [pending] };
    installWorkspaceFetch({
      getRuns: () => [latest],
      handleWrite: async () => {
        throw new Error("unexpected write");
      },
      messages: () => ({ items: [], next_before_sequence: null }),
      runDetails: () => detail,
    });

    renderApp(`/chat/${SESSION.id}`);

    const approval = await screen.findByRole("region", {
      name: "等待工具审批",
    });
    expect(
      within(approval).getByText(/approval_tool/),
    ).toBeInTheDocument();
    expect(
      within(approval).queryByText("当前暂无待确认的操作，正在同步最新进展。"),
    ).not.toBeInTheDocument();
  });

  it("查询终态关闭流并在持久消息到达后收敛同一 Run 的临时输出", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    let latestMessagesPage: ConversationMessagePage = {
      items: [],
      next_before_sequence: null,
    };
    let olderMessagesPage: ConversationMessagePage = {
      items: [],
      next_before_sequence: null,
    };
    let runs = [
      makeRun({
        id: "run-terminal-query",
        session_id: SESSION.id,
        status: "running",
      }),
    ];
    installWorkspaceFetch({
      getRuns: () => runs,
      handleWrite: async () => {
        throw new Error("unexpected write");
      },
      messages: (beforeSequence) =>
        beforeSequence === undefined
          ? latestMessagesPage
          : olderMessagesPage,
      runEvents: (afterSequence) => {
        if (afterSequence === 0) {
          return {
            items: [
              {
                created_at: "2026-11-10T03:00:01Z",
                id: "event-started",
                payload: {},
                run_id: runs[0].id,
                sequence: 1,
                type: "started",
              },
            ],
            next_after_sequence: 1,
          };
        }
        return {
          items: [
            {
              created_at: "2026-11-10T03:00:03Z",
              id: "event-completed",
              payload: {
                assistant_message_id: "msg-terminal-assistant",
              },
              run_id: runs[0].id,
              sequence: 2,
              type: "completed",
            },
          ],
          next_after_sequence: null,
        };
      },
    });
    renderApp(`/chat/${SESSION.id}`);

    expect(
      await screen.findByRole("button", { name: "运行中，暂不可发送" }),
    ).toBeDisabled();
    const source = FakeEventSource.instances[0];
    act(() => {
      source.emit("snapshot", {
        payload: {
          event_sequence: 2,
          events: [],
          events_after_sequence: null,
          events_complete: true,
          messages: [],
          messages_before_sequence: null,
          messages_complete: true,
          status: "running",
        },
      });
      source.emit("text_delta", {
        payload: { text: "未经快照确认的临时输出" },
      });
    });
    expect(
      await screen.findByLabelText("Athena 的临时流式输出"),
    ).toBeInTheDocument();

    const completed = {
      ...runs[0],
      event_sequence: 3,
      finished_at: "2026-11-10T03:00:03Z",
      status: "completed" as const,
      updated_at: "2026-11-10T03:00:03Z",
    };
    latestMessagesPage = {
      items: [
        makeMessage({
          id: "msg-terminal-delivery",
          parts: [{ text: "已交付文件", type: "text" }],
          role: "assistant",
          run_id: completed.id,
          sequence: 3,
          session_id: SESSION.id,
          status: "completed",
        }),
      ],
      next_before_sequence: 2,
    };
    olderMessagesPage = {
      items: [
        makeMessage({
          id: "msg-terminal-assistant",
          parts: [{ text: "持久最终回答 42", type: "text" }],
          role: "assistant",
          run_id: completed.id,
          sequence: 2,
          session_id: SESSION.id,
          status: "completed",
        }),
      ],
      next_before_sequence: null,
    };
    runs = [completed];
    act(() => {
      // 终态 snapshot 只带旧交付消息；清理 transient 必须等 events 中的精确 ID 对账。
      source.emit("snapshot", {
        payload: {
          event_sequence: completed.event_sequence,
          events: [],
          events_after_sequence: null,
          events_complete: true,
          messages: [
            {
              created_at: "2026-11-10T03:00:02Z",
              id: "msg-terminal-delivery-snapshot",
              role: "assistant",
              sequence: 3,
              status: "completed",
              text: "已交付文件",
            },
          ],
          messages_before_sequence: null,
          messages_complete: true,
          status: "completed",
        },
      });
    });
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "发送消息" }),
      ).toBeInTheDocument();
    });

    expect(
      screen.queryByRole("button", { name: "运行中，暂不可发送" }),
    ).not.toBeInTheDocument();
    expect(source.closed).toBe(true);
    expect(
      await screen.findByRole("article", {
        name: "Athena的消息，序号 2",
      }),
    ).toHaveTextContent("持久最终回答 42");
    expect(
      screen.queryByLabelText("Athena 的临时流式输出"),
    ).not.toBeInTheDocument();
  });

  it("同 Run 缓存交付消息时，终态 refetch 503 仍保留草稿并等待最终回答", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    let completed = false;
    let failTerminalReconcile = true;
    const runs = [
      makeRun({
        id: "run-terminal-retry",
        session_id: SESSION.id,
        status: "running",
      }),
    ];
    const delivery = makeMessage({
      id: "msg-terminal-delivery",
      parts: [{ text: "较早的交付消息", type: "text" }],
      role: "assistant",
      run_id: runs[0].id,
      sequence: 1,
      session_id: SESSION.id,
      status: "completed",
    });
    const finalAnswer = makeMessage({
      id: "msg-terminal-retry",
      parts: [{ text: "重试后的持久回答", type: "text" }],
      role: "assistant",
      run_id: runs[0].id,
      sequence: 2,
      session_id: SESSION.id,
      status: "completed",
    });
    installWorkspaceFetch({
      getRuns: () =>
        completed
          ? [
              {
                ...runs[0],
                event_sequence: 3,
                status: "completed",
                updated_at: "2026-11-10T03:00:03Z",
              },
            ]
          : runs,
      handleWrite: async () => {
        throw new Error("unexpected write");
      },
      messages: () => ({
        items: [delivery],
        next_before_sequence: null,
      }),
      messagesResponse: async () => {
        if (!completed) {
          return jsonResponse({
            items: [delivery],
            next_before_sequence: null,
          });
        }
        if (failTerminalReconcile) {
          return jsonResponse({ detail: "temporary_failure" }, 503);
        }
        return jsonResponse({
          items: [delivery, finalAnswer],
          next_before_sequence: null,
        });
      },
      runEvents: () => ({
        items: [
          {
            created_at: "2026-11-10T03:00:03Z",
            id: "event-terminal-retry",
            payload: { assistant_message_id: finalAnswer.id },
            run_id: runs[0].id,
            sequence: 3,
            type: "completed",
          },
        ],
        next_after_sequence: null,
      }),
    });
    const { client } = renderApp(`/chat/${SESSION.id}`);

    expect(
      await screen.findByRole("button", { name: "运行中，暂不可发送" }),
    ).toBeDisabled();
    const source = FakeEventSource.instances[0];
    act(() => {
      source.emit("text_delta", {
        payload: { text: "失败时保留的草稿" },
      });
    });
    const completedRun = {
      ...runs[0],
      event_sequence: 3,
      status: "completed" as const,
      updated_at: "2026-11-10T03:00:03Z",
    };
    completed = true;
    act(() => {
      client.setQueryData(chatKeys.latestRun(SESSION.id), {
        items: [completedRun],
        next_cursor: null,
      });
      client.setQueryData(
        chatKeys.run(SESSION.id, completedRun.id),
        completedRun,
      );
    });

    expect(
      await screen.findByText("任务已完成，但最终回复同步失败，请重试加载。"),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText("Athena 的临时流式输出"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("article", { name: "Athena的消息，序号 1" }),
    ).toHaveTextContent("较早的交付消息");

    failTerminalReconcile = false;
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "重试加载最终消息" }));
    expect(
      await screen.findByRole("article", {
        name: "Athena的消息，序号 2",
      }),
    ).toHaveTextContent("重试后的持久回答");
    expect(
      screen.queryByLabelText("Athena 的临时流式输出"),
    ).not.toBeInTheDocument();
  });

  it("completed 的消息页耗尽时保留草稿且不回退旧交付消息", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const running = makeRun({
      id: "run-message-exhausted",
      session_id: SESSION.id,
      status: "running",
    });
    const delivery = makeMessage({
      id: "msg-old-delivery",
      parts: [{ text: "旧文件交付", type: "text" }],
      role: "assistant",
      run_id: running.id,
      sequence: 3,
      session_id: SESSION.id,
    });
    const fetchMock = installWorkspaceFetch({
      getRuns: () => [running],
      handleWrite: async () => {
        throw new Error("unexpected write");
      },
      messages: (beforeSequence) => {
        if (beforeSequence === undefined) {
          return {
            items: [delivery],
            next_before_sequence: 2,
          };
        }
        if (beforeSequence === 2) {
          return {
            items: [
              makeMessage({
                id: running.trigger_message_id,
                parts: [{ text: "当前 Run 的触发消息", type: "text" }],
                role: "user",
                run_id: null,
                sequence: 1,
                session_id: SESSION.id,
              }),
            ],
            next_before_sequence: 1,
          };
        }
        throw new Error("不应扫描当前 Run 触发消息之前的会话历史");
      },
      runEvents: () => ({
        items: [
          {
            created_at: "2026-11-10T03:00:03Z",
            id: "event-message-exhausted",
            payload: { assistant_message_id: "msg-authoritative-missing" },
            run_id: running.id,
            sequence: 4,
            type: "completed",
          },
        ],
        next_after_sequence: null,
      }),
    });
    const { client } = renderApp(`/chat/${SESSION.id}`);

    expect(
      await screen.findByRole("button", { name: "运行中，暂不可发送" }),
    ).toBeDisabled();
    const source = FakeEventSource.instances[0];
    act(() => {
      source.emit("text_delta", {
        payload: { text: "消息耗尽时仍需保留" },
      });
    });
    const completed = {
      ...running,
      event_sequence: 4,
      status: "completed" as const,
      updated_at: "2026-11-10T03:00:03Z",
    };
    act(() => {
      client.setQueryData(chatKeys.latestRun(SESSION.id), {
        items: [completed],
        next_cursor: null,
      });
      client.setQueryData(chatKeys.run(SESSION.id, running.id), completed);
    });

    expect(
      await screen.findByText(/未找到对应的最终回复/),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText("Athena 的临时流式输出"),
    ).toHaveTextContent("消息耗尽时仍需保留");
    expect(screen.getByText("旧文件交付")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([input]) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        return (
          url.pathname === `/api/v1/sessions/${SESSION.id}/messages` &&
          url.searchParams.get("before_sequence") === "2"
        );
      }),
    ).toBe(true);
  });

  it("completed 的事件页耗尽时保留草稿且不读取 latest assistant", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const running = makeRun({
      id: "run-event-exhausted",
      session_id: SESSION.id,
      status: "running",
    });
    const delivery = makeMessage({
      id: "msg-event-old-delivery",
      parts: [{ text: "旧交付不能被当成最终回答", type: "text" }],
      role: "assistant",
      run_id: running.id,
      sequence: 2,
      session_id: SESSION.id,
    });
    const fetchMock = installWorkspaceFetch({
      getRuns: () => [running],
      handleWrite: async () => {
        throw new Error("unexpected write");
      },
      messages: () => ({
        items: [delivery],
        next_before_sequence: null,
      }),
      runEvents: (afterSequence) =>
        afterSequence === 0
          ? {
              items: [
                {
                  created_at: "2026-11-10T03:00:02Z",
                  id: "event-started-exhausted",
                  payload: {},
                  run_id: running.id,
                  sequence: 2,
                  type: "started",
                },
              ],
              next_after_sequence: 2,
            }
          : {
              items: [],
              next_after_sequence: null,
            },
    });
    const { client } = renderApp(`/chat/${SESSION.id}`);

    expect(
      await screen.findByRole("button", { name: "运行中，暂不可发送" }),
    ).toBeDisabled();
    const source = FakeEventSource.instances[0];
    act(() => {
      source.emit("text_delta", {
        payload: { text: "事件耗尽时仍需保留" },
      });
    });
    const completed = {
      ...running,
      event_sequence: 4,
      status: "completed" as const,
      updated_at: "2026-11-10T03:00:03Z",
    };
    act(() => {
      client.setQueryData(chatKeys.latestRun(SESSION.id), {
        items: [completed],
        next_cursor: null,
      });
      client.setQueryData(chatKeys.run(SESSION.id, running.id), completed);
    });

    expect(
      await screen.findByText(/未找到对应的最终回复/),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText("Athena 的临时流式输出"),
    ).toHaveTextContent("事件耗尽时仍需保留");
    expect(screen.getByText("旧交付不能被当成最终回答")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.filter(([input]) =>
        new URL(getRequestUrl(input), "http://localhost").pathname.endsWith(
          "/events",
        ),
      ),
    ).toHaveLength(2);
  });

  it.each([
    ["failed", "任务执行未完成", "任务执行未完成，已生成的临时内容"],
    ["cancelled", "本次任务已取消", "任务已取消，已生成的临时内容"],
  ] as const)(
    "%s 终态保留旧交付旁的未完成回答且不猜测 settle",
    async (terminalStatus, statusLabel, retentionLabel) => {
      vi.stubGlobal("EventSource", FakeEventSource);
      const running = makeRun({
        id: `run-${terminalStatus}-delivery`,
        session_id: SESSION.id,
        status: "running",
      });
      const delivery = makeMessage({
        id: `msg-${terminalStatus}-delivery`,
        parts: [
          {
            artifact_id: "artifact-1",
            artifacts: [],
            output: null,
            status: "success",
            tool_call_id: "call-delivery",
            type: "tool_result",
          },
        ],
        role: "assistant",
        run_id: running.id,
        sequence: 1,
        session_id: SESSION.id,
        status: "completed",
      });
      const fetchMock = installWorkspaceFetch({
        getRuns: () => [running],
        handleWrite: async () => {
          throw new Error("unexpected write");
        },
        messages: () => ({
          items: [delivery],
          next_before_sequence: null,
        }),
      });
      renderApp(`/chat/${SESSION.id}`);

      expect(
        await screen.findByRole("button", { name: "运行中，暂不可发送" }),
      ).toBeDisabled();
      const source = FakeEventSource.instances[0];
      act(() => {
        source.emit("text_delta", {
          payload: { text: `${terminalStatus} 时仍需保留的回答` },
        });
        source.emit("run_event", {
          payload: {
            event: {
              created_at: "2026-11-10T03:00:03Z",
              id: `event-${terminalStatus}`,
              payload:
                terminalStatus === "failed"
                  ? { safe_error_code: "e2e_failed" }
                  : {},
              run_id: running.id,
              sequence: 3,
              type: terminalStatus,
            },
          },
        });
      });

      expect(await screen.findByText(statusLabel)).toBeInTheDocument();
      expect(
        screen.getByLabelText("Athena 的临时流式输出"),
      ).toHaveTextContent(`${terminalStatus} 时仍需保留的回答`);
      expect(
        screen.getByText(retentionLabel, { exact: false }),
      ).toHaveTextContent("刷新后不承诺恢复");
      expect(
        screen.getByRole("article", {
          name: "Athena的消息，序号 1",
        }),
      ).toBeInTheDocument();
      await waitFor(() => {
        expect(source.closed).toBe(true);
      });
      const eventLookups = fetchMock.mock.calls.filter(([input]) =>
        new URL(getRequestUrl(input), "http://localhost").pathname.endsWith(
          "/events",
        ),
      );
      expect(eventLookups).toHaveLength(terminalStatus === "failed" ? 1 : 0);
    },
  );

  it("failed 事件有报告 ID 时跨消息页恢复权威报告并清除瞬时文本", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const running = makeRun({
      id: "run-failed-report",
      session_id: SESSION.id,
      status: "running",
    });
    const trigger = makeMessage({
      id: running.trigger_message_id,
      parts: [{ text: "触发安全收尾", type: "text" }],
      reply_to_message_id: null,
      role: "user",
      run_id: null,
      sequence: 3,
      session_id: SESSION.id,
    });
    const report = makeMessage({
      id: "msg-failed-report",
      parts: [{ text: "权威失败报告：未完成原任务", type: "text" }],
      reply_to_message_id: trigger.id,
      role: "assistant",
      run_id: running.id,
      sequence: 5,
      session_id: SESSION.id,
      status: "completed",
    });
    const delivery = makeMessage({
      id: "msg-failed-delivery",
      parts: [{ text: "普通工具交付消息", type: "text" }],
      reply_to_message_id: null,
      role: "assistant",
      run_id: running.id,
      sequence: 9,
      session_id: SESSION.id,
      status: "completed",
    });
    const otherRunReport = makeMessage({
      id: "msg-other-run-report",
      parts: [{ text: "其他 Run 的报告", type: "text" }],
      reply_to_message_id: trigger.id,
      role: "assistant",
      run_id: "run-other",
      sequence: 7,
      session_id: SESSION.id,
      status: "completed",
    });
    installWorkspaceFetch({
      getRuns: () => [running],
      handleWrite: async () => {
        throw new Error("unexpected write");
      },
      messages: (beforeSequence) => {
        if (beforeSequence === undefined) {
          return {
            items: [delivery],
            next_before_sequence: 9,
          };
        }
        if (beforeSequence === 9) {
          return {
            items: [otherRunReport],
            next_before_sequence: 7,
          };
        }
        if (beforeSequence === 7) {
          return {
            items: [report],
            next_before_sequence: 5,
          };
        }
        return {
          items: [trigger],
          next_before_sequence: null,
        };
      },
    });
    renderApp(`/chat/${SESSION.id}`);

    expect(
      await screen.findByRole("button", { name: "运行中，暂不可发送" }),
    ).toBeDisabled();
    const source = FakeEventSource.instances[0];
    act(() => {
      source.emit("text_delta", {
        payload: { text: "不能当作报告的瞬时回答" },
      });
      source.emit("run_event", {
        payload: {
          event: {
            created_at: "2026-11-10T03:00:03Z",
            id: "event-failed-report",
            payload: {
              context_stop_reason: "compression_failed",
              report_message_id: report.id,
              safe_error_code: "run_context_safety_stop",
            },
            run_id: running.id,
            sequence: 4,
            type: "failed",
          },
        },
      });
    });

    expect(
      await screen.findByRole("article", {
        name: "Athena的消息，序号 5",
      }),
    ).toHaveTextContent("权威失败报告：未完成原任务");
    await waitFor(() => {
      expect(
        screen.queryByLabelText("Athena 的临时流式输出"),
      ).not.toBeInTheDocument();
    });
    expect(screen.getByText("普通工具交付消息")).toBeInTheDocument();
    expect(screen.getByText("其他 Run 的报告")).toBeInTheDocument();
  });

  it("刷新后的 failed Run 从投影报告 ID 精确恢复，不读取 latest assistant", async () => {
    const trigger = makeMessage({
      id: "msg-refresh-trigger",
      parts: [{ text: "刷新前的触发消息", type: "text" }],
      reply_to_message_id: null,
      role: "user",
      run_id: null,
      sequence: 3,
      session_id: SESSION.id,
    });
    const report = makeMessage({
      id: "msg-refresh-report",
      parts: [{ text: "刷新后仍可见的失败报告", type: "text" }],
      reply_to_message_id: trigger.id,
      role: "assistant",
      run_id: "run-refresh-report",
      sequence: 5,
      session_id: SESSION.id,
      status: "completed",
    });
    const latestAssistant = makeMessage({
      id: "msg-latest-assistant",
      parts: [{ text: "另一个 Run 的 latest assistant", type: "text" }],
      reply_to_message_id: null,
      role: "assistant",
      run_id: "run-other",
      sequence: 9,
      session_id: SESSION.id,
      status: "completed",
    });
    const failed = makeRun({
      id: "run-refresh-report",
      report_message_id: report.id,
      safe_error_code: "run_context_safety_stop",
      session_id: SESSION.id,
      status: "failed",
      trigger_message_id: trigger.id,
    });
    const fetchMock = installWorkspaceFetch({
      getRuns: () => [failed],
      handleWrite: async () => {
        throw new Error("unexpected write");
      },
      messages: (beforeSequence) => {
        if (beforeSequence === undefined) {
          return {
            items: [latestAssistant],
            next_before_sequence: 9,
          };
        }
        if (beforeSequence === 9) {
          return {
            items: [report],
            next_before_sequence: 5,
          };
        }
        return {
          items: [trigger],
          next_before_sequence: null,
        };
      },
    });

    renderApp(`/chat/${SESSION.id}`);

    expect(await screen.findByText("任务执行未完成")).toBeInTheDocument();
    expect(
      await screen.findByRole("article", {
        name: "Athena的消息，序号 5",
      }),
    ).toHaveTextContent("刷新后仍可见的失败报告");
    expect(
      screen.queryByText(/未找到对应的权威说明/),
    ).not.toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([input]) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        return (
          url.pathname === `/api/v1/sessions/${SESSION.id}/messages` &&
          url.searchParams.get("before_sequence") === "9"
        );
      }),
    ).toBe(true);
  });

  it("failed 报告身份不匹配时保留瞬时文本并显示明确安全状态", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const running = makeRun({
      id: "run-invalid-report",
      session_id: SESSION.id,
      status: "running",
    });
    const trigger = makeMessage({
      id: running.trigger_message_id,
      parts: [{ text: "触发报告校验", type: "text" }],
      reply_to_message_id: null,
      role: "user",
      run_id: null,
      sequence: 3,
      session_id: SESSION.id,
    });
    const wrongRunReport = makeMessage({
      id: "msg-invalid-report",
      parts: [{ text: "不能冒充目标 Run 报告", type: "text" }],
      reply_to_message_id: trigger.id,
      role: "assistant",
      run_id: "run-other",
      sequence: 5,
      session_id: SESSION.id,
      status: "completed",
    });
    installWorkspaceFetch({
      getRuns: () => [running],
      handleWrite: async () => {
        throw new Error("unexpected write");
      },
      messages: () => ({
        items: [wrongRunReport, trigger],
        next_before_sequence: null,
      }),
    });
    renderApp(`/chat/${SESSION.id}`);

    expect(
      await screen.findByRole("button", { name: "运行中，暂不可发送" }),
    ).toBeDisabled();
    const source = FakeEventSource.instances[0];
    act(() => {
      source.emit("text_delta", {
        payload: { text: "身份不符时必须保留的瞬时文本" },
      });
      source.emit("run_event", {
        payload: {
          event: {
            created_at: "2026-11-10T03:00:03Z",
            id: "event-invalid-report",
            payload: {
              report_message_id: wrongRunReport.id,
              safe_error_code: "run_context_safety_stop",
            },
            run_id: running.id,
            sequence: 4,
            type: "failed",
          },
        },
      });
    });

    expect(
      await screen.findByText(
        "任务失败，但未找到对应的权威说明。清空草稿前请重试同步。",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText("Athena 的临时流式输出"),
    ).toHaveTextContent("身份不符时必须保留的瞬时文本");
  });

  it("SSE run_event 先于查询刷新投影取消中，终态快照关闭连接", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const running = makeRun({
      id: "run-stream-state",
      session_id: SESSION.id,
      status: "running",
    });
    installWorkspaceFetch({
      getRuns: () => [running],
      handleWrite: async () => {
        throw new Error("unexpected write");
      },
      messages: () => ({ items: [], next_before_sequence: null }),
    });

    renderApp(`/chat/${SESSION.id}`);

    expect(
      await screen.findByRole("button", { name: "运行中，暂不可发送" }),
    ).toBeDisabled();
    const source = FakeEventSource.instances[0];
    act(() => {
      source.emit("run_event", {
        payload: {
          event: {
            created_at: "2026-11-10T03:00:01Z",
            id: "event-cancel",
            payload: {},
            run_id: running.id,
            sequence: 2,
            type: "cancel_requested",
          },
        },
      });
    });
    expect(await screen.findByText("正在停止当前任务…")).toBeInTheDocument();
    expect(source.closed).toBe(false);

    act(() => {
      source.emit("snapshot", {
        payload: {
          event_sequence: 3,
          events: [],
          events_after_sequence: null,
          events_complete: true,
          messages: [],
          messages_before_sequence: null,
          messages_complete: true,
          status: "cancelled",
        },
      });
    });
    expect(await screen.findByText("本次任务已取消")).toBeInTheDocument();
    expect(source.closed).toBe(true);
  });

  it("停止返回 cancelling 时不提前显示已取消，重复点击仍请求服务端", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    let runs = [
      makeRun({
        id: "run-running",
        session_id: SESSION.id,
        status: "running",
      }),
    ];
    let cancelCalls = 0;
    installWorkspaceFetch({
      getRuns: () => runs,
      handleWrite: async (pathname) => {
        if (pathname.endsWith("/cancel")) {
          cancelCalls += 1;
          runs = [{ ...runs[0], status: "cancelling" }];
          return jsonResponse(runs[0]);
        }
        throw new Error(`Unexpected write: ${pathname}`);
      },
      messages: () => ({ items: [], next_before_sequence: null }),
    });
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);

    await screen.findByRole("button", { name: "运行中，暂不可发送" });
    await user.click(screen.getByRole("button", { name: "停止任务" }));
    expect(await screen.findByText("正在停止当前任务…")).toBeInTheDocument();
    expect(screen.queryByText("本次任务已取消")).not.toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "再次请求停止" }),
    );
    expect(cancelCalls).toBe(2);
  });

  it("run_already_active 保留草稿并同步为服务端活跃状态", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    let runs: Run[] = [];
    const running = makeRun({
      id: "run-active",
      session_id: SESSION.id,
      status: "running",
    });
    installWorkspaceFetch({
      getRuns: () => runs,
      handleWrite: async (pathname, _method, init) => {
        if (pathname.endsWith("/runs")) {
          const body = JSON.parse(String(init?.body)) as { text: string };
          expect(body.text).toBe("不能丢失的草稿");
          runs = [running];
          return jsonResponse({ detail: "run_already_active" }, 409);
        }
        throw new Error(`Unexpected write: ${pathname}`);
      },
      messages: () => ({ items: [], next_before_sequence: null }),
    });
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);

    const composer = await screen.findByLabelText("对话草稿");
    await user.type(composer, "不能丢失的草稿");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(
      await screen.findByText(
        "该会话已有运行中的任务，请等待完成或先停止。",
      ),
    ).toBeInTheDocument();
    expect(composer).toHaveValue("不能丢失的草稿");
    expect(
      await screen.findByRole("button", { name: "运行中，暂不可发送" }),
    ).toBeDisabled();
  });

  it("普通提交网络失败后用同一 request_id 重试并保留原文", async () => {
    let attempt = 0;
    const requestIds: string[] = [];
    installWorkspaceFetch({
      getRuns: () => [],
      handleWrite: async (pathname, _method, init) => {
        if (pathname.endsWith("/runs")) {
          attempt += 1;
          const body = JSON.parse(String(init?.body)) as {
            request_id: string;
            text: string;
          };
          requestIds.push(body.request_id);
          expect(body.text).toBe("网络失败也要保留");
          throw new TypeError("Failed to fetch");
        }
        throw new Error(`Unexpected write: ${pathname}`);
      },
      messages: () => ({ items: [], next_before_sequence: null }),
    });
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);

    const composer = await screen.findByLabelText("对话草稿");
    await user.type(composer, "网络失败也要保留");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    expect(
      await screen.findByText("无法连接服务器，请检查网络后重试。"),
    ).toBeInTheDocument();
    expect(composer).toHaveValue("网络失败也要保留");

    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(attempt).toBe(2));
    expect(requestIds[0]).toBe(requestIds[1]);
  });

  it("提交响应延迟期间继续输入时，只清空仍等于原始草稿的输入", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const submitGate = deferred<Response>();
    const started = deferred<void>();
    const queued = makeRun({
      id: "run-after-edit",
      session_id: SESSION.id,
      status: "queued",
    });
    let runs: Run[] = [];
    installWorkspaceFetch({
      getRuns: () => runs,
      handleWrite: async (pathname, _method, init) => {
        if (pathname.endsWith("/runs")) {
          const body = JSON.parse(String(init?.body)) as { text: string };
          expect(body.text).toBe("原始草稿");
          started.resolve();
          await submitGate.promise;
          runs = [queued];
          return jsonResponse(queued, 201);
        }
        throw new Error(`Unexpected write: ${pathname}`);
      },
      messages: () => ({ items: [], next_before_sequence: null }),
    });
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);

    const composer = await screen.findByLabelText("对话草稿");
    await user.type(composer, "原始草稿 ");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await started.promise;

    await user.clear(composer);
    await user.type(composer, "原始草稿");
    expect(composer).toHaveValue("原始草稿");

    await act(async () => {
      submitGate.resolve(jsonResponse(queued, 201));
      await submitGate.promise;
    });

    expect(
      await screen.findByText("任务排队中，即将开始处理…"),
    ).toBeInTheDocument();
    expect(composer).toHaveValue("原始草稿");
  });

  it("提交 HTTP 成功但响应投影过期时仍确认成功，并保留新编辑", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const submitGate = deferred<Response>();
    const submitStarted = deferred<void>();
    const responseRun = makeRun({
      event_sequence: 1,
      id: "run-submit-stale-success",
      session_id: SESSION.id,
      status: "queued",
    });
    const advancedRun = makeRun({
      event_sequence: 2,
      id: responseRun.id,
      session_id: SESSION.id,
      status: "completed",
    });
    let runs: Run[] = [];
    const requestIds: string[] = [];
    installWorkspaceFetch({
      getRuns: () => runs,
      handleWrite: async (pathname, method, init) => {
        if (pathname.endsWith("/runs") && method === "POST") {
          const body = JSON.parse(String(init?.body)) as {
            request_id: string;
            text: string;
          };
          requestIds.push(body.request_id);
          expect(body.text).toBe("原始提交");
          submitStarted.resolve();
          await submitGate.promise;
          return jsonResponse(responseRun, 201);
        }
        throw new Error(`Unexpected write: ${pathname}`);
      },
      messages: () => ({ items: [], next_before_sequence: null }),
    });
    const user = userEvent.setup();
    const { client } = renderApp(`/chat/${SESSION.id}`);

    const composer = await screen.findByLabelText("对话草稿");
    await user.type(composer, "原始提交");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await submitStarted.promise;

    act(() => {
      runs = [advancedRun];
      client.setQueryData(chatKeys.latestRun(SESSION.id), {
        items: [advancedRun],
        next_cursor: null,
      });
      client.setQueryData(
        chatKeys.run(SESSION.id, advancedRun.id),
        advancedRun,
      );
    });
    await waitFor(() => {
      expect(
        screen.queryByText("任务排队中，即将开始处理…"),
      ).not.toBeInTheDocument();
    });

    await user.clear(composer);
    await user.type(composer, "提交期间的新草稿");
    await act(async () => {
      submitGate.resolve(jsonResponse(responseRun, 201));
      await submitGate.promise;
    });

    expect(
      await screen.findByText(
        "消息已提交，正在按服务端最新运行状态同步。",
      ),
    ).toBeInTheDocument();
    expect(composer).toHaveValue("提交期间的新草稿");
    expect(
      screen.getByRole("button", { name: "发送消息" }),
    ).toBeEnabled();
    expect(
      screen.queryByText("任务排队中，即将开始处理…"),
    ).not.toBeInTheDocument();
    expect(requestIds).toHaveLength(1);
    expect(
      screen.queryByText("无法连接服务器，请检查网络后重试。"),
    ).not.toBeInTheDocument();
  });

  it("补充回答在途时停止与快速重复提交都被共享锁阻止，最终状态不倒退", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const answerGate = deferred<Response>();
    const answerStarted = deferred<void>();
    let runs = [
      makeRun({
        id: "run-input-lock",
        pending_input: {
          created_at: "2026-11-10T03:00:00Z",
          id: "input-lock",
          question_message_id: "msg-question-lock",
          status: "pending",
        },
        session_id: SESSION.id,
        status: "waiting_for_input",
      }),
    ];
    let answerCalls = 0;
    let cancelCalls = 0;
    installWorkspaceFetch({
      getRuns: () => runs,
      handleWrite: async (pathname) => {
        if (pathname.endsWith("/input-requests/input-lock/answer")) {
          answerCalls += 1;
          answerStarted.resolve();
          await answerGate.promise;
          runs = [
            {
              ...runs[0],
              pending_input: null,
              status: "queued",
            },
          ];
          return jsonResponse(runs[0]);
        }
        if (pathname.endsWith("/cancel")) {
          cancelCalls += 1;
          runs = [{ ...runs[0], status: "cancelling" }];
          return jsonResponse(runs[0]);
        }
        throw new Error(`Unexpected write: ${pathname}`);
      },
      messages: () => ({
        items: [
          makeMessage({
            id: "msg-question-lock",
            parts: [{ text: "请补充执行范围。", type: "text" }],
            sequence: 2,
            session_id: SESSION.id,
          }),
        ],
        next_before_sequence: null,
      }),
    });
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);

    const answerInput = await screen.findByLabelText("补充信息");
    await user.type(answerInput, "范围保持不变");
    const answerButton = screen.getByRole("button", { name: "提交回答" });
    act(() => {
      // 同一事件循环内快速触发两次，验证程序入口也会被同步 ref 拦住。
      answerButton.click();
      answerButton.click();
    });
    await answerStarted.promise;

    expect(answerCalls).toBe(1);
    expect(answerButton).toBeDisabled();
    const stopButton = screen.getByRole("button", { name: "停止任务" });
    expect(stopButton).toBeDisabled();
    await user.click(stopButton);
    expect(cancelCalls).toBe(0);

    await act(async () => {
      answerGate.resolve(
        jsonResponse({
          ...runs[0],
          pending_input: null,
          status: "queued",
        }),
      );
      await answerGate.promise;
    });
    expect(
      await screen.findByText("任务排队中，即将开始处理…"),
    ).toBeInTheDocument();
    expect(answerCalls).toBe(1);
    expect(cancelCalls).toBe(0);
  });

  it("回答 HTTP 成功但响应投影过期时仍清空原始回答并确认成功", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const answerGate = deferred<Response>();
    const answerStarted = deferred<void>();
    const waiting = makeRun({
      event_sequence: 1,
      id: "run-answer-stale-success",
      pending_input: {
        created_at: "2026-11-10T03:00:00Z",
        id: "input-stale-success",
        question_message_id: "msg-question-stale-success",
        status: "pending",
      },
      session_id: SESSION.id,
      status: "waiting_for_input",
    });
    const responseRun = {
      ...waiting,
      pending_input: null,
      status: "queued" as const,
    };
    const advancedRun = makeRun({
      event_sequence: 2,
      id: waiting.id,
      pending_input: null,
      session_id: SESSION.id,
      status: "completed",
    });
    let runs: Run[] = [waiting];
    const requestIds: string[] = [];
    installWorkspaceFetch({
      getRuns: () => runs,
      handleWrite: async (pathname, _method, init) => {
        if (
          pathname.endsWith(
            "/input-requests/input-stale-success/answer",
          )
        ) {
          const body = JSON.parse(String(init?.body)) as {
            request_id: string;
            text: string;
          };
          requestIds.push(body.request_id);
          expect(body.text).toBe("原始补充回答");
          answerStarted.resolve();
          await answerGate.promise;
          return jsonResponse(responseRun);
        }
        throw new Error(`Unexpected write: ${pathname}`);
      },
      messages: () => ({
        items: [
          makeMessage({
            id: "msg-question-stale-success",
            parts: [{ text: "请补充回答。", type: "text" }],
            sequence: 2,
            session_id: SESSION.id,
          }),
        ],
        next_before_sequence: null,
      }),
    });
    const user = userEvent.setup();
    const { client } = renderApp(`/chat/${SESSION.id}`);

    const answerInput = await screen.findByLabelText("补充信息");
    await user.type(answerInput, "原始补充回答");
    await user.click(screen.getByRole("button", { name: "提交回答" }));
    await answerStarted.promise;

    act(() => {
      runs = [advancedRun];
      client.setQueryData(chatKeys.latestRun(SESSION.id), {
        items: [advancedRun],
        next_cursor: null,
      });
      client.setQueryData(
        chatKeys.run(SESSION.id, advancedRun.id),
        advancedRun,
      );
    });
    await waitFor(() => {
      expect(
        screen.queryByRole("button", { name: "提交回答" }),
      ).not.toBeInTheDocument();
    });

    await act(async () => {
      answerGate.resolve(jsonResponse(responseRun));
      await answerGate.promise;
    });

    expect(
      await screen.findByText(
        "补充回答已提交，正在按服务端最新运行状态同步。",
      ),
    ).toBeInTheDocument();
    expect(answerInput).toHaveValue("");
    expect(
      screen.getByRole("button", { name: "发送消息" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("任务排队中，即将开始处理…"),
    ).not.toBeInTheDocument();
    expect(requestIds).toHaveLength(1);
    expect(
      screen.queryByText("无法连接服务器，请检查网络后重试。"),
    ).not.toBeInTheDocument();
  });

  it("已知 request_id 冲突保留草稿，下一次提交改用新的 request_id", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    let attempt = 0;
    let runs: Run[] = [];
    const requestIds: string[] = [];
    installWorkspaceFetch({
      getRuns: () => runs,
      handleWrite: async (pathname, _method, init) => {
        if (pathname.endsWith("/runs")) {
          attempt += 1;
          const body = JSON.parse(String(init?.body)) as {
            request_id: string;
            text: string;
          };
          requestIds.push(body.request_id);
          expect(body.text).toBe("冲突后仍保留");
          if (attempt === 1) {
            return jsonResponse(
              { detail: "run_request_id_conflict" },
              409,
            );
          }
          runs = [
            makeRun({
              id: "run-after-conflict",
              session_id: SESSION.id,
              status: "queued",
            }),
          ];
          return jsonResponse(runs[0], 201);
        }
        throw new Error(`Unexpected write: ${pathname}`);
      },
      messages: () => ({ items: [], next_before_sequence: null }),
    });
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);

    const composer = await screen.findByLabelText("对话草稿");
    await user.type(composer, "冲突后仍保留");
    await user.click(screen.getByRole("button", { name: "发送消息" }));
    expect(
      await screen.findByText(
        "请求标识已被另一条内容占用，请检查内容后重新发送。",
      ),
    ).toBeInTheDocument();
    expect(composer).toHaveValue("冲突后仍保留");

    await user.click(screen.getByRole("button", { name: "发送消息" }));
    await waitFor(() => expect(attempt).toBe(2));
    expect(requestIds[0]).not.toBe(requestIds[1]);
  });

  it("补充输入失败保留文本，重试复用 request_id，成功后回到 queued", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    let runs = [
      makeRun({
        id: "run-input",
        pending_input: {
          created_at: "2026-11-10T03:00:00Z",
          id: "input-1",
          question_message_id: "msg-question",
          status: "pending",
        },
        session_id: SESSION.id,
        status: "waiting_for_input",
      }),
    ];
    let answerAttempt = 0;
    const requestIds: string[] = [];
    installWorkspaceFetch({
      getRuns: () => runs,
      handleWrite: async (pathname, _method, init) => {
        if (pathname.endsWith("/input-requests/input-1/answer")) {
          answerAttempt += 1;
          const body = JSON.parse(String(init?.body)) as {
            request_id: string;
            text: string;
          };
          requestIds.push(body.request_id);
          expect(body.text).toBe("季度口径按自然季度");
          if (answerAttempt === 1) {
            throw new TypeError("Failed to fetch");
          }
          runs = [
            {
              ...runs[0],
              pending_input: null,
              status: "queued",
            },
          ];
          return jsonResponse(runs[0]);
        }
        throw new Error(`Unexpected write: ${pathname}`);
      },
      messages: () => ({
        items: [
          makeMessage({
            id: "msg-question",
            parts: [{ text: "请确认季度统计口径？", type: "text" }],
            sequence: 2,
            session_id: SESSION.id,
          }),
        ],
        next_before_sequence: null,
      }),
    });
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);

    const questions = await screen.findAllByText("请确认季度统计口径？");
    expect(questions.length).toBeGreaterThan(0);
    expect(FakeEventSource.instances).toHaveLength(0);
    const answerInput = screen.getByLabelText("补充信息");
    await user.type(answerInput, "季度口径按自然季度");
    await user.click(screen.getByRole("button", { name: "提交回答" }));
    expect(
      await screen.findByText("无法连接服务器，请检查网络后重试。"),
    ).toBeInTheDocument();
    expect(answerInput).toHaveValue("季度口径按自然季度");

    await user.click(screen.getByRole("button", { name: "提交回答" }));
    expect(
      await screen.findByText("任务排队中，即将开始处理…"),
    ).toBeInTheDocument();
    expect(requestIds[0]).toBe(requestIds[1]);
    expect(answerInput).toHaveValue("");
    await waitFor(() => {
      expect(FakeEventSource.instances).toHaveLength(1);
    });
  });

  it("补充输入请求失效时保留原文并重新同步服务端活跃状态", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    let runs = [
      makeRun({
        id: "run-stale-input",
        pending_input: {
          created_at: "2026-11-10T03:00:00Z",
          id: "input-stale",
          question_message_id: "msg-stale-question",
          status: "pending",
        },
        session_id: SESSION.id,
        status: "waiting_for_input",
      }),
    ];
    installWorkspaceFetch({
      getRuns: () => runs,
      handleWrite: async (pathname) => {
        if (
          pathname.endsWith(
            "/input-requests/input-stale/answer",
          )
        ) {
          runs = [
            {
              ...runs[0],
              pending_input: null,
              status: "running",
            },
          ];
          return jsonResponse(
            { detail: "run_input_request_not_pending" },
            409,
          );
        }
        throw new Error(`Unexpected write: ${pathname}`);
      },
      messages: () => ({
        items: [
          makeMessage({
            id: "msg-stale-question",
            parts: [{ text: "这个提问已经失效。", type: "text" }],
            sequence: 2,
            session_id: SESSION.id,
          }),
        ],
        next_before_sequence: null,
      }),
    });
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);

    const answerInput = await screen.findByLabelText("补充信息");
    await user.type(answerInput, "不能丢的补充回答");
    await user.click(screen.getByRole("button", { name: "提交回答" }));

    expect(
      await screen.findByText(
        "该补充输入请求已失效，正在同步最新运行状态。",
      ),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: "运行中，暂不可发送" }),
    ).toBeDisabled();
    expect(answerInput).toHaveValue("不能丢的补充回答");
  });
});

describe("审批与失败重试", () => {
  it("展示真实审批字段与参数，并逐项 approve/reject", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const pendingOne: RunPendingApproval = {
      batch_message_id: "batch-1",
      call_id: "call-record-1",
      model_name: "write_crm",
      ordinal: 1,
      requested_at: "2026-11-10T03:00:00Z",
      safe_skill_projection: null,
      tool_kind: "mcp",
    };
    const pendingTwo: RunPendingApproval = {
      batch_message_id: "batch-1",
      call_id: "call-record-2",
      model_name: "send_email",
      ordinal: 2,
      requested_at: "2026-11-10T03:00:01Z",
      safe_skill_projection: null,
      tool_kind: "mcp",
    };
    let runs = [
      makeRun({
        id: "run-approval",
        pending_approvals: [pendingOne, pendingTwo],
        session_id: SESSION.id,
        status: "waiting_for_approval",
      }),
    ];
    const decisions: Array<{ decision: string; requestId: string }> = [];
    installWorkspaceFetch({
      getRuns: () => runs,
      handleWrite: async (pathname, _method, init) => {
        const match = /tool-calls\/([^/]+)\/approval$/.exec(pathname);
        if (match) {
          const body = JSON.parse(String(init?.body)) as {
            decision: "approve" | "reject";
            request_id: string;
          };
          decisions.push({
            decision: body.decision,
            requestId: body.request_id,
          });
          if (body.decision === "approve") {
            runs = [
              {
                ...runs[0],
                pending_approvals: [pendingTwo],
              },
            ];
          } else {
            runs = [
              {
                ...runs[0],
                pending_approvals: [],
                status: "queued",
              },
            ];
          }
          return jsonResponse(runs[0]);
        }
        throw new Error(`Unexpected write: ${pathname}`);
      },
      messages: () => ({ items: [], next_before_sequence: null }),
      toolCallDetail: (_runId, callId) =>
        makeToolCallDetail({
          id: callId,
          ordinal: callId.endsWith("1") ? 1 : 2,
          tool_call_id: `protocol-${callId}`,
        }),
    });
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);

    expect(await screen.findByText("等待工具审批")).toBeInTheDocument();
    expect(FakeEventSource.instances).toHaveLength(0);
    expect(screen.getByText("write_crm")).toBeInTheDocument();
    expect(screen.getByText("send_email")).toBeInTheDocument();
    expect(screen.getByText("1 / 2")).toBeInTheDocument();
    expect(screen.getByText("2 / 2")).toBeInTheDocument();

    await user.click(
      screen.getAllByRole("button", { name: "查看参数" })[0],
    );
    expect(await screen.findByText("query")).toBeInTheDocument();
    expect(screen.getByText("真实参数")).toBeInTheDocument();

    await user.click(screen.getAllByRole("button", { name: "确认执行" })[0]);
    await waitFor(() => expect(decisions).toHaveLength(1));
    expect(decisions[0].decision).toBe("approve");

    await user.click(await screen.findByRole("button", { name: "拒绝" }));
    await waitFor(() => expect(decisions).toHaveLength(2));
    expect(decisions[1].decision).toBe("reject");
    expect(
      await screen.findByText("任务排队中，即将开始处理…"),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(FakeEventSource.instances).toHaveLength(1);
    });
  });

  it("审批请求串行提交，延迟响应不会覆盖最后的 queued 状态", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const pendingOne: RunPendingApproval = {
      batch_message_id: "batch-serial",
      call_id: "call-serial-1",
      model_name: "write_crm",
      ordinal: 1,
      requested_at: "2026-11-10T03:00:00Z",
      safe_skill_projection: null,
      tool_kind: "mcp",
    };
    const pendingTwo: RunPendingApproval = {
      batch_message_id: "batch-serial",
      call_id: "call-serial-2",
      model_name: "send_email",
      ordinal: 2,
      requested_at: "2026-11-10T03:00:01Z",
      safe_skill_projection: null,
      tool_kind: "mcp",
    };
    let runs = [
      makeRun({
        id: "run-approval-serial",
        pending_approvals: [pendingOne, pendingTwo],
        session_id: SESSION.id,
        status: "waiting_for_approval",
      }),
    ];
    const firstDecision = deferred<Response>();
    const decisions: string[] = [];
    installWorkspaceFetch({
      getRuns: () => runs,
      handleWrite: async (pathname, _method, init) => {
        const match = /tool-calls\/([^/]+)\/approval$/.exec(pathname);
        if (!match) {
          throw new Error(`Unexpected write: ${pathname}`);
        }
        const callId = decodeURIComponent(match[1]);
        const body = JSON.parse(String(init?.body)) as {
          decision: "approve" | "reject";
        };
        decisions.push(`${callId}:${body.decision}`);
        if (decisions.length === 1) {
          return firstDecision.promise;
        }
        runs = [
          {
            ...runs[0],
            pending_approvals: [],
            status: "queued",
          },
        ];
        return jsonResponse(runs[0]);
      },
      messages: () => ({ items: [], next_before_sequence: null }),
    });
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);

    await screen.findByText("等待工具审批");
    await user.click(screen.getAllByRole("button", { name: "确认执行" })[0]);
    await waitFor(() => expect(decisions).toHaveLength(1));

    expect(
      screen.getByRole("button", { name: "正在提交…" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "等待其他审批…" }),
    ).toBeDisabled();
    screen
      .getAllByRole("button", { name: "拒绝" })
      .forEach((button) => expect(button).toBeDisabled());
    const stopButton = screen.getByRole("button", { name: "停止任务" });
    expect(stopButton).toBeDisabled();

    await user.click(
      screen.getByRole("button", { name: "等待其他审批…" }),
    );
    await user.click(stopButton);
    expect(decisions).toHaveLength(1);

    runs = [
      {
        ...runs[0],
        pending_approvals: [pendingTwo],
      },
    ];
    await act(async () => {
      // 第一项决定延迟返回；期间第二项无法发出请求，因此不存在可倒退覆盖的迟到响应。
      firstDecision.resolve(jsonResponse(runs[0]));
      await firstDecision.promise;
    });

    expect(await screen.findByText("send_email")).toBeInTheDocument();
    expect(screen.queryByText("write_crm")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "确认执行" }));
    await waitFor(() => expect(decisions).toHaveLength(2));
    expect(decisions).toEqual([
      "call-serial-1:approve",
      "call-serial-2:approve",
    ]);
    expect(
      await screen.findByText("任务排队中，即将开始处理…"),
    ).toBeInTheDocument();
  });

  it("审批响应迟到时不能回退先完成的取消状态", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const pending: RunPendingApproval = {
      batch_message_id: "batch-stale-approval",
      call_id: "call-stale-approval",
      model_name: "write_crm",
      ordinal: 1,
      requested_at: "2026-11-10T03:00:00Z",
      safe_skill_projection: null,
      tool_kind: "mcp",
    };
    const waiting = makeRun({
      event_sequence: 1,
      id: "run-stale-approval",
      pending_approvals: [pending],
      session_id: SESSION.id,
      status: "waiting_for_approval",
    });
    const cancelling = {
      ...waiting,
      cancel_requested_at: "2026-11-10T03:01:00Z",
      event_sequence: 2,
      pending_approvals: [pending],
      status: "cancelling" as const,
    };
    const approvalGate = deferred<Response>();
    const approvalStarted = deferred<void>();
    let runs: Run[] = [waiting];
    let approvalCalls = 0;
    installWorkspaceFetch({
      getRuns: () => runs,
      handleWrite: async (pathname) => {
        if (/tool-calls\/[^/]+\/approval$/.test(pathname)) {
          approvalCalls += 1;
          approvalStarted.resolve();
          return approvalGate.promise;
        }
        throw new Error(`Unexpected write: ${pathname}`);
      },
      messages: () => ({ items: [], next_before_sequence: null }),
    });
    const user = userEvent.setup();
    const { client } = renderApp(`/chat/${SESSION.id}`);

    expect(await screen.findByText("等待工具审批")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "确认执行" }));
    await approvalStarted.promise;

    act(() => {
      runs = [cancelling];
      client.setQueryData(chatKeys.latestRun(SESSION.id), {
        items: [cancelling],
        next_cursor: null,
      });
      client.setQueryData(
        chatKeys.run(SESSION.id, waiting.id),
        cancelling,
      );
    });
    expect(await screen.findByText("正在停止当前任务…")).toBeInTheDocument();

    await act(async () => {
      approvalGate.resolve(jsonResponse(waiting));
      await approvalGate.promise;
    });

    await waitFor(() => {
      expect(screen.getByText("正在停止当前任务…")).toBeInTheDocument();
    });
    expect(screen.queryByText("等待工具审批")).not.toBeInTheDocument();
    expect(approvalCalls).toBe(1);
  });

  it("取消请求在途时审批入口被共享锁阻止，最终保持已取消", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const pending: RunPendingApproval = {
      batch_message_id: "batch-cancel-lock",
      call_id: "call-cancel-lock",
      model_name: "send_email",
      ordinal: 1,
      requested_at: "2026-11-10T03:00:00Z",
      safe_skill_projection: null,
      tool_kind: "mcp",
    };
    let runs = [
      makeRun({
        id: "run-cancel-lock",
        pending_approvals: [pending],
        session_id: SESSION.id,
        status: "waiting_for_approval",
      }),
    ];
    const cancelGate = deferred<Response>();
    const cancelStarted = deferred<void>();
    let approvalCalls = 0;
    installWorkspaceFetch({
      getRuns: () => runs,
      handleWrite: async (pathname) => {
        if (pathname.endsWith("/cancel")) {
          cancelStarted.resolve();
          await cancelGate.promise;
          runs = [{ ...runs[0], status: "cancelled" }];
          return jsonResponse(runs[0]);
        }
        if (/tool-calls\/[^/]+\/approval$/.test(pathname)) {
          approvalCalls += 1;
          return jsonResponse(runs[0]);
        }
        throw new Error(`Unexpected write: ${pathname}`);
      },
      messages: () => ({ items: [], next_before_sequence: null }),
    });
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);

    await screen.findByText("等待工具审批");
    await user.click(screen.getByRole("button", { name: "停止任务" }));
    await cancelStarted.promise;
    const approve = screen.getByRole("button", {
      name: "等待其他审批…",
    });
    expect(approve).toBeDisabled();
    await user.click(approve);
    expect(approvalCalls).toBe(0);

    await act(async () => {
      cancelGate.resolve(
        jsonResponse({ ...runs[0], status: "cancelled" }),
      );
      await cancelGate.promise;
    });
    expect(await screen.findByText("本次任务已取消")).toBeInTheDocument();
    expect(approvalCalls).toBe(0);
  });

  it("失败 Run 先展示完整风险，网络失败复用 request_id，确认后切换新 Run", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const failed = makeRun({
      finished_at: "2026-11-10T03:05:00Z",
      id: "run-failed",
      safe_error_code: "tool_execution_failed",
      session_id: SESSION.id,
      status: "failed",
    });
    const retried = makeRun({
      id: "run-retried",
      retry_of_run_id: failed.id,
      session_id: SESSION.id,
      status: "queued",
    });
    let runs = [failed];
    let retryAttempt = 0;
    const requestIds: string[] = [];
    const fetchMock = installWorkspaceFetch({
      getRuns: () => runs,
      handleWrite: async (pathname, _method, init) => {
        if (pathname.endsWith("/retry")) {
          retryAttempt += 1;
          const body = JSON.parse(String(init?.body)) as {
            confirm_unknown: boolean;
            request_id: string;
          };
          expect(body.confirm_unknown).toBe(true);
          requestIds.push(body.request_id);
          if (retryAttempt === 1) {
            throw new TypeError("Failed to fetch");
          }
          runs = [retried, failed];
          return jsonResponse(retried);
        }
        throw new Error(`Unexpected write: ${pathname}`);
      },
      messages: () => ({ items: [], next_before_sequence: null }),
    });
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);

    await screen.findByText("任务执行未完成");
    await user.click(
      screen.getByRole("button", { name: "重新执行" }),
    );
    const dialog = screen.getByRole("alertdialog", {
      name: "重新执行本次任务",
    });
    expect(
      within(dialog).getByText(/整个任务将从头执行/),
    ).toBeInTheDocument();
    expect(
      within(dialog).queryByText(/confirm_unknown/),
    ).not.toBeInTheDocument();
    expect(
      within(dialog).queryByRole("note", { name: "服务端重试警告" }),
    ).not.toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([input]) =>
        new URL(getRequestUrl(input), "http://localhost").pathname.endsWith(
          "/retry",
        ),
      ),
    ).toBe(false);

    await user.click(
      within(dialog).getByRole("button", { name: "确认重新执行" }),
    );
    expect(
      await within(dialog).findByText(
        "无法连接服务器，请检查网络后重试。",
      ),
    ).toBeInTheDocument();
    await user.click(
      within(dialog).getByRole("button", { name: "确认重新执行" }),
    );

    expect(
      await screen.findByText("任务排队中，即将开始处理…"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("alertdialog", { name: "重新执行本次任务" }),
    ).not.toBeInTheDocument();
    expect(requestIds).toHaveLength(2);
    expect(requestIds[0]).toBe(requestIds[1]);
    await waitFor(() => {
      expect(FakeEventSource.instances).toHaveLength(1);
    });
  });

  it("重试弹窗原样展示服务端 warning，新 Run 的 warning 也会展示", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const serverWarning =
      "服务端补充：该任务包含一次结果未知的外部写入，请确认影响范围。";
    const retriedWarning =
      "新 Run：此前部分操作可能已经生效，请结合外部系统核对。";
    const failed = makeRun({
      finished_at: "2026-11-10T03:05:00Z",
      id: "run-failed-warning",
      retry_warning: serverWarning,
      session_id: SESSION.id,
      status: "failed",
    });
    const retried = makeRun({
      id: "run-retried-warning",
      retry_of_run_id: failed.id,
      retry_warning: retriedWarning,
      session_id: SESSION.id,
      status: "queued",
    });
    let runs = [failed];
    installWorkspaceFetch({
      getRuns: () => runs,
      handleWrite: async (pathname) => {
        if (pathname.endsWith("/retry")) {
          runs = [retried, failed];
          return jsonResponse(retried);
        }
        throw new Error(`Unexpected write: ${pathname}`);
      },
      messages: () => ({ items: [], next_before_sequence: null }),
    });
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);

    await screen.findByText("任务执行未完成");
    await user.click(
      screen.getByRole("button", { name: "重新执行" }),
    );
    const dialog = screen.getByRole("alertdialog", {
      name: "重新执行本次任务",
    });
    expect(
      within(dialog).getByText(/整个任务将从头执行/),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("note", { name: "服务端重试警告" }),
    ).toHaveTextContent(serverWarning);

    await user.click(
      within(dialog).getByRole("button", { name: "确认重新执行" }),
    );
    expect(
      await screen.findByText("任务排队中，即将开始处理…"),
    ).toBeInTheDocument();
    expect(await screen.findByText(retriedWarning)).toBeInTheDocument();
  });

  it("服务端 warning 与固定风险完全相同时只展示一次", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const fixedWarning =
      "整个任务将从头执行，可能重复此前已成功的操作；原操作结果未知时也可能已生效。";
    const failed = makeRun({
      finished_at: "2026-11-10T03:05:00Z",
      id: "run-failed-fixed-warning",
      retry_warning: fixedWarning,
      session_id: SESSION.id,
      status: "failed",
    });
    installWorkspaceFetch({
      getRuns: () => [failed],
      handleWrite: async () => {
        throw new Error("unexpected write");
      },
      messages: () => ({ items: [], next_before_sequence: null }),
    });
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);

    await screen.findByText("任务执行未完成");
    await user.click(
      screen.getByRole("button", { name: "重新执行" }),
    );
    const dialog = screen.getByRole("alertdialog", {
      name: "重新执行本次任务",
    });
    expect(within(dialog).getAllByText(fixedWarning)).toHaveLength(1);
    expect(
      within(dialog).getByRole("note", { name: "服务端重试警告" }),
    ).toHaveTextContent(fixedWarning);
  });
});

describe("窄屏消息内执行进度与输入区", () => {
  it.each([920, 390])(
    "%ipx 可展开/收起消息内执行进度并继续编辑输入",
    async (width) => {
      window.innerWidth = width;
      const completed = makeRun({
        id: "run-trace",
        session_id: SESSION.id,
        status: "completed",
      });
      const toolCall = makeToolCall({
        id: "call-record-trace",
        ordinal: 1,
        tool_call_id: "model-call-trace",
      });
      installWorkspaceFetch({
        getRuns: () => [completed],
        handleWrite: async () => {
          throw new Error("unexpected write");
        },
        messages: () => ({ items: [], next_before_sequence: null }),
        toolCallDetail: () =>
          makeToolCallDetail({
            id: toolCall.id,
            ordinal: 1,
            tool_call_id: toolCall.tool_call_id,
          }),
        toolCalls: () => [toolCall],
      });
      const user = userEvent.setup();

      renderApp(`/chat/${SESSION.id}`);

      const composer = await screen.findByLabelText("对话草稿");
      await user.type(composer, `窄屏草稿 ${width}`);
      expect(composer).toHaveValue(`窄屏草稿 ${width}`);

      const progress = await screen.findByRole("region", { name: "执行进度" });
      await user.click(
        within(progress).getByRole("button", { name: /已完成 1 个步骤/ }),
      );
      const stepList = within(progress).getByRole("list", {
        name: "工具调用步骤",
      });
      await user.click(
        within(stepList).getByRole("button", { name: /web_search/ }),
      );
      expect(await within(progress).findByText("真实参数")).toBeInTheDocument();
      await user.click(
        within(progress).getByRole("button", { name: /收起/ }),
      );
      expect(
        within(progress).queryByRole("list", { name: "工具调用步骤" }),
      ).not.toBeInTheDocument();
      expect(composer).toHaveValue(`窄屏草稿 ${width}`);
    },
  );
});

describe("新草稿创建与提交部分失败", () => {
  it("Session 创建成功但 Run 结果不明时进入会话，保留原文和 request_id 且不重复创建 Session", async () => {
    window.innerWidth = 1024;
    vi.stubGlobal("EventSource", FakeEventSource);
    const created = makeSession({
      id: "session-created",
      project_id: null,
      title: null,
    });
    const queued = makeRun({
      id: "run-created",
      session_id: created.id,
      status: "queued",
    });
    const runRequestIds: string[] = [];
    let sessionCreateCount = 0;
    let runAttempt = 0;
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/projects") {
          return jsonResponse(listResponse([], 1, 100, 0));
        }
        if (url.pathname === "/api/v1/skills") {
          return jsonResponse({ items: [] });
        }
        if (url.pathname === "/api/v1/sessions" && method === "GET") {
          return jsonResponse(listResponse([]));
        }
        if (url.pathname === "/api/v1/sessions" && method === "POST") {
          sessionCreateCount += 1;
          return jsonResponse(created);
        }
        if (
          url.pathname === `/api/v1/sessions/${created.id}/runs` &&
          method === "POST"
        ) {
          runAttempt += 1;
          const body = JSON.parse(String(init?.body)) as {
            request_id: string;
            text: string;
          };
          runRequestIds.push(body.request_id);
          expect(body.text).toBe("草稿不能丢");
          if (runAttempt === 1) {
            throw new TypeError("Failed to fetch");
          }
          return jsonResponse(queued);
        }
        if (
          url.pathname === `/api/v1/sessions/${created.id}/runs` &&
          method === "GET"
        ) {
          return jsonResponse({ items: [], next_cursor: null });
        }
        if (
          url.pathname === `/api/v1/sessions/${created.id}` &&
          method === "GET"
        ) {
          return jsonResponse(created);
        }
        if (
          url.pathname === `/api/v1/sessions/${created.id}/messages`
        ) {
          return jsonResponse({ items: [], next_before_sequence: null });
        }
        if (url.pathname === `/api/v1/sessions/${created.id}/files`) {
          return jsonResponse({
            items: [],
            next_after_file_id: null,
          });
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/chat");

    const composer = await screen.findByLabelText("对话草稿");
    await user.type(composer, "草稿不能丢");
    await user.click(screen.getByRole("button", { name: "发送消息" }));

    expect(
      await screen.findByRole("heading", { name: "未命名对话" }),
    ).toBeInTheDocument();
    expect(
      await screen.findByText("无法连接服务器，请检查网络后重试。"),
    ).toBeInTheDocument();
    const sessionComposer = screen.getByLabelText("对话草稿");
    expect(sessionComposer).toHaveValue("草稿不能丢");

    await user.click(screen.getByRole("button", { name: "发送消息" }));
    expect(
      await screen.findByText("任务排队中，即将开始处理…"),
    ).toBeInTheDocument();
    expect(sessionComposer).toHaveValue("");
    expect(sessionCreateCount).toBe(1);
    expect(runRequestIds).toHaveLength(2);
    expect(runRequestIds[0]).toBe(runRequestIds[1]);
  });

  it("草稿页与会话页均支持 Enter 直接发送、Shift+Enter 换行，且忽略 IME 组合态与锁定态", async () => {
    window.innerWidth = 1024;
    vi.stubGlobal("EventSource", FakeEventSource);
    const created = makeSession({
      id: "session-shortcut",
      project_id: null,
      title: "快捷键会话",
    });
    const firstRun = makeRun({
      id: "run-shortcut-1",
      session_id: created.id,
      status: "completed",
    });
    const secondRun = makeRun({
      id: "run-shortcut-2",
      session_id: created.id,
      status: "running",
    });
    let runs: Run[] = [firstRun];
    const submittedTexts: string[] = [];

    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/projects") {
          return jsonResponse(listResponse([], 1, 100, 0));
        }
        if (url.pathname === "/api/v1/skills") {
          return jsonResponse({ items: [] });
        }
        if (url.pathname === "/api/v1/sessions" && method === "GET") {
          return jsonResponse(listResponse([]));
        }
        if (url.pathname === "/api/v1/sessions" && method === "POST") {
          return jsonResponse(created);
        }
        if (
          url.pathname === `/api/v1/sessions/${created.id}/runs` &&
          method === "POST"
        ) {
          const body = JSON.parse(String(init?.body)) as { text: string };
          submittedTexts.push(body.text);
          if (submittedTexts.length === 1) {
            runs = [firstRun];
            return jsonResponse(firstRun);
          }
          runs = [secondRun, firstRun];
          return jsonResponse(secondRun);
        }
        if (
          url.pathname === `/api/v1/sessions/${created.id}/runs` &&
          method === "GET"
        ) {
          return jsonResponse({ items: runs, next_cursor: null });
        }
        if (
          url.pathname.startsWith(`/api/v1/sessions/${created.id}/runs/`) &&
          url.pathname.endsWith("/tool-calls")
        ) {
          return jsonResponse({ items: [], next_after_ordinal: null });
        }
        if (
          url.pathname.startsWith(`/api/v1/sessions/${created.id}/runs/`) &&
          method === "GET"
        ) {
          const runId = url.pathname.split("/").pop();
          const matched = runs.find((item) => item.id === runId) ?? runs[0];
          return jsonResponse(matched);
        }
        if (
          url.pathname === `/api/v1/sessions/${created.id}` &&
          method === "GET"
        ) {
          return jsonResponse(created);
        }
        if (url.pathname === `/api/v1/sessions/${created.id}/messages`) {
          return jsonResponse({ items: [], next_before_sequence: null });
        }
        if (url.pathname === `/api/v1/sessions/${created.id}/files`) {
          return jsonResponse({
            items: [],
            next_after_file_id: null,
          });
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/chat");

    const draftComposer = await screen.findByLabelText("对话草稿");
    await user.type(draftComposer, "草稿快捷键发送");
    // IME 组合输入态（isComposing 或 WebKit keyCode 229）下按 Enter 不应触发提交
    fireEvent.keyDown(draftComposer, {
      isComposing: true,
      key: "Enter",
    });
    fireEvent.keyDown(draftComposer, {
      key: "Enter",
      keyCode: 229,
    });
    // Shift + Enter 仅换行不提交
    fireEvent.keyDown(draftComposer, {
      key: "Enter",
      shiftKey: true,
    });
    expect(submittedTexts).toHaveLength(0);

    // 正常 Enter 触发草稿页创建会话并提交 Run
    fireEvent.keyDown(draftComposer, {
      key: "Enter",
    });

    expect(
      await screen.findByRole("heading", { name: "快捷键会话" }),
    ).toBeInTheDocument();
    expect(submittedTexts).toEqual(["草稿快捷键发送"]);

    const sessionComposer = await screen.findByLabelText("对话草稿");
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "发送消息" }),
      ).toBeInTheDocument();
    });
    await user.type(sessionComposer, "会话页 Enter 发送");

    // 会话页 Shift+Enter 不触发提交
    fireEvent.keyDown(sessionComposer, {
      key: "Enter",
      shiftKey: true,
    });
    expect(submittedTexts).toHaveLength(1);

    // 会话页直接 Enter 触发提交
    fireEvent.keyDown(sessionComposer, {
      key: "Enter",
    });

    await waitFor(() => {
      expect(submittedTexts).toEqual([
        "草稿快捷键发送",
        "会话页 Enter 发送",
      ]);
    });
    expect(
      await screen.findByRole("button", { name: "运行中，暂不可发送" }),
    ).toBeDisabled();
    expect(sessionComposer).toHaveValue("");

    // 运行锁定态下输入新内容并按 Enter 不应重复提交
    await user.type(sessionComposer, "锁定期间不可发送");
    fireEvent.keyDown(sessionComposer, {
      key: "Enter",
    });
    expect(submittedTexts).toHaveLength(2);
    expect(sessionComposer).toHaveValue("锁定期间不可发送");
  });
});

describe("聊天热路径隔离、节流对账与步骤详情按需加载", () => {
  it("输入区高频击键不会触发已有历史消息行重渲染", async () => {
    const completedRun = makeRun({
      id: "run-typing-isolation",
      session_id: SESSION.id,
      status: "completed",
    });
    const historyMessage = makeMessage({
      id: "msg-history-stable",
      parts: [{ text: "稳定的历史回答", type: "text" }],
      role: "assistant",
      sequence: 1,
      session_id: SESSION.id,
    });
    installWorkspaceFetch({
      getRuns: () => [completedRun],
      handleWrite: async () => {
        throw new Error("unexpected write");
      },
      messages: () => ({
        items: [historyMessage],
        next_before_sequence: null,
      }),
    });
    const user = userEvent.setup();
    renderApp(`/chat/${SESSION.id}`);

    expect(await screen.findByText("稳定的历史回答")).toBeInTheDocument();
    const composer = screen.getByLabelText("对话草稿");
    const formatSpy = vi.spyOn(formatLib, "formatDateTime");
    formatSpy.mockClear();

    await user.type(composer, "连续输入十个字符验证热路径隔离");

    expect(composer).toHaveValue("连续输入十个字符验证热路径隔离");
    // SessionComposer 独立承载草稿输入态，击键期间 SessionWorkbench 与 ChatMessageList 不应重渲染。
    expect(formatSpy).not.toHaveBeenCalled();
  });

  it("完成态默认收起为单行摘要且不提前请求单步详情，点击具体步骤后才懒加载", async () => {
    window.innerWidth = 800;
    const completedRun = makeRun({
      id: "run-trace-lazy",
      session_id: SESSION.id,
      status: "completed",
    });
    const toolCall = makeToolCall({
      id: "call-lazy-1",
      model_name: "lazy_search_tool",
      ordinal: 1,
      tool_call_id: "model-call-lazy-1",
    });
    const fetchMock = installWorkspaceFetch({
      getRuns: () => [completedRun],
      handleWrite: async () => {
        throw new Error("unexpected write");
      },
      messages: () => ({ items: [], next_before_sequence: null }),
      toolCallDetail: () =>
        makeToolCallDetail({
          id: toolCall.id,
          model_name: toolCall.model_name,
          ordinal: 1,
          tool_call_id: toolCall.tool_call_id,
        }),
      toolCalls: () => [toolCall],
    });
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);
    expect(
      await screen.findByRole("heading", { name: "真实前端会话" }),
    ).toBeInTheDocument();
    const progress = await screen.findByRole("region", { name: "执行进度" });
    expect(
      await within(progress).findByRole("button", {
        name: /已完成 1 个步骤.*lazy_search_tool/,
      }),
    ).toBeInTheDocument();

    const countDetailRequests = () =>
      fetchMock.mock.calls.filter(([input]) =>
        new URL(getRequestUrl(input), "http://localhost").pathname.endsWith(
          "/tool-calls/call-lazy-1",
        ),
      ).length;

    expect(countDetailRequests()).toBe(0);

    await user.click(
      within(progress).getByRole("button", { name: /已完成 1 个步骤/ }),
    );
    expect(countDetailRequests()).toBe(0);

    const stepList = within(progress).getByRole("list", {
      name: "工具调用步骤",
    });
    await user.click(
      within(stepList).getByRole("button", { name: /lazy_search_tool/ }),
    );
    expect(await within(progress).findByText("真实参数")).toBeInTheDocument();
    expect(countDetailRequests()).toBe(1);
  });

  it("高频连续纯进度 SSE run_event 在 500ms 窗口内合并为一次对账且不盲目重拉消息历史，并阻止旧快照回退缓存", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const running = makeRun({
      event_sequence: 5,
      id: "run-throttle-monotonic",
      session_id: SESSION.id,
      status: "running",
    });
    const fetchMock = installWorkspaceFetch({
      getRuns: () => [running],
      handleWrite: async () => {
        throw new Error("unexpected write");
      },
      messages: () => ({ items: [], next_before_sequence: null }),
    });

    const view = renderApp(`/chat/${SESSION.id}`);
    expect(
      await screen.findByRole("button", { name: "运行中，暂不可发送" }),
    ).toBeDisabled();
    const source = FakeEventSource.instances[0];

    const countRunDetailRequests = () =>
      fetchMock.mock.calls.filter(([input]) => {
        const pathname = new URL(
          getRequestUrl(input),
          "http://localhost",
        ).pathname;
        return pathname === `/api/v1/sessions/${SESSION.id}/runs/${running.id}`;
      }).length;
    const countMessageRequests = () =>
      fetchMock.mock.calls.filter(([input]) => {
        const pathname = new URL(
          getRequestUrl(input),
          "http://localhost",
        ).pathname;
        return pathname === `/api/v1/sessions/${SESSION.id}/messages`;
      }).length;

    const baselineDetailCalls = countRunDetailRequests();
    const baselineMessageCalls = countMessageRequests();

    // 同一 500ms 窗口内连续到达 5 个纯中间进度事件，应合并为 1 次 Run/ToolCalls 失效且不刷新 messages。
    const progressTypes = [
      "started",
      "tool_call_started",
      "tool_call_finished",
      "compression_started",
      "subagent_status_changed",
    ] as const;
    act(() => {
      progressTypes.forEach((eventType, index) => {
        const seq = 6 + index;
        source.emit("run_event", {
          payload: {
            event: {
              created_at: `2026-11-10T03:00:0${index + 1}Z`,
              id: `event-burst-${seq}`,
              payload: {},
              run_id: running.id,
              sequence: seq,
              type: eventType,
            },
          },
        });
      });
    });

    await waitFor(
      () => {
        expect(countRunDetailRequests()).toBe(baselineDetailCalls + 1);
      },
      { timeout: 1_500 },
    );
    expect(countMessageRequests()).toBe(baselineMessageCalls);

    // 同一 500ms 窗口内先收到纯进度事件、后收到产生消息的事件（waiting_for_input），必须合并升级为刷新 messages。
    const detailCallsBeforeUpgrade = countRunDetailRequests();
    act(() => {
      source.emit("run_event", {
        payload: {
          event: {
            created_at: "2026-11-10T03:00:11Z",
            id: "event-upgrade-progress",
            payload: {},
            run_id: running.id,
            sequence: 11,
            type: "tool_call_started",
          },
        },
      });
      source.emit("run_event", {
        payload: {
          event: {
            created_at: "2026-11-10T03:00:12Z",
            id: "event-upgrade-message",
            payload: {
              input_request_id: "input-req-1",
              question_message_id: "msg-question-1",
            },
            run_id: running.id,
            sequence: 12,
            type: "waiting_for_input",
          },
        },
      });
    });

    await waitFor(
      () => {
        expect(countRunDetailRequests()).toBe(detailCallsBeforeUpgrade + 1);
        expect(countMessageRequests()).toBe(baselineMessageCalls + 1);
      },
      { timeout: 1_500 },
    );

    // 将缓存推进到 cancelling (#10)，随后注入低序号与旧状态的 snapshot，必须被单调性守卫拒绝。
    const cancellingRun = {
      ...running,
      event_sequence: 10,
      status: "cancelling" as const,
    };
    view.client.setQueryData(
      chatKeys.run(SESSION.id, running.id),
      cancellingRun,
    );
    view.client.setQueryData(chatKeys.latestRun(SESSION.id), {
      items: [cancellingRun],
      next_cursor: null,
    });

    act(() => {
      source.emit("snapshot", {
        payload: {
          event_sequence: 8,
          events: [],
          events_after_sequence: null,
          events_complete: true,
          messages: [],
          messages_before_sequence: null,
          messages_complete: true,
          report_message_id: null,
          status: "running",
        },
      });
    });

    const cachedRun = view.client.getQueryData<Run>(
      chatKeys.run(SESSION.id, running.id),
    );
    expect(cachedRun?.status).toBe("cancelling");
    expect(cachedRun?.event_sequence).toBe(10);
  });

  it("completed 终态对账优先复用 SSE 内存 completed 事件，跳过 /events 分页请求", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const running = makeRun({
      event_sequence: 2,
      id: "run-completed-reuse-memory",
      session_id: SESSION.id,
      status: "running",
    });
    const trigger = makeMessage({
      id: running.trigger_message_id,
      parts: [{ text: "帮我生成季度汇总", type: "text" }],
      role: "user",
      run_id: null,
      sequence: 1,
      session_id: SESSION.id,
    });
    const finalAnswer = makeMessage({
      id: "msg-final-reused",
      parts: [{ text: "季度汇总已完成。", type: "text" }],
      role: "assistant",
      run_id: running.id,
      sequence: 2,
      session_id: SESSION.id,
      status: "completed",
    });
    let currentRun: Run = running;
    let currentMessages = [trigger];
    const fetchMock = installWorkspaceFetch({
      getRuns: () => [currentRun],
      handleWrite: async () => {
        throw new Error("unexpected write");
      },
      messages: () => ({
        items: currentMessages,
        next_before_sequence: null,
      }),
      runEvents: () => {
        throw new Error("命中内存 completed 事件时不应发起 /events 分页查询");
      },
    });

    renderApp(`/chat/${SESSION.id}`);
    expect(
      await screen.findByRole("button", { name: "运行中，暂不可发送" }),
    ).toBeDisabled();
    const source = FakeEventSource.instances[0];

    currentRun = {
      ...running,
      event_sequence: 3,
      finished_at: "2026-11-10T03:00:05Z",
      status: "completed",
    };
    currentMessages = [finalAnswer, trigger];

    act(() => {
      source.emit("text_delta", { payload: { text: "季度汇总已完成。" } });
      source.emit("run_event", {
        payload: {
          event: {
            created_at: "2026-11-10T03:00:05Z",
            id: "event-completed-memory",
            payload: { assistant_message_id: finalAnswer.id },
            run_id: running.id,
            sequence: 3,
            type: "completed",
          },
        },
      });
    });

    expect(
      await screen.findByRole("article", {
        name: "Athena的消息，序号 2",
      }),
    ).toHaveTextContent("季度汇总已完成。");
    await waitFor(() => {
      expect(
        screen.queryByLabelText("Athena 的临时流式输出"),
      ).not.toBeInTheDocument();
    });
    expect(
      fetchMock.mock.calls.filter(([input]) =>
        new URL(getRequestUrl(input), "http://localhost").pathname.endsWith(
          "/events",
        ),
      ),
    ).toHaveLength(0);
  });

  it("运行中无产物 tool_call_finished 不重拉消息；但 live snapshot 出现未缓存交付消息 ID 时精准补拉消息与文件并渲染附件卡片", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const running = makeRun({
      event_sequence: 2,
      id: "run-live-delivery",
      session_id: SESSION.id,
      status: "running",
    });
    const trigger = makeMessage({
      id: running.trigger_message_id,
      parts: [{ text: "请执行财务技能导出报表并继续分析", type: "text" }],
      role: "user",
      run_id: null,
      sequence: 1,
      session_id: SESSION.id,
    });
    const deliveredFile = makeSessionFile({
      filename: "q4-budget.xlsx",
      id: "file-live-artifact-1",
      label: "Q4 预算导出表",
      mime_type:
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      session_id: SESSION.id,
      size_bytes: 8_192,
      source: "artifact",
    });
    const deliveryMessage = makeMessage({
      id: "msg-live-delivery-1",
      parts: [
        { text: "已生成以下交付文件。", type: "text" },
        {
          file_id: deliveredFile.id,
          filename: deliveredFile.filename,
          mime_type: deliveredFile.mime_type,
          size_bytes: deliveredFile.size_bytes,
          type: "file_ref",
        },
      ],
      role: "assistant",
      run_id: running.id,
      sequence: 2,
      session_id: SESSION.id,
      status: "completed",
    });

    let currentMessages = [trigger];
    let currentFiles: SessionFile[] = [];
    const fetchMock = installWorkspaceFetch({
      getFiles: () => currentFiles,
      getRuns: () => [running],
      handleWrite: async () => {
        throw new Error("unexpected write");
      },
      messages: () => ({
        items: currentMessages,
        next_before_sequence: null,
      }),
    });

    renderApp(`/chat/${SESSION.id}`);
    expect(
      await screen.findByRole("button", { name: "运行中，暂不可发送" }),
    ).toBeDisabled();
    expect(await screen.findByText("请执行财务技能导出报表并继续分析")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "会话文件（0）" }),
    ).toBeInTheDocument();

    const source = FakeEventSource.instances[0];
    const countRequests = (suffix: string) =>
      fetchMock.mock.calls.filter(([input]) =>
        new URL(getRequestUrl(input), "http://localhost").pathname.endsWith(
          suffix,
        ),
      ).length;

    const baselineRunCalls = countRequests(`/runs/${running.id}`);
    const baselineMessageCalls = countRequests("/messages");
    const baselineFileCalls = countRequests("/files");

    // 1. 普通无产物 tool_call_finished + 无新消息 ID 的 running snapshot：
    //    仅刷新 Run/ToolCalls，不触发 /messages 或 /files 重拉。
    act(() => {
      source.emit("run_event", {
        payload: {
          event: {
            created_at: "2026-11-10T03:00:02Z",
            id: "event-plain-tool-finished",
            payload: { call_id: "call-1", ordinal: 1, status: "succeeded" },
            run_id: running.id,
            sequence: 3,
            type: "tool_call_finished",
          },
        },
      });
      source.emit("snapshot", {
        payload: {
          event_sequence: 3,
          events: [],
          events_after_sequence: null,
          events_complete: true,
          messages: [],
          messages_before_sequence: null,
          messages_complete: true,
          report_message_id: null,
          status: "running",
        },
      });
    });

    await waitFor(
      () => {
        expect(countRequests(`/runs/${running.id}`)).toBe(baselineRunCalls + 1);
      },
      { timeout: 1_500 },
    );
    expect(countRequests("/messages")).toBe(baselineMessageCalls);
    expect(countRequests("/files")).toBe(baselineFileCalls);

    // 2. Skill 脚本完成产出交付文件，后端在 running 态推送含新消息 ID（纯文本投影）的 snapshot：
    //    精准触发 /messages 与 /files 补拉，运行期间立即渲染 FileRefPart 附件卡片并同步文件计数。
    currentMessages = [deliveryMessage, trigger];
    currentFiles = [deliveredFile];
    act(() => {
      source.emit("run_event", {
        payload: {
          event: {
            created_at: "2026-11-10T03:00:04Z",
            id: "event-skill-tool-finished",
            payload: { call_id: "call-2", ordinal: 2, status: "succeeded" },
            run_id: running.id,
            sequence: 4,
            type: "tool_call_finished",
          },
        },
      });
      source.emit("snapshot", {
        payload: {
          event_sequence: 4,
          events: [],
          events_after_sequence: null,
          events_complete: true,
          messages: [
            {
              created_at: deliveryMessage.created_at,
              id: deliveryMessage.id,
              role: "assistant",
              sequence: deliveryMessage.sequence,
              status: "completed",
              text: "已生成以下交付文件。",
            },
          ],
          messages_before_sequence: null,
          messages_complete: true,
          report_message_id: null,
          status: "running",
        },
      });
    });

    const deliveryArticle = await screen.findByRole("article", {
      name: "Athena的消息，序号 2",
    });
    expect(deliveryArticle).toHaveTextContent("已生成以下交付文件。");
    expect(
      await within(deliveryArticle).findByRole("button", {
        name: "下载文件：q4-budget.xlsx",
      }),
    ).toBeInTheDocument();
    expect(
      within(deliveryArticle).getByRole("button", {
        name: "预览文件：q4-budget.xlsx",
      }),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: "会话文件（1）" }),
    ).toBeInTheDocument();
    expect(countRequests("/messages")).toBe(baselineMessageCalls + 1);
    expect(countRequests("/files")).toBe(baselineFileCalls + 1);

    // 3. 同一交付消息 ID 的后续周期 running snapshot 不得重复触发 /messages 或 /files 失效。
    act(() => {
      source.emit("snapshot", {
        payload: {
          event_sequence: 5,
          events: [],
          events_after_sequence: null,
          events_complete: true,
          messages: [
            {
              created_at: deliveryMessage.created_at,
              id: deliveryMessage.id,
              role: "assistant",
              sequence: deliveryMessage.sequence,
              status: "completed",
              text: "已生成以下交付文件。",
            },
          ],
          messages_before_sequence: null,
          messages_complete: true,
          report_message_id: null,
          status: "running",
        },
      });
    });

    // 等待一个短事件循环后确认没有发起任何新的 /messages 或 /files 请求。
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(countRequests("/messages")).toBe(baselineMessageCalls + 1);
    expect(countRequests("/files")).toBe(baselineFileCalls + 1);
  });
});

describe("会话文件抽屉与顶层预览弹窗互斥层叠与焦点归还", () => {
  it("抽屉点击预览后抽屉主动让位为单模态层，立即按 Escape 关预览恢复抽屉并归还焦点，再按 Escape 关抽屉", async () => {
    const reportFile = makeSessionFile({
      filename: "review-report.md",
      id: "file-drawer-report",
      label: "复盘报告",
      mime_type: "text/markdown",
      session_id: SESSION.id,
      size_bytes: 1_024,
      source: "artifact",
    });
    const notesFile = makeSessionFile({
      filename: "meeting-notes.txt",
      id: "file-drawer-notes",
      mime_type: "text/plain",
      session_id: SESSION.id,
      size_bytes: 512,
      source: "upload",
    });
    const slowContent = deferred<Response>();
    let previewRequestAborted = false;

    const fetchMock = installWorkspaceFetch({
      fileContent: (fileId, signal) => {
        if (fileId === notesFile.id) {
          signal?.addEventListener("abort", () => {
            previewRequestAborted = true;
          });
          return slowContent.promise;
        }
        return jsonResponse({
          content: "# 复盘报告\n所有关键指标已核对。",
          eof: true,
          file: reportFile,
          next_offset: 32,
          source_kind: "raw_text",
          truncated: false,
        });
      },
      files: [reportFile, notesFile],
      getRuns: () => [],
      handleWrite: async () => {
        throw new Error("unexpected write");
      },
      messages: () => ({
        items: [],
        next_before_sequence: null,
      }),
    });
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);

    // 1. 打开右侧「会话文件」抽屉，验证列表渲染且页面仅存 1 个 dialog
    await user.click(await screen.findByRole("button", { name: "会话文件（2）" }));
    const drawer = await screen.findByRole("dialog", { name: /会话文件/ });
    expect(screen.getAllByRole("dialog")).toHaveLength(1);

    const countFilesRequests = () =>
      fetchMock.mock.calls.filter(([input]) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        return url.pathname === `/api/v1/sessions/${SESSION.id}/files`;
      }).length;
    const filesRequestsBeforePreview = countFilesRequests();

    // 2. 点击第二个文件（notes.txt）的「预览」：抽屉主动让位卸载，页面仅存在 1 个顶层 file-preview-dialog
    await user.click(
      within(drawer).getByRole("button", {
        name: "预览文件：meeting-notes.txt",
      }),
    );
    const previewDialog = await screen.findByTestId("file-preview-dialog");
    expect(previewDialog).toBeInTheDocument();
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    expect(
      screen.queryByRole("dialog", { name: /会话文件/ }),
    ).not.toBeInTheDocument();

    // 3. 在预览内容尚未返回时立即按 Escape：必须关闭顶层预览灯箱并恢复抽屉，且精准回焦至原文件预览按钮
    await user.keyboard("{Escape}");

    await waitFor(() => {
      expect(
        screen.queryByTestId("file-preview-dialog"),
      ).not.toBeInTheDocument();
    });
    expect(previewRequestAborted).toBe(true);

    const restoredDrawer = await screen.findByRole("dialog", {
      name: /会话文件/,
    });
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    // 抽屉恢复依赖 TanStack Query 缓存命中，不触发额外的会话文件列表请求
    expect(countFilesRequests()).toBe(filesRequestsBeforePreview);
    const restoredNotesPreviewBtn = within(restoredDrawer).getByRole("button", {
      name: "预览文件：meeting-notes.txt",
    });
    await waitFor(() => {
      expect(restoredNotesPreviewBtn).toHaveFocus();
    });

    // 4. 再按一次 Escape：抽屉正常关闭，页面无残留 dialog
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", { name: /会话文件/ }),
      ).not.toBeInTheDocument();
    });
    expect(screen.queryAllByRole("dialog")).toHaveLength(0);

    // 5. 再次从顶栏正常打开抽屉：旧的回焦目标已被清理，不会误聚焦上次预览的文件按钮
    await user.click(screen.getByRole("button", { name: "会话文件（2）" }));
    const reopenedDrawer = await screen.findByRole("dialog", {
      name: /会话文件/,
    });
    await waitFor(() => {
      expect(reopenedDrawer.contains(document.activeElement)).toBe(true);
    });
    expect(
      within(reopenedDrawer).getByRole("button", {
        name: "预览文件：meeting-notes.txt",
      }),
    ).not.toHaveFocus();
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryAllByRole("dialog")).toHaveLength(0);
    });
  });

  it("从消息流与输入器直接打开 FilePreviewDialog 并按 Escape 关闭时，抽屉保持关闭且原回焦行为无回归", async () => {
    const artifactFile = makeSessionFile({
      filename: "summary.md",
      id: "file-msg-summary",
      label: "执行摘要",
      mime_type: "text/markdown",
      session_id: SESSION.id,
      size_bytes: 1_536,
      source: "artifact",
    });
    const uploadedMemo = makeSessionFile({
      filename: "memo.txt",
      id: "file-composer-memo",
      mime_type: "text/plain",
      session_id: SESSION.id,
      size_bytes: 256,
      source: "upload",
    });
    const completedRun = makeRun({
      id: "run-preview-focus-return",
      session_id: SESSION.id,
      status: "completed",
    });

    installWorkspaceFetch({
      fileContent: (fileId) => {
        if (fileId === artifactFile.id) {
          return jsonResponse({
            content: "# 执行摘要\n本季度目标全部达成。",
            eof: true,
            file: artifactFile,
            next_offset: 36,
            source_kind: "raw_text",
            truncated: false,
          });
        }
        return jsonResponse({
          content: "备忘录正文内容",
          eof: true,
          file: uploadedMemo,
          next_offset: 21,
          source_kind: "raw_text",
          truncated: false,
        });
      },
      files: [artifactFile, uploadedMemo],
      getRuns: () => [completedRun],
      handleWrite: async () => {
        throw new Error("unexpected write");
      },
      messages: () => ({
        items: [
          makeMessage({
            id: "msg-artifact-preview-return",
            parts: [
              {
                artifact_id: null,
                artifacts: [
                  makeArtifact({
                    file_id: artifactFile.id,
                    filename: artifactFile.filename,
                    label: artifactFile.label,
                    mime_type: artifactFile.mime_type,
                    size_bytes: artifactFile.size_bytes,
                  }),
                ],
                output: null,
                status: "success",
                tool_call_id: "tc-summary",
                type: "tool_result",
              },
            ],
            role: "assistant",
            run_id: completedRun.id,
            sequence: 1,
            session_id: SESSION.id,
          }),
        ],
        next_before_sequence: null,
      }),
    });
    const user = userEvent.setup();

    renderApp(`/chat/${SESSION.id}`);

    // 1. 消息流交付产物入口：打开预览 -> Escape 关闭 -> 抽屉保持关闭且焦点精准回到原按钮
    const messagePreviewBtn = await screen.findByRole("button", {
      name: "在线预览：执行摘要",
    });
    await user.click(messagePreviewBtn);
    const artifactPreviewDialog = await screen.findByTestId(
      "file-preview-dialog",
    );
    expect(
      await within(artifactPreviewDialog).findByText("本季度目标全部达成。"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("dialog", { name: /会话文件/ }),
    ).not.toBeInTheDocument();

    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(
        screen.queryByTestId("file-preview-dialog"),
      ).not.toBeInTheDocument();
    });
    expect(
      screen.queryByRole("dialog", { name: /会话文件/ }),
    ).not.toBeInTheDocument();
    await waitFor(() => {
      expect(messagePreviewBtn).toHaveFocus();
    });

    // 2. 输入器附件芯片入口：勾选已有文件后点击芯片预览 -> Escape 关闭 -> 抽屉保持关闭且焦点回到原芯片按钮
    await user.click(screen.getByRole("button", { name: "添加附件" }));
    const attachPopover = screen.getByRole("dialog", {
      name: "引用本会话已有文件",
    });
    await user.click(
      within(attachPopover).getByRole("checkbox", { name: "memo.txt" }),
    );
    await user.keyboard("{Escape}");

    const composerPreviewBtn = await screen.findByRole("button", {
      name: "预览附件：memo.txt",
    });
    await user.click(composerPreviewBtn);
    const memoPreviewDialog = await screen.findByTestId("file-preview-dialog");
    expect(
      await within(memoPreviewDialog).findByText("备忘录正文内容"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("dialog", { name: /会话文件/ }),
    ).not.toBeInTheDocument();

    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(
        screen.queryByTestId("file-preview-dialog"),
      ).not.toBeInTheDocument();
    });
    expect(
      screen.queryByRole("dialog", { name: /会话文件/ }),
    ).not.toBeInTheDocument();
    await waitFor(() => {
      expect(composerPreviewBtn).toHaveFocus();
    });
  });
});

