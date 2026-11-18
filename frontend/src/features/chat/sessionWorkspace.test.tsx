import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  getRequestUrl,
  listResponse,
  makeMessage,
  makeRun,
  makeSession,
  makeToolCall,
  makeToolCallDetail,
  PROJECT_A,
  TEST_USER,
} from "../../test/fixtures";
import {
  FakeEventSource,
} from "../../test/fakeEventSource";
import { jsonResponse, renderApp } from "../../test/testUtils";
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
          items: [],
          next_after_file_id: null,
        });
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
    expect(await screen.findByText(/web_search/)).toBeInTheDocument();
    expect(screen.getByText("运行中")).toBeInTheDocument();
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
      within(approval).queryByText("服务端当前没有返回待审批项。"),
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

    expect(await screen.findByText("运行中")).toBeInTheDocument();
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
      screen.getByLabelText("Athena 的临时流式输出"),
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
      expect(screen.getByText("已完成")).toBeInTheDocument();
    });

    const summary = document.querySelector<HTMLElement>(".run-summary");
    expect(summary).not.toBeNull();
    expect(within(summary as HTMLElement).getByText("已完成")).toBeInTheDocument();
    expect(
      within(summary as HTMLElement).queryByText("运行中"),
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

    expect(await screen.findByText("运行中")).toBeInTheDocument();
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
      await screen.findByText(
        "运行已完成，但最终消息同步失败。请重试加载最终回答。",
      ),
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

    expect(await screen.findByText("运行中")).toBeInTheDocument();
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
      await screen.findByText(/未找到完成事件所指的最终消息/),
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

    expect(await screen.findByText("运行中")).toBeInTheDocument();
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
      await screen.findByText(/未找到完成事件所指的最终消息/),
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
    ["failed", "失败", "运行失败"],
    ["cancelled", "已取消", "运行已取消"],
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

      expect(await screen.findByText("运行中")).toBeInTheDocument();
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

    expect(await screen.findByText("运行中")).toBeInTheDocument();
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

    expect(await screen.findByText("失败")).toBeInTheDocument();
    expect(
      await screen.findByRole("article", {
        name: "Athena的消息，序号 5",
      }),
    ).toHaveTextContent("刷新后仍可见的失败报告");
    expect(
      screen.queryByText(
        "运行失败，但未找到失败事件所指的权威报告。清空状态前请重试对账。",
      ),
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

    expect(await screen.findByText("运行中")).toBeInTheDocument();
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
        "运行失败，但未找到失败事件所指的权威报告。清空状态前请重试对账。",
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

    expect(await screen.findByText("运行中")).toBeInTheDocument();
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
    expect(await screen.findByText("正在取消")).toBeInTheDocument();
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
    expect(await screen.findByText("已取消")).toBeInTheDocument();
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

    await screen.findByText("运行中");
    await user.click(screen.getByRole("button", { name: "停止任务" }));
    expect(await screen.findByText("正在取消")).toBeInTheDocument();
    expect(screen.queryByText("已取消")).not.toBeInTheDocument();

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
    expect(await screen.findByText("运行中")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "运行中，暂不可发送" }),
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

    expect(await screen.findByText("排队中")).toBeInTheDocument();
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
    expect(await screen.findByText("已完成")).toBeInTheDocument();

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
    expect(screen.getByText("已完成")).toBeInTheDocument();
    expect(screen.queryByText("排队中")).not.toBeInTheDocument();
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
    expect(await screen.findByText("排队中")).toBeInTheDocument();
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
    expect(await screen.findByText("已完成")).toBeInTheDocument();

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
    expect(screen.getByText("已完成")).toBeInTheDocument();
    expect(screen.queryByText("排队中")).not.toBeInTheDocument();
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
    expect(await screen.findByText("排队中")).toBeInTheDocument();
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
    expect(await screen.findByText("运行中")).toBeInTheDocument();
    expect(answerInput).toHaveValue("不能丢的补充回答");
    expect(
      screen.getByRole("button", { name: "运行中，暂不可发送" }),
    ).toBeDisabled();
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
    expect(screen.getByText(/#1 write_crm/)).toBeInTheDocument();
    expect(screen.getByText(/#2 send_email/)).toBeInTheDocument();

    await user.click(
      screen.getAllByRole("button", { name: "查看参数" })[0],
    );
    expect(await screen.findByText(/"query": "真实参数"/)).toBeInTheDocument();

    await user.click(screen.getAllByRole("button", { name: "批准" })[0]);
    await waitFor(() => expect(decisions).toHaveLength(1));
    expect(decisions[0].decision).toBe("approve");

    await user.click(await screen.findByRole("button", { name: "拒绝" }));
    await waitFor(() => expect(decisions).toHaveLength(2));
    expect(decisions[1].decision).toBe("reject");
    expect(await screen.findByText("排队中")).toBeInTheDocument();
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
    await user.click(screen.getAllByRole("button", { name: "批准" })[0]);
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

    expect(await screen.findByText(/#2 send_email/)).toBeInTheDocument();
    expect(screen.queryByText(/#1 write_crm/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "批准" }));
    await waitFor(() => expect(decisions).toHaveLength(2));
    expect(decisions).toEqual([
      "call-serial-1:approve",
      "call-serial-2:approve",
    ]);
    expect(await screen.findByText("排队中")).toBeInTheDocument();
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
    await user.click(screen.getByRole("button", { name: "批准" }));
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
    expect(await screen.findByText("正在取消")).toBeInTheDocument();

    await act(async () => {
      approvalGate.resolve(jsonResponse(waiting));
      await approvalGate.promise;
    });

    await waitFor(() => {
      expect(screen.getByText("正在取消")).toBeInTheDocument();
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
    expect(await screen.findByText("已取消")).toBeInTheDocument();
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

    await screen.findByText("失败");
    await user.click(
      screen.getByRole("button", { name: "重试失败任务" }),
    );
    const dialog = screen.getByRole("dialog", { name: "重试失败任务" });
    expect(
      within(dialog).getByText(/整个任务将从头执行/),
    ).toBeInTheDocument();
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
      within(dialog).getByRole("button", { name: "确认从头重试" }),
    );
    expect(
      await within(dialog).findByText(
        "无法连接服务器，请检查网络后重试。",
      ),
    ).toBeInTheDocument();
    await user.click(
      within(dialog).getByRole("button", { name: "确认从头重试" }),
    );

    expect(await screen.findByText("排队中")).toBeInTheDocument();
    expect(
      screen.queryByRole("dialog", { name: "重试失败任务" }),
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

    await screen.findByText("失败");
    await user.click(
      screen.getByRole("button", { name: "重试失败任务" }),
    );
    const dialog = screen.getByRole("dialog", { name: "重试失败任务" });
    expect(
      within(dialog).getByText(/整个任务将从头执行/),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("note", { name: "服务端重试警告" }),
    ).toHaveTextContent(serverWarning);

    await user.click(
      within(dialog).getByRole("button", { name: "确认从头重试" }),
    );
    expect(await screen.findByText("排队中")).toBeInTheDocument();
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

    await screen.findByText("失败");
    await user.click(
      screen.getByRole("button", { name: "重试失败任务" }),
    );
    const dialog = screen.getByRole("dialog", { name: "重试失败任务" });
    expect(within(dialog).getAllByText(fixedWarning)).toHaveLength(1);
    expect(
      within(dialog).getByRole("note", { name: "服务端重试警告" }),
    ).toHaveTextContent(fixedWarning);
  });
});

describe("窄屏执行轨迹与输入区", () => {
  it.each([920, 390])(
    "%ipx 可展开/收起真实工具轨迹并继续编辑输入",
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

      await user.click(
        screen.getByRole("button", { name: "查看执行轨迹" }),
      );
      expect(
        screen.getByRole("complementary", { name: "执行轨迹" }),
      ).toBeInTheDocument();
      await user.click(
        screen.getByRole("button", { name: /#1 web_search/ }),
      );
      expect(await screen.findByText(/"query": "真实参数"/)).toBeInTheDocument();
      await user.click(
        screen.getByRole("button", { name: "收起执行轨迹" }),
      );
      expect(
        screen.queryByRole("complementary", { name: "执行轨迹" }),
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
    expect(await screen.findByText("排队中")).toBeInTheDocument();
    expect(sessionComposer).toHaveValue("");
    expect(sessionCreateCount).toBe(1);
    expect(runRequestIds).toHaveLength(2);
    expect(runRequestIds[0]).toBe(runRequestIds[1]);
  });

  it("草稿页与会话页均支持 Ctrl/Meta+Enter 快捷发送，且忽略 IME 组合态与锁定态", async () => {
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
    // IME 组合输入态下按 Ctrl+Enter 不应触发提交
    fireEvent.keyDown(draftComposer, {
      ctrlKey: true,
      isComposing: true,
      key: "Enter",
    });
    expect(submittedTexts).toHaveLength(0);

    // 正常 Ctrl+Enter 触发草稿页创建会话并提交 Run
    fireEvent.keyDown(draftComposer, {
      ctrlKey: true,
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
    await user.type(sessionComposer, "会话页 Meta+Enter 发送");

    // 会话页 Meta+Enter 触发提交
    fireEvent.keyDown(sessionComposer, {
      key: "Enter",
      metaKey: true,
    });

    await waitFor(() => {
      expect(submittedTexts).toEqual([
        "草稿快捷键发送",
        "会话页 Meta+Enter 发送",
      ]);
    });
    expect(await screen.findByText("运行中")).toBeInTheDocument();
    expect(sessionComposer).toHaveValue("");

    // 运行锁定态下输入新内容并按 Ctrl+Enter 不应重复提交
    await user.type(sessionComposer, "锁定期间不可发送");
    fireEvent.keyDown(sessionComposer, {
      ctrlKey: true,
      key: "Enter",
    });
    expect(submittedTexts).toHaveLength(2);
    expect(sessionComposer).toHaveValue("锁定期间不可发送");
  });
});

