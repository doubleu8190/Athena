import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  FakeSseStream,
  createSseStreamResponse,
} from "../../test/fakeSseStream";
import {
  getRequestUrl,
  listResponse,
  makeMessage,
  makeRun,
  makeSession,
  makeSessionFile,
  makeToolCall,
  makeToolCallDetail,
  PROJECT_A,
  PROJECT_B,
  TEST_USER,
} from "../../test/fixtures";
import { jsonResponse, renderApp } from "../../test/testUtils";
import type { SessionFile } from "../files/model";
import type { Session } from "../sessions/model";
import { sessionKeys } from "../sessions/queryKeys";
import {
  resolveToolDisplayName,
  type Run,
  type RunToolCallDetail,
  type RunToolCallSummary,
  type SafeSkillApprovalProjection,
} from "./model";
import { chatKeys } from "./queryKeys";

interface M25FetchOptions {
  files?: SessionFile[];
  messages?: ReturnType<typeof makeMessage>[];
  onApproval?: (
    runId: string,
    callId: string,
    body: { decision: "approve" | "reject"; request_id: string },
  ) => Response;
  onDeleteSession?: (sessionId: string) => Response;
  onPatchSession?: (
    sessionId: string,
    body: Record<string, unknown>,
  ) => Response;
  onRetryRun?: (
    runId: string,
    body: { confirm_unknown: boolean; request_id: string },
  ) => Response;
  runEvents?: () => { items: unknown[]; next_after_sequence: number | null };
  runs?: () => Run[];
  session?: () => Session | null;
  sessionStatus?: number;
  toolCallDetail?: (callId: string) => RunToolCallDetail | undefined;
  toolCalls?: () => RunToolCallSummary[];
}

function installM25Fetch(options: M25FetchOptions = {}) {
  const defaultSession = makeSession({
    id: "session-m25",
    project_id: PROJECT_A.id,
    title: "Q4 差旅费用复盘",
  });
  let currentSession: Session | null =
    options.session !== undefined ? options.session() : defaultSession;

  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(getRequestUrl(input), "http://localhost");
      const method = init?.method ?? "GET";
      const sessionData =
        options.session !== undefined ? options.session() : currentSession;
      const targetSessionId = sessionData?.id ?? "session-m25";

      if (url.pathname.endsWith("/auth/me")) {
        return jsonResponse(TEST_USER);
      }
      if (url.pathname === "/api/v1/models" && method === "GET") {
        return jsonResponse({ default_model_id: null, items: [] });
      }
      if (url.pathname === "/api/v1/knowledge-bases" && method === "GET") {
        return jsonResponse({ items: [], page: 1, page_size: 100 });
      }
      if (url.pathname === "/api/v1/skills" && method === "GET") {
        return jsonResponse({ items: [] });
      }
      if (url.pathname === "/api/v1/projects" && method === "GET") {
        return jsonResponse(
          listResponse([PROJECT_A, PROJECT_B], 1, 100, 2),
        );
      }
      if (url.pathname === "/api/v1/sessions" && method === "GET") {
        return jsonResponse(
          listResponse(sessionData ? [sessionData] : [], 1, 30, sessionData ? 1 : 0),
        );
      }
      if (
        url.pathname === `/api/v1/sessions/${targetSessionId}` &&
        method === "GET"
      ) {
        if (options.sessionStatus === 404 || !sessionData) {
          return jsonResponse({ detail: "session_not_found" }, 404);
        }
        return jsonResponse(sessionData);
      }
      if (
        url.pathname === `/api/v1/sessions/${targetSessionId}` &&
        method === "PATCH"
      ) {
        const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        if (options.onPatchSession) {
          return options.onPatchSession(targetSessionId, body);
        }
        currentSession = {
          ...(sessionData ?? defaultSession),
          ...("title" in body
            ? { title: body.title as string | null }
            : {}),
          ...("project_id" in body
            ? { project_id: body.project_id as string | null }
            : {}),
        };
        return jsonResponse(currentSession);
      }
      if (
        url.pathname === `/api/v1/sessions/${targetSessionId}` &&
        method === "DELETE"
      ) {
        if (options.onDeleteSession) {
          return options.onDeleteSession(targetSessionId);
        }
        currentSession = null;
        return new Response(null, { status: 204 });
      }
      if (
        url.pathname === `/api/v1/sessions/${targetSessionId}/messages` &&
        method === "GET"
      ) {
        return jsonResponse({
          items: options.messages ?? [],
          next_before_sequence: null,
        });
      }
      if (
        url.pathname === `/api/v1/sessions/${targetSessionId}/files` &&
        method === "GET"
      ) {
        return jsonResponse({
          items: options.files ?? [],
          next_after_file_id: null,
        });
      }
      const downloadMatch = new RegExp(
        `^/api/v1/sessions/${targetSessionId}/files/([^/]+)/download$`,
      ).exec(url.pathname);
      if (downloadMatch && method === "GET") {
        return new Response("file-content", {
          headers: {
            "Content-Disposition": 'attachment; filename="expense-summary.xlsx"',
            "Content-Type":
              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          },
          status: 200,
        });
      }
      if (
        url.pathname === `/api/v1/sessions/${targetSessionId}/runs` &&
        method === "GET"
      ) {
        const items = options.runs?.() ?? [];
        return jsonResponse({ items: items.slice(0, 1), next_cursor: null });
      }
      const runDetailMatch = new RegExp(
        `^/api/v1/sessions/${targetSessionId}/runs/([^/]+)$`,
      ).exec(url.pathname);
      if (runDetailMatch && method === "GET") {
        const runId = decodeURIComponent(runDetailMatch[1]);
        const matched = (options.runs?.() ?? []).find(
          (item) => item.id === runId,
        );
        return matched
          ? jsonResponse(matched)
          : jsonResponse({ detail: "run_not_found" }, 404);
      }
      const runEventsMatch = new RegExp(
        `^/api/v1/sessions/${targetSessionId}/runs/([^/]+)/events$`,
      ).exec(url.pathname);
      if (runEventsMatch && method === "GET") {
        return jsonResponse(
          options.runEvents?.() ?? { items: [], next_after_sequence: null },
        );
      }
      const toolCallsMatch = new RegExp(
        `^/api/v1/sessions/${targetSessionId}/runs/([^/]+)/tool-calls$`,
      ).exec(url.pathname);
      if (toolCallsMatch && method === "GET") {
        return jsonResponse({
          items: options.toolCalls?.() ?? [],
          next_after_ordinal: null,
        });
      }
      const toolDetailMatch = new RegExp(
        `^/api/v1/sessions/${targetSessionId}/runs/([^/]+)/tool-calls/([^/]+)$`,
      ).exec(url.pathname);
      if (toolDetailMatch && method === "GET") {
        const callId = decodeURIComponent(toolDetailMatch[2]);
        const detail = options.toolCallDetail?.(callId);
        return detail
          ? jsonResponse(detail)
          : jsonResponse({ detail: "run_not_found" }, 404);
      }
      const approvalMatch = new RegExp(
        `^/api/v1/sessions/${targetSessionId}/runs/([^/]+)/tool-calls/([^/]+)/approval$`,
      ).exec(url.pathname);
      if (approvalMatch && method === "POST") {
        const runId = decodeURIComponent(approvalMatch[1]);
        const callId = decodeURIComponent(approvalMatch[2]);
        const body = JSON.parse(String(init?.body)) as {
          decision: "approve" | "reject";
          request_id: string;
        };
        if (options.onApproval) {
          return options.onApproval(runId, callId, body);
        }
        return jsonResponse(
          makeRun({ id: runId, session_id: targetSessionId, status: "queued" }),
        );
      }
      const retryMatch = new RegExp(
        `^/api/v1/sessions/${targetSessionId}/runs/([^/]+)/retry$`,
      ).exec(url.pathname);
      if (retryMatch && method === "POST") {
        const runId = decodeURIComponent(retryMatch[1]);
        const body = JSON.parse(String(init?.body)) as {
          confirm_unknown: boolean;
          request_id: string;
        };
        if (options.onRetryRun) {
          return options.onRetryRun(runId, body);
        }
        return jsonResponse(
          makeRun({
            id: "run-retried-m25",
            retry_of_run_id: runId,
            session_id: targetSessionId,
            status: "queued",
          }),
        );
      }
      if (url.pathname.endsWith("/stream") && method === "GET") {
        return createSseStreamResponse(url.pathname, init);
      }
      throw new Error(`Unexpected request: ${method} ${url}`);
    },
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  FakeSseStream.instances = [];
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("M2.5 吸底沉浸式对话工作台（下）", () => {
  it("ChatHeader 支持内联改名、项目归属调整、文件抽屉下载与菜单删除跳回 /chat", async () => {
    const patches: Array<Record<string, unknown>> = [];
    let deletedId: string | null = null;
    const files: SessionFile[] = [
      makeSessionFile({
        filename: "invoice-q4.pdf",
        id: "file-upload-1",
        mime_type: "application/pdf",
        session_id: "session-m25",
        size_bytes: 4096,
        source: "upload",
      }),
      makeSessionFile({
        filename: "expense-summary.xlsx",
        id: "file-artifact-1",
        label: "Q4 差旅汇总表",
        mime_type:
          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        session_id: "session-m25",
        size_bytes: 12_288,
        source: "artifact",
      }),
    ];
    let session = makeSession({
      id: "session-m25",
      project_id: PROJECT_A.id,
      title: "Q4 差旅费用复盘",
    });

    installM25Fetch({
      files,
      onDeleteSession: (id) => {
        deletedId = id;
        return new Response(null, { status: 204 });
      },
      onPatchSession: (_id, body) => {
        patches.push(body);
        session = {
          ...session,
          ...("title" in body ? { title: body.title as string | null } : {}),
          ...("project_id" in body
            ? { project_id: body.project_id as string | null }
            : {}),
        };
        return jsonResponse(session);
      },
      runs: () => [],
      session: () => session,
    });

    const createObjectURL = vi
      .spyOn(URL, "createObjectURL")
      .mockReturnValue("blob:drawer-file");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    const anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);

    const user = userEvent.setup();
    const view = renderApp("/chat/session-m25");

    // 1. 点击标题进入内联编辑，IME 组合回车不提交，正常回车提交 PATCH
    const titleButton = await screen.findByRole("button", {
      name: "Q4 差旅费用复盘",
    });
    await user.click(titleButton);
    const titleInput = screen.getByLabelText("编辑会话标题");
    await user.clear(titleInput);
    await user.type(titleInput, "财务部 Q4 差旅复盘");
    fireEvent.keyDown(titleInput, { isComposing: true, key: "Enter" });
    expect(patches).toHaveLength(0);

    fireEvent.keyDown(titleInput, { key: "Enter" });
    await waitFor(() => {
      expect(patches).toEqual([{ title: "财务部 Q4 差旅复盘" }]);
    });
    expect(
      await screen.findByRole("heading", { name: "财务部 Q4 差旅复盘" }),
    ).toBeInTheDocument();

    // 2. 闲置会话点击项目胶囊打开 MoveSessionProjectDialog 切换归属
    await user.click(
      screen.getByRole("button", { name: `所属项目：${PROJECT_A.name}` }),
    );
    const moveDialog = await screen.findByRole("dialog", {
      name: "调整所属项目",
    });
    await user.click(within(moveDialog).getByRole("combobox", { name: "目标项目" }));
    await user.click(screen.getByRole("option", { name: PROJECT_B.name }));
    await user.click(within(moveDialog).getByRole("button", { name: "保存归属" }));
    await waitFor(() => {
      expect(patches).toHaveLength(2);
      expect(patches[1]).toEqual({ project_id: PROJECT_B.id });
    });
    expect(
      await screen.findByRole("button", {
        name: `所属项目：${PROJECT_B.name}`,
      }),
    ).toBeInTheDocument();

    // 3. 点击 [📎 2] 打开右侧 SessionFilesDrawer，区分上传附件与生成产物并支持下载
    const filesButton = screen.getByRole("button", { name: "会话文件（2）" });
    await user.click(filesButton);
    const drawer = await screen.findByRole("dialog", { name: /会话文件/ });
    expect(
      within(drawer).getByRole("region", { name: "生成产物" }),
    ).toHaveTextContent("Q4 差旅汇总表");
    expect(
      within(drawer).getByRole("region", { name: "上传附件" }),
    ).toHaveTextContent("invoice-q4.pdf");

    await user.click(
      within(drawer).getByRole("button", { name: "下载文件：Q4 差旅汇总表" }),
    );
    await waitFor(() => expect(anchorClick).toHaveBeenCalledTimes(1));
    expect(createObjectURL).toHaveBeenCalled();

    await user.click(within(drawer).getByRole("button", { name: "关闭" }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: /会话文件/ })).not.toBeInTheDocument(),
    );

    // 4. 通过 ... 更多菜单删除当前对话，确认后跳回 /chat
    await user.click(screen.getByRole("button", { name: "更多对话操作" }));
    await user.click(await screen.findByRole("menuitem", { name: "删除对话" }));
    const deleteDialog = await screen.findByRole("alertdialog", {
      name: "删除对话",
    });
    await user.click(
      within(deleteDialog).getByRole("button", { name: "确认删除" }),
    );
    await waitFor(() => {
      expect(deletedId).toBe("session-m25");
      expect(view.getLocation().pathname).toBe("/chat");
    });
  });

  it("活跃 Run 期间锁定项目归属胶囊并阻止调整归属弹窗打开", async () => {
    const activeRun = makeRun({
      id: "run-active-lock-project",
      session_id: "session-m25",
      status: "running",
    });
    installM25Fetch({
      runs: () => [activeRun],
    });
    const user = userEvent.setup();

    renderApp("/chat/session-m25");

    const lockedPill = await screen.findByRole("button", {
      name: `所属项目：${PROJECT_A.name}（任务进行中不可更改）`,
    });
    expect(lockedPill).toHaveAttribute("aria-disabled", "true");
    await user.click(lockedPill);
    expect(
      screen.queryByRole("dialog", { name: "调整所属项目" }),
    ).not.toBeInTheDocument();

    // 顶栏已全面去工程化，不暴露 Run ID 与执行代次
    expect(screen.queryByText(/run-active-lock-project/)).not.toBeInTheDocument();
    expect(screen.queryByText(/执行代次/)).not.toBeInTheDocument();
  });

  it("消息气泡展示模型快照标签，支持复制、用户消息「编辑重发」回填聚焦与 5 节点 aria-live 通告", async () => {

    const runningRun = makeRun({
      id: "run-model-badge",
      model_snapshot: {
        id: "llm-1",
        model: "o3-mini",
        name: "深度思考模型",
        scenario_label: "深度思考",
        supports_vision: false,
        version: 1,
      },
      session_id: "session-m25",
      status: "running",
    });

    installM25Fetch({
      messages: [
        makeMessage({
          id: "msg-user-1",
          parts: [{ text: "请总结 Q4 一线城市差旅标准", type: "text" }],
          role: "user",
          run_id: runningRun.id,
          sequence: 1,
          session_id: "session-m25",
        }),
        makeMessage({
          id: "msg-assistant-1",
          parts: [{ text: "一线城市住宿标准为 **600 元/晚**。", type: "text" }],
          role: "assistant",
          run_id: runningRun.id,
          sequence: 2,
          session_id: "session-m25",
        }),
      ],
      runs: () => [runningRun],
    });
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    renderApp("/chat/session-m25");

    // 助手消息顶部展示本轮固化模型标签
    const assistantArticle = await screen.findByRole("article", {
      name: "Athena的消息，序号 2",
    });
    expect(within(assistantArticle).getByText("深度思考")).toBeInTheDocument();

    // 复制助手回答
    await user.click(
      within(assistantArticle).getByRole("button", { name: "复制回答" }),
    );
    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith(
        "一线城市住宿标准为 **600 元/晚**。",
      );
    });

    // 复制 & 编辑重发用户消息 -> 文本回填至输入器并自动聚焦
    const userArticle = screen.getByRole("article", {
      name: "你的消息，序号 1",
    });
    await user.click(
      within(userArticle).getByRole("button", { name: "复制消息" }),
    );
    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith("请总结 Q4 一线城市差旅标准");
    });
    await user.click(
      within(userArticle).getByRole("button", { name: "编辑重发" }),
    );
    const composer = screen.getByLabelText("对话草稿");
    expect(composer).toHaveValue("请总结 Q4 一线城市差旅标准");
    expect(composer).toHaveFocus();

    // 验证 aria-live="polite" 仅在生命周期节点更新，流式 text_delta 绝不刷屏
    const liveRegion = document.querySelector('[aria-live="polite"]');
    expect(liveRegion).not.toBeNull();
    expect(liveRegion?.textContent).toBe("开始生成回复");

    await waitFor(() => {
      expect(FakeSseStream.instances.length).toBeGreaterThan(0);
    });
    const source = FakeSseStream.instances[0];
    act(() => {
      source.emit("text_delta", { payload: { text: "第一段流式" } });
      source.emit("text_delta", { payload: { text: "第二段流式" } });
    });
    expect(liveRegion?.textContent).toBe("开始生成回复");
  });

  it("ExecutionProgressAccordion 运行态展示业务步骤文案，完成后自动折叠为单行摘要并支持展开", async () => {
    let currentRun = makeRun({
      first_started_at: "2026-11-20T09:00:00Z",
      finished_at: null,
      id: "run-progress-flow",
      session_id: "session-m25",
      status: "running",
    });
    let toolCalls: RunToolCallSummary[] = [
      makeToolCall({
        duration_ms: null,
        finished_at: null,
        id: "tc-kb",
        model_name: "knowledge_search",
        ordinal: 1,
        status: "executing",
        tool_call_id: "call-kb",
        tool_id: "builtin_knowledge_search",
      }),
    ];

    installM25Fetch({
      messages: [
        makeMessage({
          id: "msg-user-1",
          parts: [{ text: "查询差旅报销并提交报销单", type: "text" }],
          role: "user",
          run_id: currentRun.id,
          sequence: 1,
          session_id: "session-m25",
        }),
      ],
      runEvents: () => ({
        items: [
          {
            created_at: "2026-11-20T09:00:04Z",
            id: "ev-done",
            payload: { assistant_message_id: "msg-done" },
            run_id: currentRun.id,
            sequence: 5,
            type: "completed",
          },
        ],
        next_after_sequence: null,
      }),
      runs: () => [currentRun],
      toolCalls: () => toolCalls,
    });
    const user = userEvent.setup();

    renderApp("/chat/session-m25");

    const progress = await screen.findByRole("region", { name: "执行进度" });
    expect(
      await within(progress).findByText("正在查阅企业知识库…"),
    ).toBeInTheDocument();

    // 手动展开后，待 Run 完成时自动收起为单行紧凑摘要
    await user.click(within(progress).getByRole("button", { name: /展开/ }));
    expect(
      within(progress).getByRole("list", { name: "工具调用步骤" }),
    ).toBeInTheDocument();

    currentRun = {
      ...currentRun,
      event_sequence: 5,
      finished_at: "2026-11-20T09:00:03.800Z",
      status: "completed",
    };
    toolCalls = [
      makeToolCall({
        duration_ms: 1200,
        id: "tc-kb",
        model_name: "knowledge_search",
        ordinal: 1,
        status: "completed",
        tool_call_id: "call-kb",
        tool_id: "builtin_knowledge_search",
      }),
      makeToolCall({
        display_name: "提交费用报销单",
        duration_ms: 2600,
        id: "tc-erp",
        model_name: "erp_expense_mcp__submit_expense",
        ordinal: 2,
        status: "completed",
        tool_call_id: "call-erp",
        tool_id: "tool-erp",
      }),
    ];

    await waitFor(() => {
      expect(FakeSseStream.instances.length).toBeGreaterThan(0);
    });
    const source = FakeSseStream.instances[0];
    act(() => {
      source.emit("snapshot", {
        payload: {
          event_sequence: 5,
          events: [],
          events_after_sequence: null,
          events_complete: true,
          messages: [],
          messages_before_sequence: null,
          messages_complete: true,
          status: "completed",
        },
      });
    });

    // 完成态自动收起并展示单行紧凑摘要（步骤数 · 工具业务名 · 耗时）
    await waitFor(() => {
      expect(
        within(progress).queryByRole("list", { name: "工具调用步骤" }),
      ).not.toBeInTheDocument();
    });
    expect(
      await within(progress).findByText(
        "已完成 2 个步骤 · 查阅企业知识库、提交费用报销单 · 3.8s",
      ),
    ).toBeInTheDocument();
  });

  it("业务化审批卡片直接渲染 display_name / approval_summary / formatted_arguments，不请求原始详情，并支持 Skill can_approve=false 门禁", async () => {
    const blockedSkillProjection: SafeSkillApprovalProjection = {
      call_id: "call-skill-blocked",
      can_approve: false,
      hidden_parameter_count: 2,
      input_files: [],
      parameter_summary: [{ label: "报表周期", name: "period", value: "2026-Q4" }],
      purpose: "导出部门预算明细",
      script_label: "导出预算表",
      skill_id: "skill-budget",
      skill_name: "预算导出技能",
      skill_version_id: "ver-1",
      skill_version_label: "v2",
      unavailable_reason: "技能版本已下线，无法确认执行，请拒绝本次调用。",
    };

    let currentRun = makeRun({
      id: "run-business-approval",
      pending_approvals: [
        {
          approval_summary: "为 财务员工 提交 1280 元报销单（科目：差旅费）",
          batch_message_id: "batch-1",
          call_id: "call-erp-1",
          display_name: "提交费用报销单",
          formatted_arguments: [
            { key: "applicant", label: "申请人", value: "财务员工" },
            { key: "amount", label: "报销金额", value: "1280" },
            { key: "category", label: "费用科目", value: "差旅费" },
            { key: "cost_center", label: "cost_center", value: "CC-2026" },
          ],
          hidden_argument_count: 0,
          model_name: "erp_expense_mcp__submit_expense",
          ordinal: 1,
          requested_at: "2026-11-20T09:20:00Z",
          safe_skill_projection: null,
          tool_kind: "mcp",
        },
        {
          approval_summary: null,
          batch_message_id: "batch-1",
          call_id: "call-skill-blocked",
          display_name: null,
          formatted_arguments: [],
          hidden_argument_count: 0,
          model_name: "skill_execute",
          ordinal: 2,
          requested_at: "2026-11-20T09:20:01Z",
          safe_skill_projection: blockedSkillProjection,
          tool_kind: "builtin",
        },
      ],
      pending_subagent_approvals: [
        {
          approval_summary: "向审批人发送合规复核通知",
          batch_message_id: "sub-batch-1",
          call_id: "sub-call-notify",
          display_name: "发送飞书通知",
          formatted_arguments: [
            { key: "receiver", label: "接收人", value: "财务主管" },
          ],
          hidden_argument_count: 0,
          model_name: "feishu_mcp__send_notice",
          ordinal: 1,
          requested_at: "2026-11-20T09:20:02Z",
          safe_skill_projection: null,
          sub_run_id: "sub-run-compliance",
          subagent_name: "财务合规专员",
          subagent_slug: "finance_compliance",
          task_index: 1,
          task_summary: "核验报销单合规性",
          tool_kind: "mcp",
        },
      ],
      session_id: "session-m25",
      status: "waiting_for_approval",
    });

    const recordedDecisions: Array<{
      callId: string;
      decision: string;
      runId: string;
    }> = [];

    const fetchMock = installM25Fetch({
      onApproval: (runId, callId, body) => {
        recordedDecisions.push({ callId, decision: body.decision, runId });
        if (callId === "call-erp-1") {
          currentRun = {
            ...currentRun,
            pending_approvals: currentRun.pending_approvals.filter(
              (item) => item.call_id !== callId,
            ),
          };
          return jsonResponse(currentRun);
        }
        return jsonResponse(
          makeRun({ id: runId, session_id: "session-m25", status: "queued" }),
        );
      },
      runs: () => [currentRun],
      toolCallDetail: (callId) =>
        makeToolCallDetail({
          formatted_arguments: [],
          hidden_argument_count: 1,
          id: callId,
          ordinal: 1,
          tool_call_id: callId,
        }),
    });
    const user = userEvent.setup();

    renderApp("/chat/session-m25");

    // 1. 验证主 Run 业务审批卡片渲染业务名、摘要、格式化键值表，且无英文眉标和工程文案
    const mainApprovalRegion = await screen.findByRole("region", {
      name: "等待工具审批",
    });
    expect(screen.queryByText("Approval required")).not.toBeInTheDocument();
    expect(screen.queryByText(/Run 会重新排队/)).not.toBeInTheDocument();

    const erpCard = within(mainApprovalRegion).getByRole("article", {
      name: "操作确认：提交费用报销单",
    });
    expect(within(erpCard).getByText("1 / 2")).toBeInTheDocument();
    expect(
      within(erpCard).getByText(
        "为 财务员工 提交 1280 元报销单（科目：差旅费）",
      ),
    ).toBeInTheDocument();
    expect(within(erpCard).getByText("申请人")).toBeInTheDocument();
    expect(within(erpCard).getByText("财务员工")).toBeInTheDocument();
    expect(within(erpCard).getByText("报销金额")).toBeInTheDocument();
    expect(within(erpCard).getByText("1280")).toBeInTheDocument();
    expect(within(erpCard).getByText("CC-2026")).toBeInTheDocument();
    // 已下发 formatted_arguments 时不得展示兜底「查看参数」或拉取原始 tool-call detail
    expect(
      within(erpCard).queryByRole("button", { name: "查看参数" }),
    ).not.toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([input]) =>
        new URL(getRequestUrl(input), "http://localhost").pathname.endsWith(
          "/tool-calls/call-erp-1",
        ),
      ),
    ).toBe(false);

    // 2. 验证 Skill 审批遵循 SafeSkillProjection 与 can_approve=false 阻断
    const skillCard = within(mainApprovalRegion).getByRole("article", {
      name: "操作确认：执行技能脚本",
    });
    expect(within(skillCard).getByText("预算导出技能")).toBeInTheDocument();
    expect(
      within(skillCard).getByText(
        "技能版本已下线，无法确认执行，请拒绝本次调用。",
      ),
    ).toBeInTheDocument();
    expect(
      within(skillCard).getByRole("button", { name: "安全投影缺失，不可批准" }),
    ).toBeDisabled();
    expect(
      within(skillCard).getByRole("button", { name: "拒绝" }),
    ).toBeEnabled();

    // 3. 验证子智能体业务审批卡片展示「来自：财务合规专员」徽标与参数表
    const subApprovalRegion = screen.getByRole("region", {
      name: "协作任务操作审批（1）",
    });
    expect(
      within(subApprovalRegion).getByText("来自：财务合规专员"),
    ).toBeInTheDocument();
    expect(
      within(subApprovalRegion).getByText("向审批人发送合规复核通知"),
    ).toBeInTheDocument();
    expect(within(subApprovalRegion).getByText("接收人")).toBeInTheDocument();
    expect(within(subApprovalRegion).getByText("财务主管")).toBeInTheDocument();

    // 4. 确认主 Run 报销工具 & 拒绝子智能体通知工具，验证闭环路由
    await user.click(
      within(erpCard).getByRole("button", { name: "确认执行" }),
    );
    await waitFor(() => {
      expect(recordedDecisions).toContainEqual({
        callId: "call-erp-1",
        decision: "approve",
        runId: "run-business-approval",
      });
    });

    await user.click(
      within(subApprovalRegion).getByRole("button", { name: "拒绝" }),
    );
    await waitFor(() => {
      expect(recordedDecisions).toContainEqual({
        callId: "sub-call-notify",
        decision: "reject",
        runId: "sub-run-compliance",
      });
    });
  });

  it("首轮 Run 完成后（title === null）按有界退避静默刷新自动标题，标题到达后立即停止轮询并在卸载时清理定时器", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let sessionDetailReads = 0;
    let currentTitle: string | null = null;
    const completedRun = makeRun({
      id: "run-first-completed",
      session_id: "session-m25",
      status: "completed",
    });

    const fetchMock = installM25Fetch({
      messages: [
        makeMessage({
          id: "msg-user-1",
          parts: [{ text: "请总结一线城市差旅标准", type: "text" }],
          role: "user",
          run_id: completedRun.id,
          sequence: 1,
          session_id: "session-m25",
        }),
        makeMessage({
          id: "msg-assistant-1",
          parts: [{ text: "一线城市差旅报销标准如下…", type: "text" }],
          role: "assistant",
          run_id: completedRun.id,
          sequence: 2,
          session_id: "session-m25",
        }),
      ],
      runEvents: () => ({
        items: [
          {
            created_at: "2026-11-20T09:00:01Z",
            id: "ev-first-done",
            payload: { assistant_message_id: "msg-assistant-1" },
            run_id: completedRun.id,
            sequence: 2,
            type: "completed",
          },
        ],
        next_after_sequence: null,
      }),
      runs: () => [completedRun],
      session: () =>
        makeSession({
          id: "session-m25",
          project_id: null,
          title: currentTitle,
        }),
    });

    const originalFetch = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (input, init) => {
      const url = new URL(getRequestUrl(input), "http://localhost");
      if (
        url.pathname === "/api/v1/sessions/session-m25" &&
        (!init?.method || init.method === "GET")
      ) {
        sessionDetailReads += 1;
        // 第 3 次读取（1000ms 退避复查）模拟 Worker 已异步落库中文标题
        if (sessionDetailReads >= 3) {
          currentTitle = "一线城市差旅报销标准";
        }
      }
      return originalFetch(input, init);
    });

    const view = renderApp("/chat/session-m25");

    // 初始展示「未命名对话」
    expect(
      await screen.findByRole("heading", { name: "未命名对话" }),
    ).toBeInTheDocument();

    // 推进 1000ms 触发第 1 次静默退避复查，自动标题平滑呈现
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_050);
    });
    expect(
      await screen.findByRole("heading", { name: "一线城市差旅报销标准" }),
    ).toBeInTheDocument();

    const readsAfterTitleArrived = sessionDetailReads;
    // 继续推进超过第 2 次退避（2500ms），标题已到达因此不得再发多余轮询请求
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });
    expect(sessionDetailReads).toBe(readsAfterTitleArrived);
    view.unmount();

    // 验证卸载时立即清除未触发的退避定时器
    let unmountDetailReads = 0;
    const secondFetch = installM25Fetch({
      runs: () => [
        makeRun({
          id: "run-unmount-check",
          session_id: "session-m25",
          status: "completed",
        }),
      ],
      session: () =>
        makeSession({
          id: "session-m25",
          project_id: null,
          title: null,
        }),
    });
    const secondImpl = secondFetch.getMockImplementation()!;
    secondFetch.mockImplementation(async (input, init) => {
      const url = new URL(getRequestUrl(input), "http://localhost");
      if (
        url.pathname === "/api/v1/sessions/session-m25" &&
        (!init?.method || init.method === "GET")
      ) {
        unmountDetailReads += 1;
      }
      return secondImpl(input, init);
    });

    const secondView = renderApp("/chat/session-m25");
    expect(
      await screen.findByRole("heading", { name: "未命名对话" }),
    ).toBeInTheDocument();
    const readsBeforeUnmount = unmountDetailReads;
    secondView.unmount();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(unmountDetailReads).toBe(readsBeforeUnmount);
  });

  it("失败态柔和红框卡片弹出温和重试 AlertDialog，隐藏工程字段且仍透传 confirm_unknown: true", async () => {
    const retryBodies: Array<{ confirm_unknown: boolean; request_id: string }> =
      [];
    const failedRun = makeRun({
      execution_epoch: 3,
      id: "run-failed-secret-id",
      safe_error_code: "tool_execution_failed",
      session_id: "session-m25",
      status: "failed",
    });
    installM25Fetch({
      onRetryRun: (_runId, body) => {
        retryBodies.push(body);
        return jsonResponse(
          makeRun({
            id: "run-retry-ok",
            retry_of_run_id: failedRun.id,
            session_id: "session-m25",
            status: "queued",
          }),
        );
      },
      runs: () => [failedRun],
    });
    const user = userEvent.setup();

    renderApp("/chat/session-m25");

    expect(await screen.findByText("任务执行未完成")).toBeInTheDocument();
    expect(
      screen.getByText("部分业务操作执行失败，导致本次任务未能完成。"),
    ).toBeInTheDocument();
    // 全面去工程化：页面不暴露 Run ID、执行代次或 confirm_unknown
    expect(screen.queryByText(/run-failed-secret-id/)).not.toBeInTheDocument();
    expect(screen.queryByText(/执行代次/)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "重新执行" }));
    const dialog = await screen.findByRole("alertdialog", {
      name: "重新执行本次任务",
    });
    expect(
      within(dialog).queryByText(/confirm_unknown|execution_epoch/),
    ).not.toBeInTheDocument();

    await user.click(
      within(dialog).getByRole("button", { name: "确认重新执行" }),
    );
    await waitFor(() => expect(retryBodies).toHaveLength(1));
    expect(retryBodies[0].confirm_unknown).toBe(true);
  });

  it("404 会话展示新版 EmptyState 与「开启新对话」主按钮", async () => {
    installM25Fetch({
      session: () => null,
      sessionStatus: 404,
    });

    renderApp("/chat/session-m25");

    expect(
      await screen.findByRole("heading", { name: "对话不存在或当前无权访问" }),
    ).toBeInTheDocument();
    const newChatLink = screen.getByRole("link", { name: "开启新对话" });
    expect(newChatLink).toHaveAttribute("href", "/chat");
  });

  it("零消息且无 Run 的空会话正常渲染「开始这段对话」空态，且无权限项目回退为「项目不可访问」", async () => {
    installM25Fetch({
      messages: [],
      runs: () => [],
      session: () =>
        makeSession({
          id: "session-m25",
          project_id: "project-inaccessible",
          title: "新建空白对话",
        }),
    });

    renderApp("/chat/session-m25");

    expect(
      await screen.findByRole("heading", { name: "开始这段对话" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "所属项目：项目不可访问" }),
    ).toBeInTheDocument();
  });

  it("审批卡片对缺失投影或 can_approve=false 的技能严格阻断，且无 formatted_arguments 的工具不拉取原始参数并优雅渲染", async () => {
    const blockedDetailProjection: SafeSkillApprovalProjection = {
      call_id: "call-skill-blocked-proj",
      can_approve: false,
      hidden_parameter_count: 1,
      input_files: [],
      parameter_summary: [{ label: "科目", name: "subject", value: "差旅费" }],
      purpose: "自动生成合规凭证",
      script_label: "生成凭证脚本",
      skill_id: "skill-voucher",
      skill_name: "合规凭证技能",
      skill_version_id: "ver-2",
      skill_version_label: "v2",
      unavailable_reason: "技能已停用，禁止继续执行。",
    };

    const approvalRun = makeRun({
      id: "run-skill-and-empty-args-guard",
      pending_approvals: [
        {
          approval_summary: null,
          batch_message_id: "batch-guard",
          call_id: "call-skill-no-proj",
          display_name: "外部自动化脚本",
          formatted_arguments: [],
          hidden_argument_count: 0,
          model_name: "skill_execute",
          ordinal: 1,
          requested_at: "2026-11-20T09:20:00Z",
          safe_skill_projection: null,
          tool_kind: "builtin",
        },
        {
          approval_summary: null,
          batch_message_id: "batch-guard",
          call_id: "call-skill-blocked-proj",
          display_name: "财务凭证脚本",
          formatted_arguments: [],
          hidden_argument_count: 0,
          model_name: "skill_execute",
          ordinal: 2,
          requested_at: "2026-11-20T09:20:01Z",
          safe_skill_projection: blockedDetailProjection,
          tool_kind: "builtin",
        },
        {
          approval_summary: null,
          batch_message_id: "batch-guard",
          call_id: "call-mcp-no-args",
          display_name: "同步组织状态",
          formatted_arguments: [],
          hidden_argument_count: 0,
          model_name: "org_sync_status",
          ordinal: 3,
          requested_at: "2026-11-20T09:20:02Z",
          safe_skill_projection: null,
          tool_kind: "mcp",
        },
      ],
      session_id: "session-m25",
      status: "waiting_for_approval",
    });

    let toolDetailRequests = 0;
    installM25Fetch({
      runs: () => [approvalRun],
      toolCallDetail: () => {
        toolDetailRequests += 1;
        return undefined;
      },
    });

    renderApp("/chat/session-m25");

    // 1. 技能调用缺失 safe_skill_projection：直接展示阻断告警并禁用批准，仅允许拒绝
    const noProjCard = await screen.findByRole("article", {
      name: "操作确认：外部自动化脚本",
    });
    expect(
      within(noProjCard).getByText(/服务端没有返回可审批的安全投影/),
    ).toBeInTheDocument();
    expect(
      within(noProjCard).queryByRole("button", { name: "查看参数" }),
    ).not.toBeInTheDocument();
    expect(
      within(noProjCard).getByRole("button", { name: "安全投影缺失，不可批准" }),
    ).toBeDisabled();
    expect(within(noProjCard).getByRole("button", { name: "拒绝" })).toBeEnabled();

    // 2. 技能调用返回 safe_skill_projection(can_approve=false)：展示安全投影且禁用批准
    const blockedProjCard = screen.getByRole("article", {
      name: "操作确认：财务凭证脚本",
    });
    expect(
      within(blockedProjCard).getByText("合规凭证技能"),
    ).toBeInTheDocument();
    expect(
      within(blockedProjCard).getByText("技能已停用，禁止继续执行。"),
    ).toBeInTheDocument();
    expect(
      within(blockedProjCard).getByRole("button", {
        name: "安全投影缺失，不可批准",
      }),
    ).toBeDisabled();
    expect(
      within(blockedProjCard).getByRole("button", { name: "拒绝" }),
    ).toBeEnabled();

    // 3. 普通工具无 approval_summary 且 formatted_arguments 为空：优雅渲染，不展示「查看参数」也不拉取 /tool-calls/{id}
    const noArgsCard = screen.getByRole("article", {
      name: "操作确认：同步组织状态",
    });
    expect(
      within(noArgsCard).queryByRole("button", { name: "查看参数" }),
    ).not.toBeInTheDocument();
    expect(
      within(noArgsCard).getByRole("button", { name: "确认执行" }),
    ).toBeEnabled();
    expect(toolDetailRequests).toBe(0);
  });

  it("React.StrictMode 双挂载下首轮无标题完成态仍正常触发退避自动标题刷新", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let sessionDetailReads = 0;
    let allowAutoTitle = false;
    let currentTitle: string | null = null;
    const completedRun = makeRun({
      id: "run-strictmode-auto-title",
      session_id: "session-m25",
      status: "completed",
      trigger_message_id: "msg-strict-user",
    });
    const initialSession = makeSession({
      id: "session-m25",
      project_id: null,
      title: null,
    });

    const fetchMock = installM25Fetch({
      messages: [
        makeMessage({
          id: "msg-strict-user",
          parts: [{ text: "汇总华东区差旅预算", type: "text" }],
          role: "user",
          run_id: completedRun.id,
          sequence: 1,
          session_id: "session-m25",
        }),
        makeMessage({
          id: "msg-strict-assistant",
          parts: [{ text: "华东区差旅预算汇总完成。", type: "text" }],
          role: "assistant",
          run_id: completedRun.id,
          sequence: 2,
          session_id: "session-m25",
        }),
      ],
      runEvents: () => ({
        items: [
          {
            created_at: "2026-11-20T09:00:01Z",
            id: "ev-strict-done",
            payload: { assistant_message_id: "msg-strict-assistant" },
            run_id: completedRun.id,
            sequence: 2,
            type: "completed",
          },
        ],
        next_after_sequence: null,
      }),
      runs: () => [completedRun],
      session: () => ({
        ...initialSession,
        title: currentTitle,
      }),
    });

    const originalFetch = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (input, init) => {
      const url = new URL(getRequestUrl(input), "http://localhost");
      if (
        url.pathname === "/api/v1/sessions/session-m25" &&
        (!init?.method || init.method === "GET")
      ) {
        sessionDetailReads += 1;
        if (allowAutoTitle) {
          currentTitle = "华东区差旅预算汇总";
        }
      }
      return originalFetch(input, init);
    });

    // 预置会话与最新 Run 缓存，使 StrictMode 首次挂载 -> 模拟卸载清理 -> 二次挂载真实命中有界退避调度
    renderApp("/chat/session-m25", {
      seedQueryClient: (client) => {
        client.setQueryData(sessionKeys.detail("session-m25"), initialSession);
        client.setQueryData(chatKeys.latestRun("session-m25"), {
          items: [completedRun],
          next_cursor: null,
        });
      },
      strictMode: true,
    });

    expect(
      await screen.findByRole("heading", { name: "未命名对话" }),
    ).toBeInTheDocument();

    // 等待初始挂载与首拍即时刷新稳定，确认 1000ms 前标题仍为未命名对话
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    const readsBeforeBackoff = sessionDetailReads;
    expect(
      screen.getByRole("heading", { name: "未命名对话" }),
    ).toBeInTheDocument();

    // 开启异步标题落库并推进至 1000ms 退避窗口，验证 StrictMode 重挂载未丢失退避定时器
    allowAutoTitle = true;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_050);
    });

    expect(sessionDetailReads).toBeGreaterThan(readsBeforeBackoff);
    expect(
      await screen.findByRole("heading", { name: "华东区差旅预算汇总" }),
    ).toBeInTheDocument();

    const readsAfterTitle = sessionDetailReads;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });
    expect(sessionDetailReads).toBe(readsAfterTitle);
  });

  it("已取消 Run 的触发消息不在当前分页时，助手消息不渲染无效果的「重新执行」按钮", async () => {
    const cancelledRun = makeRun({
      id: "run-cancelled-missing-trigger",
      session_id: "session-m25",
      status: "cancelled",
      trigger_message_id: "msg-not-in-current-page",
    });

    installM25Fetch({
      messages: [
        makeMessage({
          id: "msg-assistant-cancelled",
          parts: [{ text: "本次操作已中途取消。", type: "text" }],
          role: "assistant",
          run_id: cancelledRun.id,
          sequence: 10,
          session_id: "session-m25",
        }),
      ],
      runs: () => [cancelledRun],
    });

    renderApp("/chat/session-m25");

    const assistantArticle = await screen.findByRole("article", {
      name: "Athena的消息，序号 10",
    });
    expect(
      within(assistantArticle).queryByRole("button", { name: "重新执行" }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("本次任务已取消")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "重新编辑发送" }),
    ).not.toBeInTheDocument();
  });

  it("执行进度非 Skill 工具仅渲染安全投影（摘要/白名单参数/隐藏参数提示/结果白名单/状态徽章），无别名 MCP 不暴露 mcp__xxxx", async () => {
    const completedRun = makeRun({
      id: "run-safe-tool-projection",
      session_id: "session-m25",
      status: "completed",
    });
    const calls: RunToolCallSummary[] = [
      makeToolCall({
        display_name: null,
        duration_ms: 350,
        id: "tc-mcp-anon",
        model_name: "mcp__secret_erp_create_order",
        ordinal: 1,
        status: "completed",
        tool_call_id: "call-mcp-anon",
        tool_id: "tool-mcp-anon",
        tool_kind: "mcp",
      }),
      makeToolCall({
        display_name: "提交退款审批",
        duration_ms: 180,
        id: "tc-mcp-cancelled",
        model_name: "mcp__refund_apply",
        ordinal: 2,
        status: "cancelled",
        tool_call_id: "call-mcp-cancelled",
        tool_id: "tool-mcp-cancelled",
        tool_kind: "mcp",
      }),
      makeToolCall({
        display_name: "查询外部额度",
        duration_ms: 90,
        id: "tc-mcp-unknown",
        model_name: "mcp__quota_query",
        ordinal: 3,
        status: "unknown",
        tool_call_id: "call-mcp-unknown",
        tool_id: "tool-mcp-unknown",
        tool_kind: "mcp",
      }),
    ];

    installM25Fetch({
      runs: () => [completedRun],
      toolCallDetail: (callId) => {
        if (callId === "tc-mcp-anon") {
          return makeToolCallDetail({
            business_summary: "已为当前员工提交差旅申请单",
            display_name: null,
            formatted_arguments: [
              { key: "city", label: "出差城市", value: "杭州" },
            ],
            hidden_argument_count: 2,
            id: "tc-mcp-anon",
            model_name: "mcp__secret_erp_create_order",
            ordinal: 1,
            outcome: {
              formatted_results: [
                { key: "order_no", label: "申请单号", value: "TR-2026-088" },
              ],
              status: "ok",
              summary: "申请单已成功创建并进入审批流程。",
              truncated: true,
            },
            tool_call_id: "call-mcp-anon",
            tool_kind: "mcp",
            truncated: false,
          });
        }
        if (callId === "tc-mcp-cancelled") {
          return makeToolCallDetail({
            business_summary: null,
            display_name: "提交退款审批",
            formatted_arguments: [],
            hidden_argument_count: 0,
            id: "tc-mcp-cancelled",
            model_name: "mcp__refund_apply",
            ordinal: 2,
            outcome: {
              formatted_results: [],
              status: "cancelled",
              summary: "用户已取消本次退款操作。",
              truncated: false,
            },
            status: "cancelled",
            tool_call_id: "call-mcp-cancelled",
            tool_kind: "mcp",
          });
        }
        return makeToolCallDetail({
          business_summary: null,
          display_name: "查询外部额度",
          formatted_arguments: [],
          hidden_argument_count: 1,
          id: "tc-mcp-unknown",
          model_name: "mcp__quota_query",
          ordinal: 3,
          outcome: {
            formatted_results: [],
            status: "unknown",
            summary: "外部系统响应超时，结果未知。",
            truncated: false,
          },
          status: "unknown",
          tool_call_id: "call-mcp-unknown",
          tool_kind: "mcp",
        });
      },
      toolCalls: () => calls,
    });
    const user = userEvent.setup();

    renderApp("/chat/session-m25");

    const progress = await screen.findByRole("region", { name: "执行进度" });
    // 收起摘要与步骤标题均不得暴露 mcp__secret_erp_create_order
    expect(
      screen.queryByText(/mcp__secret_erp_create_order/),
    ).not.toBeInTheDocument();
    expect(
      await within(progress).findByText(
        /已完成 3 个步骤 · 业务工具操作、提交退款审批、查询外部额度/,
      ),
    ).toBeInTheDocument();

    await user.click(
      within(progress).getByRole("button", { name: /已完成 3 个步骤/ }),
    );
    const stepList = within(progress).getByRole("list", {
      name: "工具调用步骤",
    });

    // 1. 无别名 MCP 工具步骤展开后展示安全投影
    await user.click(
      within(stepList).getByRole("button", { name: /业务工具操作/ }),
    );
    expect(
      await within(stepList).findByText("已为当前员工提交差旅申请单"),
    ).toBeInTheDocument();
    expect(within(stepList).getByText("关键参数")).toBeInTheDocument();
    expect(within(stepList).getByText("出差城市")).toBeInTheDocument();
    expect(within(stepList).getByText("杭州")).toBeInTheDocument();
    expect(
      within(stepList).getByText("另有 2 个内部参数未向普通用户展示。"),
    ).toBeInTheDocument();
    expect(within(stepList).getByText("执行成功")).toBeInTheDocument();
    expect(within(stepList).getByText("部分结果已省略展示")).toBeInTheDocument();
    expect(
      within(stepList).getByText("申请单已成功创建并进入审批流程。"),
    ).toBeInTheDocument();
    expect(within(stepList).getByText("申请单号")).toBeInTheDocument();
    expect(within(stepList).getByText("TR-2026-088")).toBeInTheDocument();

    // 2. 空参数且无隐藏参数时显示「无额外可展示参数。」及「已取消」结果徽章
    await user.click(
      within(stepList).getByRole("button", { name: /提交退款审批/ }),
    );
    expect(
      await within(stepList).findByText("无额外可展示参数。"),
    ).toBeInTheDocument();
    expect(
      within(stepList).getByText("用户已取消本次退款操作。"),
    ).toBeInTheDocument();

    // 3. unknown 状态徽章渲染「结果未知」，仅有隐藏参数时不显示「无额外可展示参数。」
    await user.click(
      within(stepList).getByRole("button", { name: /查询外部额度/ }),
    );
    expect(await within(stepList).findByText("结果未知")).toBeInTheDocument();
    expect(
      within(stepList).getByText("另有 1 个内部参数未向普通用户展示。"),
    ).toBeInTheDocument();
  });

  it("审批卡片对无别名 MCP 兜底显示「业务工具操作」并展示 hidden_argument_count 提示", async () => {
    const approvalRun = makeRun({
      id: "run-hidden-arg-approval",
      pending_approvals: [
        {
          approval_summary: "将执行外部账务核对操作",
          batch_message_id: "batch-hidden-1",
          call_id: "call-mcp-hidden-1",
          display_name: null,
          formatted_arguments: [
            { key: "month", label: "核算月份", value: "2026-11" },
          ],
          hidden_argument_count: 3,
          model_name: "mcp__internal_ledger_reconcile",
          ordinal: 1,
          requested_at: "2026-11-20T09:30:00Z",
          safe_skill_projection: null,
          tool_kind: "mcp",
        },
        {
          approval_summary: "将触发无白名单参数的内部同步",
          batch_message_id: "batch-hidden-1",
          call_id: "call-mcp-hidden-2",
          display_name: "同步凭证状态",
          formatted_arguments: [],
          hidden_argument_count: 2,
          model_name: "mcp__internal_sync_voucher",
          ordinal: 2,
          requested_at: "2026-11-20T09:30:01Z",
          safe_skill_projection: null,
          tool_kind: "mcp",
        },
      ],
      session_id: "session-m25",
      status: "waiting_for_approval",
    });

    installM25Fetch({
      runs: () => [approvalRun],
    });

    renderApp("/chat/session-m25");

    const region = await screen.findByRole("region", {
      name: "等待工具审批",
    });
    // 无别名 MCP 审批卡绝不暴露 mcp__internal_ledger_reconcile
    expect(
      screen.queryByText(/mcp__internal_ledger_reconcile/),
    ).not.toBeInTheDocument();
    const anonCard = within(region).getByRole("article", {
      name: "操作确认：业务工具操作",
    });
    expect(within(anonCard).getByText("核算月份")).toBeInTheDocument();
    expect(within(anonCard).getByText("2026-11")).toBeInTheDocument();
    expect(
      within(anonCard).getByText("另有 3 个内部参数未向普通用户展示。"),
    ).toBeInTheDocument();

    const hiddenOnlyCard = within(region).getByRole("article", {
      name: "操作确认：同步凭证状态",
    });
    expect(
      within(hiddenOnlyCard).getByText("另有 2 个内部参数未向普通用户展示。"),
    ).toBeInTheDocument();
  });

  it("resolveToolDisplayName 纵深防御拒绝无效 mcp__/原名别名并完整对齐内置中文映射", () => {
    // 1. 即使历史或异常载荷将 display_name 设为 mcp__xxxx 或等于 model_name，也不得穿透展示底层名称
    expect(
      resolveToolDisplayName({
        display_name: "mcp__secret_erp_call",
        model_name: "mcp__secret_erp_call",
        tool_kind: "mcp",
      }),
    ).toBe("业务工具操作");
    expect(
      resolveToolDisplayName(
        {
          display_name: "MCP__UPPER_SECRET",
          model_name: "opaque_tool",
          tool_kind: "mcp",
        },
        "mcp__fallback_secret",
      ),
    ).toBe("业务工具操作");
    expect(
      resolveToolDisplayName({
        display_name: "session_file_read",
        model_name: "session_file_read",
        tool_kind: "builtin",
      }),
    ).toBe("读取会话文件");

    // 2. 与后端 _BUILTIN_DISPLAY_NAMES 完全对齐的内置映射
    expect(
      resolveToolDisplayName({
        display_name: null,
        model_name: "request_user_input",
        tool_id: "builtin_request_user_input",
      }),
    ).toBe("请求补充信息");
    expect(
      resolveToolDisplayName({
        display_name: null,
        model_name: "session_file_list",
        tool_id: "builtin_session_file_list",
      }),
    ).toBe("查看会话文件");
    expect(
      resolveToolDisplayName({
        display_name: null,
        model_name: "session_file_read",
        tool_id: "builtin_session_file_read",
      }),
    ).toBe("读取会话文件");
    expect(
      resolveToolDisplayName({
        display_name: null,
        model_name: "skill_load",
        tool_id: "builtin_skill_load",
      }),
    ).toBe("加载技能指南");
    expect(
      resolveToolDisplayName({
        display_name: null,
        model_name: "skill_read_resource",
        tool_id: "builtin_skill_read_resource",
      }),
    ).toBe("读取技能资源");
  });
});
