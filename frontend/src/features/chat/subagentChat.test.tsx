import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  getRequestUrl,
  listResponse,
  makeSession,
  PROJECT_A,
  TEST_USER,
} from "../../test/fixtures";
import { jsonResponse, renderApp } from "../../test/testUtils";
import type { Run } from "./model";

function makeRun(overrides: Partial<Run> = {}): Run {
  return {
    id: "run-parent-1",
    session_id: "session-1",
    status: "waiting_for_subagents",
    trigger_message_id: "msg-user-1",
    context_cutoff_sequence: 1,
    retry_of_run_id: null,
    model_config_version_id: "model-1",
    prompt_template_version: "v1",
    event_sequence: 3,
    execution_epoch: 1,
    queued_at: "2026-11-16T00:00:00Z",
    first_started_at: "2026-11-16T00:00:01Z",
    finished_at: null,
    created_at: "2026-11-16T00:00:00Z",
    updated_at: "2026-11-16T00:00:02Z",
    cancel_requested_at: null,
    safe_error_code: null,
    report_message_id: null,
    usage: {
      input_tokens: 100,
      output_tokens: 40,
      total_tokens: 140,
      complete: false,
      attempts: [],
      answer_tokens: null,
      compression_tokens: null,
    },
    pending_input: null,
    pending_approvals: [],
    skill_snapshot: null,
    input_file_ids: [],
    subagent_tasks: [],
    pending_subagent_inputs: [],
    pending_subagent_approvals: [],
    replayed: false,
    retry_warning: null,
    ...overrides,
  };
}

function stubNoopEventSource() {
  class MockEventSource {
    onopen: ((event: Event) => void) | null = null;
    onerror: ((event: Event) => void) | null = null;
    addEventListener() {}
    close() {}
  }
  vi.stubGlobal("EventSource", MockEventSource);
}

describe("Chat 子智能体与并发 HITL 交互", () => {
  it("展示子任务进度面板并按需加载子 Run 执行轨迹", async () => {
    stubNoopEventSource();
    const session = makeSession({
      id: "session-1",
      project_id: PROJECT_A.id,
      title: "多智能体调研",
    });
    const parentRun = makeRun({
      subagent_tasks: [
        {
          sub_run_id: "sub-run-1",
          parent_tool_call_id: "call-1",
          subagent_profile_id: "prof-1",
          subagent_slug: "research_assistant",
          subagent_name: "调研助手",
          task_index: 1,
          task_summary: "检索 2024 年行业报告",
          status: "completed",
          safe_error_code: null,
          created_at: "2026-11-16T00:00:01Z",
          first_started_at: "2026-11-16T00:00:02Z",
          finished_at: "2026-11-16T00:00:05Z",
        },
        {
          sub_run_id: "sub-run-2",
          parent_tool_call_id: "call-2",
          subagent_profile_id: "prof-2",
          subagent_slug: "code_helper",
          subagent_name: "代码助手",
          task_index: 2,
          task_summary: "检查仓库构建脚本",
          status: "failed",
          safe_error_code: "execution_timeout",
          created_at: "2026-11-16T00:00:01Z",
          first_started_at: "2026-11-16T00:00:02Z",
          finished_at: "2026-11-16T00:00:10Z",
        },
      ],
    });
    const subRun1 = makeRun({
      id: "sub-run-1",
      status: "completed",
      event_sequence: 5,
      execution_epoch: 1,
    });

    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/projects") {
          return jsonResponse({ items: [PROJECT_A] });
        }
        if (url.pathname === "/api/v1/sessions") {
          return jsonResponse(listResponse([session], 1, 20, 1));
        }
        if (url.pathname === "/api/v1/sessions/session-1") {
          return jsonResponse(session);
        }
        if (url.pathname === "/api/v1/sessions/session-1/messages") {
          return jsonResponse({
            items: [
              {
                id: "msg-user-1",
                session_id: "session-1",
                role: "user",
                status: "completed",
                sequence: 1,
                parts: [{ type: "text", text: "请同时分析 2024 和 2025 年财报" }],
                reply_to_message_id: null,
                run_id: null,
                created_at: "2026-11-16T00:00:00Z",
                completed_at: "2026-11-16T00:00:00Z",
              },
            ],
            next_before_sequence: null,
          });
        }
        if (url.pathname === "/api/v1/sessions/session-1/files") {
          return jsonResponse({ items: [], next_cursor: null });
        }
        if (url.pathname === "/api/v1/skills") {
          return jsonResponse({ items: [] });
        }
        if (url.pathname === "/api/v1/sessions/session-1/runs") {
          return jsonResponse({ items: [parentRun], next_cursor: null });
        }
        if (url.pathname === "/api/v1/sessions/session-1/runs/run-parent-1") {
          return jsonResponse(parentRun);
        }
        if (
          url.pathname ===
          "/api/v1/sessions/session-1/runs/run-parent-1/tool-calls"
        ) {
          return jsonResponse({ items: [], next_after_ordinal: null });
        }
        if (url.pathname === "/api/v1/sessions/session-1/runs/sub-run-1") {
          return jsonResponse(subRun1);
        }
        if (
          url.pathname ===
          "/api/v1/sessions/session-1/runs/sub-run-1/tool-calls"
        ) {
          return jsonResponse({
            items: [
              {
                id: "tc-1",
                tool_call_id: "call-k1",
                ordinal: 1,
                tool_id: "builtin_knowledge_search",
                tool_kind: "builtin",
                model_name: "knowledge_search",
                status: "completed",
                started_at: "2026-11-16T00:00:02Z",
                finished_at: "2026-11-16T00:00:03Z",
                duration_ms: 420,
                safe_error_code: null,
                truncated: false,
              },
            ],
            next_after_ordinal: null,
          });
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/chat/session-1");

    expect(
      await screen.findByText("协作任务（2）"),
    ).toBeInTheDocument();
    expect(screen.getByText("调研助手")).toBeInTheDocument();
    expect(screen.getByText("代码助手")).toBeInTheDocument();
    // 隐藏底层 slug / run_id / 裸错误码，向员工展示清晰中文原因
    expect(screen.queryByText("research_assistant")).not.toBeInTheDocument();
    expect(screen.queryByText("sub-run-1")).not.toBeInTheDocument();
    expect(
      screen.getByText("任务处理耗时超出上限，您可以稍后重新执行。"),
    ).toBeInTheDocument();

    const expandButtons = screen.getAllByRole("button", { name: "查看详情" });
    await user.click(expandButtons[0]);

    expect(
      await screen.findByText("查阅企业知识库"),
    ).toBeInTheDocument();
    expect(screen.getByText("420 ms")).toBeInTheDocument();
  });

  it("多子 Run 并发提问队列支持切换目标并精确路由提交", async () => {
    stubNoopEventSource();
    const session = makeSession({
      id: "session-1",
      project_id: PROJECT_A.id,
      title: "并发提问测试",
    });
    let parentRun = makeRun({
      pending_subagent_inputs: [
        {
          sub_run_id: "sub-run-1",
          subagent_slug: "research_assistant",
          subagent_name: "调研助手",
          task_index: 1,
          task_summary: "检索 2024 年行业报告",
          input_request_id: "req-1",
          question_message_id: "q-1",
          question_text: "请确认调研的时间范围？",
          created_at: "2026-11-16T00:00:03Z",
        },
        {
          sub_run_id: "sub-run-2",
          subagent_slug: "code_helper",
          subagent_name: "代码助手",
          task_index: 2,
          task_summary: "检查仓库构建脚本",
          input_request_id: "req-2",
          question_message_id: "q-2",
          question_text: "请确认目标仓库分支名称？",
          created_at: "2026-11-16T00:00:04Z",
        },
      ],
    });

    const submittedAnswerPaths: string[] = [];

    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/projects") {
          return jsonResponse({ items: [PROJECT_A] });
        }
        if (url.pathname === "/api/v1/sessions") {
          return jsonResponse(listResponse([session], 1, 20, 1));
        }
        if (url.pathname === "/api/v1/sessions/session-1") {
          return jsonResponse(session);
        }
        if (url.pathname === "/api/v1/sessions/session-1/messages") {
          return jsonResponse({
            items: [
              {
                id: "msg-user-1",
                session_id: "session-1",
                role: "user",
                status: "completed",
                sequence: 1,
                parts: [{ type: "text", text: "请同时分析 2024 和 2025 年财报" }],
                reply_to_message_id: null,
                run_id: null,
                created_at: "2026-11-16T00:00:00Z",
                completed_at: "2026-11-16T00:00:00Z",
              },
            ],
            next_before_sequence: null,
          });
        }
        if (url.pathname === "/api/v1/sessions/session-1/files") {
          return jsonResponse({ items: [], next_cursor: null });
        }
        if (url.pathname === "/api/v1/skills") {
          return jsonResponse({ items: [] });
        }
        if (url.pathname === "/api/v1/sessions/session-1/runs") {
          return jsonResponse({ items: [parentRun], next_cursor: null });
        }
        if (url.pathname === "/api/v1/sessions/session-1/runs/run-parent-1") {
          return jsonResponse(parentRun);
        }
        if (
          url.pathname ===
          "/api/v1/sessions/session-1/runs/run-parent-1/tool-calls"
        ) {
          return jsonResponse({ items: [], next_after_ordinal: null });
        }
        if (
          url.pathname ===
            "/api/v1/sessions/session-1/runs/sub-run-2/input-requests/req-2/answer" &&
          method === "POST"
        ) {
          submittedAnswerPaths.push(url.pathname);
          parentRun = {
            ...parentRun,
            pending_subagent_inputs: parentRun.pending_subagent_inputs?.filter(
              (item) => item.input_request_id !== "req-2",
            ),
          };
          return jsonResponse(
            makeRun({ id: "sub-run-2", status: "queued" }),
          );
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/chat/session-1");

    expect(await screen.findByText("2 项待回复")).toBeInTheDocument();
    // 默认选中队首
    expect(
      screen.getByText("当前回复：#1「调研助手」"),
    ).toBeInTheDocument();

    // 切换到第二个子智能体
    await user.click(screen.getByRole("button", { name: /代码助手/ }));
    expect(
      screen.getByText("当前回复：#2「代码助手」"),
    ).toBeInTheDocument();

    await user.type(
      screen.getByRole("textbox", {
        name: "正在回答子智能体「代码助手」的提问",
      }),
      "main 分支",
    );
    await user.click(
      screen.getByRole("button", { name: "提交给「代码助手」" }),
    );

    await waitFor(() => {
      expect(submittedAnswerPaths).toEqual([
        "/api/v1/sessions/session-1/runs/sub-run-2/input-requests/req-2/answer",
      ]);
    });

    // 提交后自动回落到剩余的调研助手提问
    expect(
      await screen.findByText("当前回复：#1「调研助手」"),
    ).toBeInTheDocument();
    expect(screen.queryByText("2 项待回复")).not.toBeInTheDocument();
  });

  it("按 sub_run_id 分组展示工具审批，提交互不阻塞", async () => {
    stubNoopEventSource();
    const session = makeSession({
      id: "session-1",
      project_id: PROJECT_A.id,
      title: "子智能体审批测试",
    });
    let parentRun = makeRun({
      pending_subagent_approvals: [
        {
          sub_run_id: "sub-run-1",
          subagent_slug: "research_assistant",
          subagent_name: "调研助手",
          task_index: 1,
          task_summary: "检索 2024 年行业报告",
          call_id: "call-a",
          batch_message_id: "msg-a",
          ordinal: 1,
          model_name: "mcp_search_docs",
          tool_kind: "mcp",
          requested_at: "2026-11-16T00:00:03Z",
          safe_skill_projection: null,
        },
        {
          sub_run_id: "sub-run-2",
          subagent_slug: "code_helper",
          subagent_name: "代码助手",
          task_index: 2,
          task_summary: "检查仓库构建脚本",
          call_id: "call-b",
          batch_message_id: "msg-b",
          ordinal: 1,
          model_name: "mcp_write_file",
          tool_kind: "mcp",
          requested_at: "2026-11-16T00:00:04Z",
          safe_skill_projection: null,
        },
      ],
    });

    const approvedCalls: string[] = [];

    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/projects") {
          return jsonResponse({ items: [PROJECT_A] });
        }
        if (url.pathname === "/api/v1/sessions") {
          return jsonResponse(listResponse([session], 1, 20, 1));
        }
        if (url.pathname === "/api/v1/sessions/session-1") {
          return jsonResponse(session);
        }
        if (url.pathname === "/api/v1/sessions/session-1/messages") {
          return jsonResponse({
            items: [
              {
                id: "msg-user-1",
                session_id: "session-1",
                role: "user",
                status: "completed",
                sequence: 1,
                parts: [{ type: "text", text: "请同时分析 2024 和 2025 年财报" }],
                reply_to_message_id: null,
                run_id: null,
                created_at: "2026-11-16T00:00:00Z",
                completed_at: "2026-11-16T00:00:00Z",
              },
            ],
            next_before_sequence: null,
          });
        }
        if (url.pathname === "/api/v1/sessions/session-1/files") {
          return jsonResponse({ items: [], next_cursor: null });
        }
        if (url.pathname === "/api/v1/skills") {
          return jsonResponse({ items: [] });
        }
        if (url.pathname === "/api/v1/sessions/session-1/runs") {
          return jsonResponse({ items: [parentRun], next_cursor: null });
        }
        if (url.pathname === "/api/v1/sessions/session-1/runs/run-parent-1") {
          return jsonResponse(parentRun);
        }
        if (
          url.pathname ===
          "/api/v1/sessions/session-1/runs/run-parent-1/tool-calls"
        ) {
          return jsonResponse({ items: [], next_after_ordinal: null });
        }
        if (
          url.pathname ===
            "/api/v1/sessions/session-1/runs/sub-run-2/tool-calls/call-b/approval" &&
          method === "POST"
        ) {
          approvedCalls.push(url.pathname);
          parentRun = {
            ...parentRun,
            pending_subagent_approvals:
              parentRun.pending_subagent_approvals?.filter(
                (item) => item.call_id !== "call-b",
              ),
          };
          return jsonResponse(
            makeRun({ id: "sub-run-2", status: "queued" }),
          );
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/chat/session-1");

    const approvalPanel = await screen.findByRole("region", {
      name: "协作任务操作审批（2）",
    });
    expect(within(approvalPanel).getByText("来自：调研助手")).toBeInTheDocument();
    expect(within(approvalPanel).getByText("来自：代码助手")).toBeInTheDocument();

    const approveButtons = within(approvalPanel).getAllByRole("button", {
      name: "确认执行",
    });
    await user.click(approveButtons[1]);

    await waitFor(() => {
      expect(approvedCalls).toEqual([
        "/api/v1/sessions/session-1/runs/sub-run-2/tool-calls/call-b/approval",
      ]);
    });
  });

  it("waiting_for_subagents 状态锁定普通发送并支持停止取消", async () => {
    stubNoopEventSource();
    const session = makeSession({
      id: "session-1",
      project_id: PROJECT_A.id,
      title: "等待子任务测试",
    });
    let parentRun = makeRun({
      status: "waiting_for_subagents",
    });
    let cancelCalled = false;

    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/projects") {
          return jsonResponse({ items: [PROJECT_A] });
        }
        if (url.pathname === "/api/v1/sessions") {
          return jsonResponse(listResponse([session], 1, 20, 1));
        }
        if (url.pathname === "/api/v1/sessions/session-1") {
          return jsonResponse(session);
        }
        if (url.pathname === "/api/v1/sessions/session-1/messages") {
          return jsonResponse({
            items: [
              {
                id: "msg-user-1",
                session_id: "session-1",
                role: "user",
                status: "completed",
                sequence: 1,
                parts: [{ type: "text", text: "请同时分析 2024 和 2025 年财报" }],
                reply_to_message_id: null,
                run_id: null,
                created_at: "2026-11-16T00:00:00Z",
                completed_at: "2026-11-16T00:00:00Z",
              },
            ],
            next_before_sequence: null,
          });
        }
        if (url.pathname === "/api/v1/sessions/session-1/files") {
          return jsonResponse({ items: [], next_cursor: null });
        }
        if (url.pathname === "/api/v1/skills") {
          return jsonResponse({ items: [] });
        }
        if (url.pathname === "/api/v1/sessions/session-1/runs") {
          return jsonResponse({ items: [parentRun], next_cursor: null });
        }
        if (url.pathname === "/api/v1/sessions/session-1/runs/run-parent-1") {
          return jsonResponse(parentRun);
        }
        if (
          url.pathname ===
          "/api/v1/sessions/session-1/runs/run-parent-1/tool-calls"
        ) {
          return jsonResponse({ items: [], next_after_ordinal: null });
        }
        if (
          url.pathname ===
            "/api/v1/sessions/session-1/runs/run-parent-1/cancel" &&
          method === "POST"
        ) {
          cancelCalled = true;
          parentRun = { ...parentRun, status: "cancelled" };
          return jsonResponse(parentRun);
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/chat/session-1");

    expect(
      await screen.findByText(
        "正在等待子智能体完成任务，完成后将自动继续回复；您可随时点击停止以取消任务",
      ),
    ).toBeInTheDocument();

    const submitButton = screen.getByRole("button", {
      name: "等待子智能体完成…",
    });
    expect(submitButton).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "停止任务" }));
    await waitFor(() => {
      expect(cancelCalled).toBe(true);
    });
  });

  it("同一 Subagent 并发派发 2 个任务并提出相同问题时可通过序号与 task_summary 区分路由", async () => {
    stubNoopEventSource();
    const session = makeSession({
      id: "session-1",
      project_id: PROJECT_A.id,
      title: "同源并发任务区分测试",
    });
    let parentRun = makeRun({
      subagent_tasks: [
        {
          sub_run_id: "sub-run-1",
          parent_tool_call_id: "call-1",
          subagent_profile_id: "prof-1",
          subagent_slug: "researcher",
          subagent_name: "调研员",
          task_index: 1,
          task_summary: "分析 2024 年财报",
          status: "waiting_for_input",
          safe_error_code: null,
          created_at: "2026-11-16T00:00:01Z",
          first_started_at: "2026-11-16T00:00:02Z",
          finished_at: null,
        },
        {
          sub_run_id: "sub-run-2",
          parent_tool_call_id: "call-2",
          subagent_profile_id: "prof-1",
          subagent_slug: "researcher",
          subagent_name: "调研员",
          task_index: 2,
          task_summary: "分析 2025 年财报",
          status: "waiting_for_input",
          safe_error_code: null,
          created_at: "2026-11-16T00:00:01Z",
          first_started_at: "2026-11-16T00:00:02Z",
          finished_at: null,
        },
      ],
      pending_subagent_inputs: [
        {
          sub_run_id: "sub-run-1",
          subagent_slug: "researcher",
          subagent_name: "调研员",
          task_index: 1,
          task_summary: "分析 2024 年财报",
          input_request_id: "req-1",
          question_message_id: "q-1",
          question_text: "请确认口径范围？",
          created_at: "2026-11-16T00:00:03Z",
        },
        {
          sub_run_id: "sub-run-2",
          subagent_slug: "researcher",
          subagent_name: "调研员",
          task_index: 2,
          task_summary: "分析 2025 年财报",
          input_request_id: "req-2",
          question_message_id: "q-2",
          question_text: "请确认口径范围？",
          created_at: "2026-11-16T00:00:04Z",
        },
      ],
    });

    const submittedPaths: string[] = [];

    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/projects") {
          return jsonResponse({ items: [PROJECT_A] });
        }
        if (url.pathname === "/api/v1/sessions") {
          return jsonResponse(listResponse([session], 1, 20, 1));
        }
        if (url.pathname === "/api/v1/sessions/session-1") {
          return jsonResponse(session);
        }
        if (url.pathname === "/api/v1/sessions/session-1/messages") {
          return jsonResponse({
            items: [
              {
                id: "msg-user-1",
                session_id: "session-1",
                role: "user",
                status: "completed",
                sequence: 1,
                parts: [{ type: "text", text: "请同时分析 2024 和 2025 年财报" }],
                reply_to_message_id: null,
                run_id: null,
                created_at: "2026-11-16T00:00:00Z",
                completed_at: "2026-11-16T00:00:00Z",
              },
            ],
            next_before_sequence: null,
          });
        }
        if (url.pathname === "/api/v1/sessions/session-1/files") {
          return jsonResponse({ items: [], next_cursor: null });
        }
        if (url.pathname === "/api/v1/skills") {
          return jsonResponse({ items: [] });
        }
        if (url.pathname === "/api/v1/sessions/session-1/runs") {
          return jsonResponse({ items: [parentRun], next_cursor: null });
        }
        if (url.pathname === "/api/v1/sessions/session-1/runs/run-parent-1") {
          return jsonResponse(parentRun);
        }
        if (
          url.pathname ===
          "/api/v1/sessions/session-1/runs/run-parent-1/tool-calls"
        ) {
          return jsonResponse({ items: [], next_after_ordinal: null });
        }
        if (
          url.pathname ===
            "/api/v1/sessions/session-1/runs/sub-run-2/input-requests/req-2/answer" &&
          method === "POST"
        ) {
          submittedPaths.push(url.pathname);
          parentRun = {
            ...parentRun,
            pending_subagent_inputs: parentRun.pending_subagent_inputs?.filter(
              (item) => item.input_request_id !== "req-2",
            ),
          };
          return jsonResponse(
            makeRun({ id: "sub-run-2", status: "queued" }),
          );
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/chat/session-1");

    const queue = await screen.findByRole("region", {
      name: "需要补充信息",
    });
    expect(within(queue).getByText("#1")).toBeInTheDocument();
    expect(within(queue).getByText("#2")).toBeInTheDocument();
    expect(within(queue).getAllByText(/分析 2024 年财报/)).not.toHaveLength(0);
    expect(within(queue).getByText(/分析 2025 年财报/)).toBeInTheDocument();

    // 默认选中 #1
    expect(
      within(queue).getByText("当前回复：#1「调研员」"),
    ).toBeInTheDocument();

    // 切换到 #2
    await user.click(
      within(queue).getByRole("button", { name: /#2.*分析 2025 年财报/ }),
    );
    expect(
      within(queue).getByText("当前回复：#2「调研员」"),
    ).toBeInTheDocument();

    await user.type(
      screen.getByRole("textbox", {
        name: "正在回答子智能体「调研员」的提问",
      }),
      "合并报表口径",
    );
    await user.click(screen.getByRole("button", { name: "提交给「调研员」" }));

    await waitFor(() => {
      expect(submittedPaths).toEqual([
        "/api/v1/sessions/session-1/runs/sub-run-2/input-requests/req-2/answer",
      ]);
    });

    // 主聊天消息区保持隔离，不插入子任务的回答文本
    const messageHistory = screen.getByRole("region", { name: "消息历史" });
    expect(
      await within(messageHistory).findByText("请同时分析 2024 和 2025 年财报"),
    ).toBeInTheDocument();
    expect(within(messageHistory).queryByText("合并报表口径")).toBeNull();
  });

  it("展开已完成且有问答记录的子任务详情时展示提问与回答，即使 tool_calls 为空", async () => {
    stubNoopEventSource();
    const session = makeSession({
      id: "session-1",
      project_id: PROJECT_A.id,
      title: "子任务问答回看",
    });
    const parentRun = makeRun({
      status: "completed",
      subagent_tasks: [
        {
          sub_run_id: "sub-run-qa-1",
          parent_tool_call_id: "call-qa-1",
          subagent_profile_id: "prof-1",
          subagent_slug: "researcher",
          subagent_name: "调研员",
          task_index: 1,
          task_summary: "核对关键指标定义",
          status: "completed",
          safe_error_code: null,
          created_at: "2026-11-16T00:00:01Z",
          first_started_at: "2026-11-16T00:00:02Z",
          finished_at: "2026-11-16T00:00:08Z",
        },
      ],
    });
    const subRunWithInteractions = makeRun({
      id: "sub-run-qa-1",
      status: "completed",
      event_sequence: 4,
      input_interactions: [
        {
          input_request_id: "req-qa-1",
          status: "answered",
          question_text: "请确认指标是否包含海外子公司？",
          answer_text: "包含全部海外子公司。",
          created_at: "2026-11-16T00:00:03Z",
          answered_at: "2026-11-16T00:00:06Z",
        },
      ],
    });

    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/projects") {
          return jsonResponse({ items: [PROJECT_A] });
        }
        if (url.pathname === "/api/v1/sessions") {
          return jsonResponse(listResponse([session], 1, 20, 1));
        }
        if (url.pathname === "/api/v1/sessions/session-1") {
          return jsonResponse(session);
        }
        if (url.pathname === "/api/v1/sessions/session-1/messages") {
          return jsonResponse({ items: [], next_before_sequence: null });
        }
        if (url.pathname === "/api/v1/sessions/session-1/files") {
          return jsonResponse({ items: [], next_cursor: null });
        }
        if (url.pathname === "/api/v1/skills") {
          return jsonResponse({ items: [] });
        }
        if (url.pathname === "/api/v1/sessions/session-1/runs") {
          return jsonResponse({ items: [parentRun], next_cursor: null });
        }
        if (url.pathname === "/api/v1/sessions/session-1/runs/run-parent-1") {
          return jsonResponse(parentRun);
        }
        if (
          url.pathname ===
          "/api/v1/sessions/session-1/runs/run-parent-1/tool-calls"
        ) {
          return jsonResponse({ items: [], next_after_ordinal: null });
        }
        if (url.pathname === "/api/v1/sessions/session-1/runs/sub-run-qa-1") {
          return jsonResponse(subRunWithInteractions);
        }
        if (
          url.pathname ===
          "/api/v1/sessions/session-1/runs/sub-run-qa-1/tool-calls"
        ) {
          return jsonResponse({ items: [], next_after_ordinal: null });
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/chat/session-1");

    const progress = await screen.findByRole("region", {
      name: "执行进度",
    });
    await user.click(
      within(progress).getByRole("button", { name: /已完成 1 个步骤/ }),
    );
    await user.click(
      await within(progress).findByRole("button", { name: "查看详情" }),
    );

    expect(
      await within(progress).findByText("补充问答记录"),
    ).toBeInTheDocument();
    expect(within(progress).getByText("已回答")).toBeInTheDocument();
    expect(
      within(progress).getByText("请确认指标是否包含海外子公司？"),
    ).toBeInTheDocument();
    expect(
      within(progress).getByText("包含全部海外子公司。"),
    ).toBeInTheDocument();
    expect(within(progress).queryByText(/暂无执行步骤/)).toBeNull();
  });

  it("展开 waiting_for_input 子任务后提交定向回答，即使返回空 input_interactions 也不丢失问答记录并刷新详情", async () => {
    stubNoopEventSource();
    const session = makeSession({
      id: "session-1",
      project_id: PROJECT_A.id,
      title: "展开态回答缓存回归",
    });
    let parentRun = makeRun({
      status: "waiting_for_subagents",
      subagent_tasks: [
        {
          sub_run_id: "sub-run-1",
          parent_tool_call_id: "call-1",
          subagent_profile_id: "prof-1",
          subagent_slug: "researcher",
          subagent_name: "调研员",
          task_index: 1,
          task_summary: "核对关键指标定义",
          status: "waiting_for_input",
          safe_error_code: null,
          created_at: "2026-11-16T00:00:01Z",
          first_started_at: "2026-11-16T00:00:02Z",
          finished_at: null,
        },
      ],
      pending_subagent_inputs: [
        {
          sub_run_id: "sub-run-1",
          subagent_slug: "researcher",
          subagent_name: "调研员",
          task_index: 1,
          task_summary: "核对关键指标定义",
          input_request_id: "req-1",
          question_message_id: "q-1",
          question_text: "请确认是否包含海外子公司？",
          created_at: "2026-11-16T00:00:03Z",
        },
      ],
    });

    let subRunDetailCalls = 0;
    let answered = false;

    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/projects") {
          return jsonResponse({ items: [PROJECT_A] });
        }
        if (url.pathname === "/api/v1/sessions") {
          return jsonResponse(listResponse([session], 1, 20, 1));
        }
        if (url.pathname === "/api/v1/sessions/session-1") {
          return jsonResponse(session);
        }
        if (url.pathname === "/api/v1/sessions/session-1/messages") {
          return jsonResponse({ items: [], next_before_sequence: null });
        }
        if (url.pathname === "/api/v1/sessions/session-1/files") {
          return jsonResponse({ items: [], next_cursor: null });
        }
        if (url.pathname === "/api/v1/skills") {
          return jsonResponse({ items: [] });
        }
        if (url.pathname === "/api/v1/sessions/session-1/runs") {
          return jsonResponse({ items: [parentRun], next_cursor: null });
        }
        if (url.pathname === "/api/v1/sessions/session-1/runs/run-parent-1") {
          return jsonResponse(parentRun);
        }
        if (
          url.pathname ===
          "/api/v1/sessions/session-1/runs/run-parent-1/tool-calls"
        ) {
          return jsonResponse({ items: [], next_after_ordinal: null });
        }
        if (url.pathname === "/api/v1/sessions/session-1/runs/sub-run-1") {
          subRunDetailCalls += 1;
          return jsonResponse(
            makeRun({
              id: "sub-run-1",
              status: answered ? "queued" : "waiting_for_input",
              event_sequence: answered ? 4 : 3,
              input_interactions: [
                {
                  input_request_id: "req-1",
                  status: answered ? "answered" : "pending",
                  question_text: "请确认是否包含海外子公司？",
                  answer_text: answered ? "包含海外子公司" : null,
                  created_at: "2026-11-16T00:00:03Z",
                  answered_at: answered ? "2026-11-16T00:00:05Z" : null,
                },
              ],
            }),
          );
        }
        if (
          url.pathname ===
          "/api/v1/sessions/session-1/runs/sub-run-1/tool-calls"
        ) {
          return jsonResponse({ items: [], next_after_ordinal: null });
        }
        if (
          url.pathname ===
            "/api/v1/sessions/session-1/runs/sub-run-1/input-requests/req-1/answer" &&
          method === "POST"
        ) {
          answered = true;
          parentRun = {
            ...parentRun,
            pending_subagent_inputs: [],
          };
          // 模拟服务端 answer 接口返回的精简 Run 中 input_interactions 为空数组
          return jsonResponse(
            makeRun({
              id: "sub-run-1",
              status: "queued",
              event_sequence: 4,
              input_interactions: [],
            }),
          );
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/chat/session-1");

    // 1. 先展开子任务详情并看到待回答的提问
    const progress = await screen.findByRole("region", {
      name: "执行进度",
    });
    await user.click(
      await within(progress).findByRole("button", { name: "查看详情" }),
    );
    expect(
      await within(progress).findByText("待回答"),
    ).toBeInTheDocument();
    expect(
      within(progress).getByText("请确认是否包含海外子公司？"),
    ).toBeInTheDocument();

    // 2. 在 Composer 提交定向回答
    await user.type(
      screen.getByRole("textbox", {
        name: "正在回答子智能体「调研员」的提问",
      }),
      "包含海外子公司",
    );
    await user.click(screen.getByRole("button", { name: "提交给「调研员」" }));

    // 3. 详情面板保持展开，立即展示已回答内容且不退化为空态，同时触发子 Run 详情刷新
    expect(
      await within(progress).findByText("已回答"),
    ).toBeInTheDocument();
    expect(
      within(progress).getByText("包含海外子公司"),
    ).toBeInTheDocument();
    expect(
      within(progress).queryByText(/暂无执行步骤或问答记录/),
    ).toBeNull();
    await waitFor(() => {
      expect(subRunDetailCalls).toBeGreaterThanOrEqual(2);
    });
  });
});
