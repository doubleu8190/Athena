import { expect, test, type Page, type Route } from "@playwright/test";

const NOW = "2026-11-24T09:00:00Z";

interface MockUserFixture {
  auth_provider: "mock" | "feishu" | "local";
  avatar_url: string | null;
  created_at: string;
  departments: Array<{
    id: string;
    is_primary: boolean;
    name: string;
  }>;
  display_name: string;
  email: string;
  employee_no: string;
  id: string;
  job_title: string;
  managed_department_ids: string[];
  role: "super_admin" | "dept_admin" | "member";
}

const MOCK_MEMBER: MockUserFixture = {
  auth_provider: "mock",
  avatar_url: null,
  created_at: "2026-10-01T08:00:00Z",
  departments: [
    {
      id: "dept-finance",
      is_primary: true,
      name: "财务分析部",
    },
  ],
  display_name: "林致远",
  email: "lin.zhiyuan@example.com",
  employee_no: "E2048",
  id: "mock-member-1",
  job_title: "高级财务分析专员",
  managed_department_ids: [],
  role: "member",
};

const MOCK_SUPER_ADMIN: MockUserFixture = {
  auth_provider: "mock",
  avatar_url: null,
  created_at: "2026-09-01T08:00:00Z",
  departments: [
    {
      id: "dept-platform",
      is_primary: true,
      name: "平台工程部",
    },
  ],
  display_name: "陈思远",
  email: "chen.siyuan@example.com",
  employee_no: "E0001",
  id: "mock-super-admin-1",
  job_title: "平台负责人",
  managed_department_ids: [],
  role: "super_admin",
};

interface SessionFixture {
  created_at: string;
  id: string;
  last_message_at: string | null;
  project_id: string | null;
  title: string | null;
  updated_at: string;
}

interface ProjectFixture {
  created_at: string;
  default_skill_ids: string[];
  description: string | null;
  id: string;
  instructions: string | null;
  name: string;
  updated_at: string;
}

interface MemoryFixture {
  content: string;
  created_at: string;
  id: string;
  source: "manual" | "chat_explicit";
  updated_at: string;
}

interface WorkbenchFixtureOptions {
  authenticated?: boolean;
  user?: MockUserFixture;
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    body: JSON.stringify(body),
    contentType: "application/json",
    status,
  });
}

async function expectNoHorizontalOverflow(page: Page) {
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth - window.innerWidth,
      ),
    )
    .toBeLessThanOrEqual(1);
}

/**
 * 安装第二期核心工作台确定性 HTTP fixture，覆盖认证、模型、知识库、会话、审批、偏好记忆与项目文件夹全链路。
 */
async function installWorkbenchFixtures(
  page: Page,
  options: WorkbenchFixtureOptions = {},
) {
  const state = {
    activeRunApproved: false,
    activeRunCreated: false,
    authenticated: options.authenticated ?? true,
    currentUser: options.user ?? MOCK_MEMBER,
    deletedProjectRequests: [] as string[],
    lastSubmittedRunBody: null as Record<string, unknown> | null,
    memories: [
      {
        content: "周报金额统一按万元展示并保留两位小数。",
        created_at: NOW,
        id: "mem-1",
        source: "chat_explicit" as const,
        updated_at: NOW,
      },
    ] as MemoryFixture[],
    preferences: {
      custom_instructions: "",
      memory_enabled: true,
      updated_at: NOW as string | null,
    },
    projects: [
      {
        created_at: "2026-11-01T08:00:00Z",
        default_skill_ids: [],
        description: "汇总各部门 Q4 预算执行偏差与滚动预测口径。",
        id: "proj-q4-budget",
        instructions: "请先输出结论摘要，再列示异常科目明细。",
        name: "Q4 预算专项",
        updated_at: NOW,
      },
    ] as ProjectFixture[],
    searchQueries: [] as string[],
    sessions: [
      {
        created_at: "2026-11-23T08:00:00Z",
        id: "sess-q4-review",
        last_message_at: "2026-11-23T10:30:00Z",
        project_id: "proj-q4-budget",
        title: "Q4 费用偏差复盘",
        updated_at: "2026-11-23T10:30:00Z",
      },
      {
        created_at: "2026-11-22T08:00:00Z",
        id: "sess-travel-policy",
        last_message_at: "2026-11-22T09:15:00Z",
        project_id: null,
        title: "差旅报销标准咨询",
        updated_at: "2026-11-22T09:15:00Z",
      },
    ] as SessionFixture[],
    uploadedFiles: [] as Array<{
      created_at: string;
      extracted_text_bytes: number | null;
      extraction_error_code: string | null;
      extraction_status: "none" | "pending" | "ready" | "failed";
      filename: string;
      id: string;
      label: string | null;
      mime_type: string;
      session_id: string;
      size_bytes: number;
      source: "upload" | "artifact";
    }>,
  };

  const buildRun = (sessionId: string) => {
    const approved = state.activeRunApproved;
    return {
      assistant_message_id: approved ? "msg-assistant-final" : null,
      cancel_requested_at: null,
      context_cutoff_sequence: 1,
      created_at: NOW,
      depth: 0,
      event_sequence: approved ? 6 : 3,
      execution_epoch: 1,
      finished_at: approved ? "2026-11-24T09:00:02Z" : null,
      first_started_at: "2026-11-24T09:00:00Z",
      id: "run-workbench-1",
      input_file_ids: state.uploadedFiles.map((file) => file.id),
      input_interactions: [],
      knowledge_base_ids:
        (state.lastSubmittedRunBody?.knowledge_base_ids as
          | string[]
          | null
          | undefined) ?? null,
      model_config_version_id: "model-analysis",
      model_snapshot: {
        id: "model-analysis",
        model: "athena-pro-analysis",
        name: "Athena Pro Analysis",
        scenario_label: "深度分析",
        supports_vision: true,
        version: 1,
      },
      parent_run_id: null,
      parent_tool_call_id: null,
      pending_approvals: approved
        ? []
        : [
            {
              approval_summary:
                "将为林致远提交 2026 年 11 月上海出差审批单，预计费用 4,800 元。",
              batch_message_id: "msg-assistant-batch",
              call_id: "call-travel-1",
              display_name: "发起差旅审批单",
              formatted_arguments: [
                { key: "destination", label: "出差城市", value: "上海市" },
                { key: "budget", label: "预计费用", value: "4,800 元" },
              ],
              hidden_argument_count: 0,
              model_name: "mcp__oa_create_travel_request",
              ordinal: 1,
              requested_at: NOW,
              safe_skill_projection: null,
              tool_kind: "mcp",
            },
          ],
      pending_input: null,
      pending_subagent_approvals: [],
      pending_subagent_inputs: [],
      prompt_template_version: "v1",
      queued_at: NOW,
      replayed: false,
      report_message_id: approved ? "msg-assistant-final" : null,
      retry_of_run_id: null,
      retry_warning: null,
      root_run_id: null,
      safe_error_code: null,
      session_id: sessionId,
      skill_snapshot: null,
      status: approved ? "completed" : "waiting_for_approval",
      subagent_name: null,
      subagent_profile_id: null,
      subagent_slug: null,
      subagent_tasks: [],
      trigger_message_id: "msg-user-1",
      updated_at: approved ? "2026-11-24T09:00:02Z" : NOW,
      usage: {
        answer_tokens: approved ? 128 : null,
        attempts: [],
        complete: approved,
        compression_tokens: 0,
        input_tokens: 320,
        output_tokens: approved ? 128 : null,
        total_tokens: approved ? 448 : null,
      },
    };
  };

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === "/api/v1/auth/providers" && method === "GET") {
      return json(route, {
        feishu_enabled: false,
        local_login_enabled: true,
        mock_enabled: true,
      });
    }
    if (path === "/api/v1/auth/mock/users" && method === "GET") {
      return json(route, {
        items: [MOCK_MEMBER, MOCK_SUPER_ADMIN],
      });
    }
    if (path === "/api/v1/auth/mock/login" && method === "POST") {
      const body = request.postDataJSON() as { user_id: string };
      const target =
        body.user_id === MOCK_SUPER_ADMIN.id ? MOCK_SUPER_ADMIN : MOCK_MEMBER;
      state.authenticated = true;
      state.currentUser = target;
      return json(route, target);
    }
    if (path === "/api/v1/auth/me" && method === "GET") {
      if (!state.authenticated) {
        return json(route, { detail: "unauthenticated" }, 401);
      }
      return json(route, state.currentUser);
    }
    if (path === "/api/v1/auth/logout" && method === "POST") {
      state.authenticated = false;
      return route.fulfill({ status: 204 });
    }

    if (path === "/api/v1/models" && method === "GET") {
      return json(route, {
        default_model_id: "model-general",
        items: [
          {
            id: "model-general",
            is_default: true,
            name: "Athena General",
            scenario_description: "日常问答、邮件起草与制度检索",
            scenario_label: "通用办公",
            supports_vision: false,
          },
          {
            id: "model-analysis",
            is_default: false,
            name: "Athena Pro Analysis",
            scenario_description: "复杂财务分析、长文档比对与图表解读",
            scenario_label: "深度分析",
            supports_vision: true,
          },
        ],
      });
    }
    if (path === "/api/v1/skills" && method === "GET") {
      return json(route, { items: [] });
    }
    if (path === "/api/v1/knowledge-bases" && method === "GET") {
      return json(route, {
        items: [
          {
            created_at: NOW,
            description: "公司差旅住宿与交通补贴标准",
            document_count: 6,
            id: "kb-company-travel",
            name: "企业差旅与报销制度",
            owner_department_id: null,
            updated_at: NOW,
            visibility_grants: [],
            visibility_scope: "company",
          },
        ],
        page: 1,
        page_size: Number(url.searchParams.get("page_size") ?? 20),
        total: 1,
      });
    }

    // 个人偏好与显式记忆接口
    if (path === "/api/v1/me/preferences" && method === "GET") {
      return json(route, state.preferences);
    }
    if (path === "/api/v1/me/preferences" && method === "PUT") {
      const body = request.postDataJSON() as {
        custom_instructions: string;
        memory_enabled: boolean;
      };
      state.preferences = {
        custom_instructions: body.custom_instructions,
        memory_enabled: body.memory_enabled,
        updated_at: "2026-11-24T09:10:00Z",
      };
      return json(route, state.preferences);
    }
    if (path === "/api/v1/me/memories" && method === "GET") {
      return json(route, {
        items: state.memories,
        limit: 50,
        total: state.memories.length,
      });
    }
    if (path === "/api/v1/me/memories" && method === "POST") {
      const body = request.postDataJSON() as { content: string };
      const created: MemoryFixture = {
        content: body.content,
        created_at: "2026-11-24T09:11:00Z",
        id: `mem-${state.memories.length + 1}`,
        source: "manual",
        updated_at: "2026-11-24T09:11:00Z",
      };
      state.memories = [created, ...state.memories];
      return json(route, created, 201);
    }
    const memoryMatch = /^\/api\/v1\/me\/memories\/([^/]+)$/.exec(path);
    if (memoryMatch) {
      const memoryId = decodeURIComponent(memoryMatch[1]);
      if (method === "PATCH") {
        const body = request.postDataJSON() as { content: string };
        state.memories = state.memories.map((item) =>
          item.id === memoryId
            ? {
                ...item,
                content: body.content,
                updated_at: "2026-11-24T09:12:00Z",
              }
            : item,
        );
        const updated = state.memories.find((item) => item.id === memoryId);
        return updated
          ? json(route, updated)
          : json(route, { detail: "memory_not_found" }, 404);
      }
      if (method === "DELETE") {
        state.memories = state.memories.filter((item) => item.id !== memoryId);
        return route.fulfill({ status: 204 });
      }
    }

    // 项目接口
    if (path === "/api/v1/projects" && method === "GET") {
      return json(route, {
        items: state.projects,
        page: Number(url.searchParams.get("page") ?? 1),
        page_size: Number(url.searchParams.get("page_size") ?? 20),
        total: state.projects.length,
      });
    }
    const projectKbMatch = /^\/api\/v1\/projects\/([^/]+)\/knowledge-bases$/.exec(
      path,
    );
    if (projectKbMatch && method === "GET") {
      // 对齐 listProjectKnowledgeBases() 真实后端契约：直接返回 KnowledgeBase[] 数组。
      return json(route, []);
    }
    const projectMatch = /^\/api\/v1\/projects\/([^/]+)$/.exec(path);
    if (projectMatch) {
      const projectId = decodeURIComponent(projectMatch[1]);
      const project = state.projects.find((item) => item.id === projectId);
      if (method === "GET") {
        return project
          ? json(route, project)
          : json(route, { detail: "project_not_found" }, 404);
      }
      if (method === "PATCH" && project) {
        const body = request.postDataJSON() as Partial<ProjectFixture>;
        const updated: ProjectFixture = {
          ...project,
          ...body,
          updated_at: "2026-11-24T09:15:00Z",
        };
        state.projects = state.projects.map((item) =>
          item.id === projectId ? updated : item,
        );
        return json(route, updated);
      }
      if (method === "DELETE") {
        state.deletedProjectRequests.push(`${path}${url.search}`);
        const detach = url.searchParams.get("detach_sessions") === "true";
        state.projects = state.projects.filter((item) => item.id !== projectId);
        if (detach) {
          state.sessions = state.sessions.map((session) =>
            session.project_id === projectId
              ? { ...session, project_id: null }
              : session,
          );
        }
        return route.fulfill({ status: 204 });
      }
    }

    // 会话列表、创建与检索接口
    if (path === "/api/v1/sessions" && method === "GET") {
      const q = (url.searchParams.get("q") ?? "").trim();
      const projectId = url.searchParams.get("project_id");
      if (q) {
        state.searchQueries.push(q);
      }
      const filtered = state.sessions.filter((session) => {
        if (projectId && session.project_id !== projectId) {
          return false;
        }
        if (q && !(session.title ?? "").includes(q)) {
          return false;
        }
        return true;
      });
      return json(route, {
        items: filtered,
        page: Number(url.searchParams.get("page") ?? 1),
        page_size: Number(url.searchParams.get("page_size") ?? 20),
        total: filtered.length,
      });
    }
    if (path === "/api/v1/sessions" && method === "POST") {
      const body = request.postDataJSON() as {
        project_id: string | null;
        title: string | null;
      };
      const created: SessionFixture = {
        created_at: NOW,
        id: "sess-workbench-new",
        last_message_at: NOW,
        project_id: body.project_id,
        title: body.title ?? "上海出差申请协作",
        updated_at: NOW,
      };
      state.sessions = [created, ...state.sessions];
      return json(route, created, 201);
    }

    const sessionDetailMatch = /^\/api\/v1\/sessions\/([^/]+)$/.exec(path);
    if (sessionDetailMatch && method === "GET") {
      const sessionId = decodeURIComponent(sessionDetailMatch[1]);
      const session = state.sessions.find((item) => item.id === sessionId);
      return session
        ? json(route, session)
        : json(route, { detail: "session_not_found" }, 404);
    }

    const sessionFilesMatch = /^\/api\/v1\/sessions\/([^/]+)\/files$/.exec(
      path,
    );
    if (sessionFilesMatch) {
      const sessionId = decodeURIComponent(sessionFilesMatch[1]);
      if (method === "GET") {
        return json(route, {
          items: state.uploadedFiles,
          next_after_file_id: null,
        });
      }
      if (method === "POST") {
        const uploaded = {
          created_at: NOW,
          extracted_text_bytes: null,
          extraction_error_code: null,
          extraction_status: "none" as const,
          filename: "travel-plan.txt",
          id: "file-travel-plan",
          label: null,
          mime_type: "text/plain",
          session_id: sessionId,
          size_bytes: 42,
          source: "upload" as const,
        };
        state.uploadedFiles = [uploaded];
        return json(route, uploaded, 201);
      }
    }

    const sessionMessagesMatch =
      /^\/api\/v1\/sessions\/([^/]+)\/messages$/.exec(path);
    if (sessionMessagesMatch && method === "GET") {
      const sessionId = decodeURIComponent(sessionMessagesMatch[1]);
      const items: unknown[] = [
        {
          completed_at: NOW,
          created_at: NOW,
          id: "msg-user-1",
          parts: [
            {
              text: "请根据附件行程帮我发起上海出差审批单",
              type: "text",
            },
            ...state.uploadedFiles.map((file) => ({
              file_id: file.id,
              filename: file.filename,
              mime_type: file.mime_type,
              size_bytes: file.size_bytes,
              type: "file_ref",
            })),
          ],
          reply_to_message_id: null,
          role: "user",
          run_id: "run-workbench-1",
          sequence: 1,
          session_id: sessionId,
          status: "completed",
        },
      ];
      if (state.activeRunApproved) {
        items.push({
          completed_at: "2026-11-24T09:00:02Z",
          created_at: "2026-11-24T09:00:02Z",
          id: "msg-assistant-final",
          parts: [
            {
              text: "已为你提交上海出差审批单，审批单号 **TR-20261124-01**，请留意审批通知。",
              type: "text",
            },
          ],
          reply_to_message_id: "msg-user-1",
          role: "assistant",
          run_id: "run-workbench-1",
          sequence: 2,
          session_id: sessionId,
          status: "completed",
        });
      }
      return json(route, {
        items,
        next_before_sequence: null,
      });
    }

    const sessionRunsMatch = /^\/api\/v1\/sessions\/([^/]+)\/runs$/.exec(path);
    if (sessionRunsMatch) {
      const sessionId = decodeURIComponent(sessionRunsMatch[1]);
      if (method === "POST") {
        state.activeRunCreated = true;
        state.activeRunApproved = false;
        state.lastSubmittedRunBody = request.postDataJSON() as Record<
          string,
          unknown
        >;
        return json(route, buildRun(sessionId), 201);
      }
      if (method === "GET") {
        return json(route, {
          items: state.activeRunCreated ? [buildRun(sessionId)] : [],
          next_cursor: null,
        });
      }
    }

    const runDetailMatch = /^\/api\/v1\/sessions\/([^/]+)\/runs\/([^/]+)$/.exec(
      path,
    );
    if (runDetailMatch && method === "GET") {
      return json(route, buildRun(decodeURIComponent(runDetailMatch[1])));
    }

    const runToolCallsMatch =
      /^\/api\/v1\/sessions\/([^/]+)\/runs\/([^/]+)\/tool-calls$/.exec(path);
    if (runToolCallsMatch && method === "GET") {
      return json(route, {
        items: [
          {
            display_name: "发起差旅审批单",
            duration_ms: state.activeRunApproved ? 1200 : null,
            finished_at: state.activeRunApproved
              ? "2026-11-24T09:00:02Z"
              : null,
            id: "call-travel-1",
            model_name: "mcp__oa_create_travel_request",
            ordinal: 1,
            safe_error_code: null,
            started_at: NOW,
            status: state.activeRunApproved
              ? "completed"
              : "waiting_for_approval",
            tool_call_id: "call-travel-1",
            tool_id: "tool-oa-travel",
            tool_kind: "mcp",
            truncated: false,
          },
        ],
        next_after_ordinal: null,
      });
    }

    const approvalMatch =
      /^\/api\/v1\/sessions\/([^/]+)\/runs\/([^/]+)\/tool-calls\/([^/]+)\/approval$/.exec(
        path,
      );
    if (approvalMatch && method === "POST") {
      state.activeRunApproved = true;
      return json(route, buildRun(decodeURIComponent(approvalMatch[1])));
    }

    const streamMatch =
      /^\/api\/v1\/sessions\/([^/]+)\/runs\/([^/]+)\/stream$/.exec(path);
    if (streamMatch && method === "GET") {
      const run = buildRun(decodeURIComponent(streamMatch[1]));
      return route.fulfill({
        body: `event: snapshot\ndata: ${JSON.stringify({
          payload: {
            event_sequence: run.event_sequence,
            events: [],
            events_after_sequence: null,
            events_complete: true,
            messages: [],
            messages_before_sequence: null,
            messages_complete: true,
            pending_subagent_approvals: [],
            pending_subagent_inputs: [],
            report_message_id: run.report_message_id,
            status: run.status,
            subagent_tasks: [],
          },
        })}\n\n`,
        contentType: "text/event-stream",
        status: 200,
      });
    }

    return json(route, { detail: `workbench_e2e_mock_missing:${path}` }, 404);
  });

  return state;
}

test.describe("Athena v2 第二期核心工作台冒烟验收", () => {
  test("场景 1：Mock 多角色登录、单层侧栏折叠持久化与防抖检索会话", async ({
    page,
  }) => {
    const state = await installWorkbenchFixtures(page, {
      authenticated: false,
    });
    await page.setViewportSize({ height: 900, width: 1280 });

    await page.goto("/login");
    await expect(
      page.getByRole("heading", { level: 1, name: "登录工作台" }),
    ).toBeVisible();

    // 点击预置 Mock 员工卡登录并进入 /chat。
    await page.getByRole("button", { name: /林致远/ }).click();
    await expect(page).toHaveURL(/\/chat$/);
    await expect(
      page.getByRole("heading", { level: 1, name: "你好，林致远" }),
    ).toBeVisible();

    // 折叠桌面侧栏为 56px 精简图标栏（轮询等待宽度过渡动画收束），并刷新页面验证 localStorage 持久化。
    const sidebar = page.getByRole("complementary", { name: "工作区侧栏" });
    await expect(sidebar).toHaveAttribute("data-collapsed", "false");
    await sidebar.getByRole("button", { name: "收起侧栏" }).click();
    await expect(sidebar).toHaveAttribute("data-collapsed", "true");
    await expect
      .poll(async () => Math.round((await sidebar.boundingBox())?.width ?? 0))
      .toBe(56);

    await page.reload();
    await expect(sidebar).toHaveAttribute("data-collapsed", "true");
    await sidebar.getByRole("button", { name: "展开侧栏" }).click();
    await expect(sidebar).toHaveAttribute("data-collapsed", "false");

    // 在侧栏搜索框输入关键词触发防抖 GET /api/v1/sessions?q=偏差 检索，等待响应完成以确认 route 回调已落盘。
    const searchInput = sidebar.getByRole("textbox", {
      name: "搜索历史对话",
    });
    const searchResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "GET" &&
        new URL(response.url()).pathname === "/api/v1/sessions" &&
        new URL(response.url()).searchParams.get("q") === "偏差",
    );
    await searchInput.fill("偏差");
    await searchResponsePromise;
    expect(state.searchQueries).toContain("偏差");
    await expect(sidebar.getByRole("link", { name: "Q4 费用偏差复盘" })).toBeVisible();
  });

  test("场景 2：无项目日常对话、一体化输入器三态知识库与业务化审批卡片", async ({
    page,
  }) => {
    const state = await installWorkbenchFixtures(page);
    await page.setViewportSize({ height: 900, width: 1280 });
    await page.goto("/chat");

    // 切换场景化模型为「深度分析」。
    await page.getByRole("button", { name: /选择模型/ }).click();
    await page.getByRole("option", { name: /深度分析/ }).click();
    await expect(
      page.getByRole("button", { name: "选择模型：深度分析" }),
    ).toBeVisible();

    // 验证知识库范围三态切换（默认 -> 关闭 -> 显式勾选企业知识库）。
    const kbTrigger = page.getByRole("button", { name: /知识库范围/ });
    await kbTrigger.click();
    const kbSwitch = page.getByRole("switch", { name: "启用知识库检索" });
    await kbSwitch.click();
    await expect(kbTrigger).toHaveAttribute("data-knowledge-mode", "off");
    await kbSwitch.click();
    await expect(kbTrigger).toHaveAttribute("data-knowledge-mode", "default");
    await page.getByRole("checkbox", { name: "企业差旅与报销制度" }).click();
    await page.getByRole("button", { name: "完成" }).click();
    await expect(kbTrigger).toHaveAttribute("data-knowledge-mode", "explicit");

    // 附加任务文件并通过 Enter 键提交消息。
    await page.getByLabel("选择任务文件").setInputFiles({
      buffer: Buffer.from("目的地：上海\n预算：4800元"),
      mimeType: "text/plain",
      name: "travel-plan.txt",
    });
    await expect(page.getByText("travel-plan.txt")).toBeVisible();

    const composer = page.getByLabel("对话草稿");
    await composer.fill("请根据附件行程帮我发起上海出差审批单");
    await composer.press("Enter");

    await expect(page).toHaveURL(/\/chat\/sess-workbench-new$/);
    expect(state.lastSubmittedRunBody).toMatchObject({
      input_file_ids: ["file-travel-plan"],
      knowledge_base_ids: ["kb-company-travel"],
      model_profile_id: "model-analysis",
    });

    // 验证业务化审批卡片（中文名称、业务摘要、2 列中文键值表，不裸抛原始 JSON）。
    const approvalCard = page.getByRole("article", {
      name: "操作确认：发起差旅审批单",
    });
    await expect(approvalCard).toBeVisible();
    await expect(approvalCard).toContainText(
      "将为林致远提交 2026 年 11 月上海出差审批单，预计费用 4,800 元。",
    );
    await expect(approvalCard).toContainText("出差城市");
    await expect(approvalCard).toContainText("上海市");
    await expect(approvalCard).toContainText("预计费用");
    await expect(approvalCard).toContainText("4,800 元");
    await expect(approvalCard).not.toContainText('{"destination"');

    // 点击「确认执行」完成审批，核验折叠式执行进度条与最终回复。
    await approvalCard.getByRole("button", { name: "确认执行" }).click();
    await expect(approvalCard).toBeHidden();
    await expect(page.getByText(/TR-20261124-01/)).toBeVisible();

    const progressSection = page.getByRole("region", { name: "执行进度" });
    await expect(progressSection).toBeVisible();
    const toggleProgress = progressSection.getByRole("button", {
      name: /已完成 1 个步骤.*发起差旅审批单/,
    });
    await expect(toggleProgress).toHaveAttribute("aria-expanded", "false");
    await toggleProgress.click();
    await expect(toggleProgress).toHaveAttribute("aria-expanded", "true");
    await expect(
      progressSection.getByRole("list", { name: "工具调用步骤" }),
    ).toContainText("发起差旅审批单");
  });

  test("场景 3：个人工作偏好、显式记忆 CRUD 与非 local 账号资料页隐藏改密", async ({
    page,
  }) => {
    await installWorkbenchFixtures(page);
    await page.setViewportSize({ height: 900, width: 1280 });
    await page.goto("/preferences");

    // 保存长期工作指令。
    const instructionsInput = page.getByLabel("工作背景与输出偏好");
    await instructionsInput.fill(
      "我是财务分析专员，输出请结论先行并优先给出制度条款依据。",
    );
    await page.getByRole("button", { name: "保存指令" }).click();
    await expect(page.getByText("当前内容已与服务端同步")).toBeVisible();

    // 切换显式记忆总开关（关闭再开启）。
    const memorySwitch = page.getByRole("switch", { name: "启用显式记忆" });
    await expect(memorySwitch).toHaveAttribute("aria-checked", "true");
    await memorySwitch.click();
    await expect(memorySwitch).toHaveAttribute("aria-checked", "false");
    await expect(
      page.getByText(/已关闭对话自动记忆与注入/),
    ).toBeVisible();
    await memorySwitch.click();
    await expect(memorySwitch).toHaveAttribute("aria-checked", "true");

    // 新增、编辑、删除显式备忘。
    await page
      .getByLabel("手动添加显式备忘")
      .fill("差旅预算超过 5,000 元需追加分管副总审批。");
    await page.getByRole("button", { name: "添加备忘" }).click();
    await expect(
      page.getByText("差旅预算超过 5,000 元需追加分管副总审批。"),
    ).toBeVisible();

    await page
      .getByRole("button", { name: /编辑备忘：差旅预算超过/ })
      .click();
    const editInput = page.getByLabel("编辑备忘内容");
    await editInput.fill("差旅预算超过 8,000 元需追加分管副总审批。");
    await page.getByRole("button", { name: "保存", exact: true }).click();
    await expect(
      page.getByText("差旅预算超过 8,000 元需追加分管副总审批。"),
    ).toBeVisible();

    await page
      .getByRole("button", { name: /删除备忘：差旅预算超过/ })
      .click();
    const deleteDialog = page.getByRole("alertdialog", {
      name: "删除显式备忘",
    });
    await deleteDialog.getByRole("button", { name: "确认删除" }).click();
    // 先等待确认弹窗完全关闭，再在显式备忘列表内断言目标条目已移除，避免弹窗预览与列表双匹配触发 strict mode 冲突。
    await expect(deleteDialog).toBeHidden();
    await expect(
      page
        .getByRole("list", { name: "显式备忘列表" })
        .getByText("差旅预算超过 8,000 元需追加分管副总审批。"),
    ).toBeHidden();

    // 切至 /profile 验证企业组织名片且非 local 账号不展示本地改密表单。
    await page.getByRole("link", { name: "账号与安全" }).click();
    await expect(page).toHaveURL(/\/profile$/);
    await expect(page.getByText("财务分析部").first()).toBeVisible();
    await expect(page.getByText("E2048")).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "修改密码" }),
    ).toHaveCount(0);
  });

  test("场景 4：单页双栏项目文件夹与温和解绑删除项目", async ({
    page,
  }) => {
    const state = await installWorkbenchFixtures(page);
    await page.setViewportSize({ height: 900, width: 1280 });

    await page.goto("/projects/proj-q4-budget");
    await expect(page).toHaveURL(/\/projects\/proj-q4-budget$/);
    await expect(
      page.getByRole("heading", { level: 1, name: "Q4 预算专项" }),
    ).toBeVisible();

    // 更新项目专属背景指令（精确限定 textbox 角色，避免与同标题 section 冲突）。
    const projectInstructions = page.getByRole("textbox", {
      name: "专属背景指令",
    });
    await projectInstructions.fill(
      "优先对比 Q4 实际支出与年度预算口径，标注超支超过 5% 的科目。",
    );
    await page.getByRole("button", { name: "保存指令" }).click();
    await expect(page.getByText("专属背景指令已保存")).toBeVisible();

    // 点击删除项目，验证默认勾选「保留历史对话」并发起 DELETE ?detach_sessions=true。
    await page.getByRole("button", { name: "删除项目" }).click();
    const deleteDialog = page.getByRole("alertdialog", { name: "删除项目" });
    const detachCheckbox = deleteDialog.getByRole("checkbox", {
      name: /保留历史对话/,
    });
    await expect(detachCheckbox).toHaveAttribute("aria-checked", "true");
    await deleteDialog.getByRole("button", { name: "确认删除" }).click();

    await expect(page).toHaveURL(/\/projects$/);
    expect(state.deletedProjectRequests).toContain(
      "/api/v1/projects/proj-q4-budget?detach_sessions=true",
    );
  });

  test("场景 5：移动端 <1024px 窄屏 48px 顶栏、Sheet 抽屉导航自动收起与零横向溢出", async ({
    page,
  }) => {
    await installWorkbenchFixtures(page);
    await page.setViewportSize({ height: 844, width: 390 });
    await page.goto("/chat");

    const topBar = page.locator("header").first();
    await expect(topBar).toBeVisible();
    const headerBox = await topBar.boundingBox();
    expect(headerBox).not.toBeNull();
    expect(Math.round(headerBox!.height)).toBe(48);

    // 打开左侧 Sheet 抽屉并点击导航项，核验跳转后抽屉自动收起且主内容无横向溢出。
    await page.getByRole("button", { name: "打开导航菜单" }).click();
    const drawer = page.getByRole("dialog", { name: "工作区导航" });
    await expect(drawer).toBeVisible();

    await drawer.getByRole("link", { name: "技能库" }).click();
    await expect(page).toHaveURL(/\/skills$/);
    await expect(drawer).toBeHidden();
    await expectNoHorizontalOverflow(page);
  });
});

// 真实合法 1×1 PNG 图片字节（带完整 IHDR/IDAT/IEND CRC），供第三期多模态上传、缩略图与原图灯箱 E2E 使用。
const VALID_1X1_PNG_BUFFER = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
  "base64",
);

const MOCK_DEPT_ADMIN: MockUserFixture = {
  auth_provider: "mock",
  avatar_url: null,
  created_at: "2026-09-15T08:00:00Z",
  departments: [
    {
      id: "dept-finance",
      is_primary: true,
      name: "财务分析部",
    },
    {
      id: "dept-audit",
      is_primary: false,
      name: "预算审计部",
    },
  ],
  display_name: "周明岚",
  email: "zhou.minglan@example.com",
  employee_no: "E1008",
  id: "mock-dept-admin-1",
  job_title: "财务分析部负责人",
  managed_department_ids: ["dept-finance", "dept-audit"],
  role: "dept_admin",
};

interface Phase3SessionFileFixture {
  created_at: string;
  extracted_text_bytes: number | null;
  extraction_error_code: string | null;
  extraction_status: "none" | "ready" | "failed";
  filename: string;
  id: string;
  label: string | null;
  mime_type: string;
  session_id: string;
  size_bytes: number;
  source: "upload" | "artifact";
}

interface Phase3SkillGrantFixture {
  created_at: string;
  id: string;
  include_subdepartments: boolean;
  target_id: string;
  target_type: "department" | "role";
}

interface Phase3AdminSkillFixture {
  current_version_id: string;
  current_version_label: string;
  dependency_check: {
    checked_at: string;
    items: unknown[];
    python_implementation: string;
    python_version: string;
    status: "satisfied";
  };
  description: string;
  enabled: boolean;
  featured: boolean;
  format: "markdown" | "package";
  has_scripts: boolean;
  id: string;
  name: string;
  owner_department_id: string | null;
  revision: number;
  scope: "company" | "department";
  script_requires_approval: boolean;
  slug: string;
  starter_prompt: string | null;
  updated_at: string;
  versions: Array<{
    created_at: string;
    dependencies: string[];
    dependency_check: {
      checked_at: string;
      items: unknown[];
      python_implementation: string;
      python_version: string;
      status: "satisfied";
    };
    description: string;
    files: Array<{
      path: string;
      sha256: string;
      size_bytes: number;
      text: boolean;
    }>;
    guide: string;
    id: string;
    label: string;
    name: string;
    ordinal: number;
    package_sha256: string;
    scripts: unknown[];
    skill_id: string;
    slug: string;
  }>;
  visibility_grants: Phase3SkillGrantFixture[];
}

interface Phase3KnowledgeBaseFixture {
  created_at: string;
  description: string | null;
  id: string;
  name: string;
  owner_department_id: string | null;
  updated_at: string;
  visibility_grants: Phase3SkillGrantFixture[];
  visibility_scope: "company" | "department" | "personal";
}

interface Phase3KnowledgeDocumentFixture {
  active_version_id: string | null;
  created_at: string;
  id: string;
  knowledge_base_id: string;
  latest_version: {
    chunk_count: number;
    completed_at: string | null;
    created_at: string;
    document_id: string;
    error_code: string | null;
    id: string;
    state: "queued" | "processing" | "ready" | "failed";
    warnings: string[];
  } | null;
  pending_version_id: string | null;
  title: string;
  updated_at: string;
}

/** 从 multipart/form-data 字节流中安全解析上传文件名，用于确定性区分 PDF 与 PNG 附件。 */
function extractMultipartFilename(raw: Buffer | null): string {
  if (!raw) {
    return "upload.bin";
  }
  const text = raw.toString("utf8");
  const match = /filename="([^"]+)"/.exec(text);
  return match?.[1] ?? "upload.bin";
}

/**
 * 安装第三期多模态、技能双形态与知识库跨部门共享确定性 HTTP fixture，
 * 严格遵循 docs/phase3-multimodal-and-skills-api-contract.md 与一期企业契约。
 */
async function installPhase3Fixtures(
  page: Page,
  initialUser: MockUserFixture,
) {
  const csvContent =
    "部门,Q3预算(万元),Q3实际(万元),偏差率\n财务分析部,120.00,114.50,-4.58%\n平台工程部,260.00,255.00,-1.92%\n";
  const csvBytes = Buffer.byteLength(csvContent, "utf8");
  const artifactCsvFile: Phase3SessionFileFixture = {
    created_at: "2026-11-24T09:00:02Z",
    extracted_text_bytes: null,
    extraction_error_code: null,
    extraction_status: "none",
    filename: "dept-budget-summary.csv",
    id: "file-artifact-csv",
    label: "部门预算汇总表",
    mime_type: "text/csv",
    session_id: "sess-phase3-mm",
    size_bytes: csvBytes,
    source: "artifact",
  };

  const state = {
    adminSkills: [] as Phase3AdminSkillFixture[],
    contentRequests: [] as string[],
    createdKnowledgeBodies: [] as Array<Record<string, unknown>>,
    createdMarkdownSkillBodies: [] as Array<Record<string, unknown>>,
    createdMarkdownSkillIdempotencyKeys: [] as Array<string | null>,
    currentUser: initialUser,
    knowledgeBases: [] as Phase3KnowledgeBaseFixture[],
    knowledgeDocumentsByBase: new Map<
      string,
      Phase3KnowledgeDocumentFixture[]
    >(),
    lastSubmittedRunBody: null as Record<string, unknown> | null,
    previewRequests: [] as string[],
    runSubmitted: false,
    sessionFiles: [] as Phase3SessionFileFixture[],
    sessions: [] as SessionFixture[],
    submittedInputFileIds: [] as string[],
  };

  const orgDepartments = [
    {
      created_at: "2026-09-01T00:00:00Z",
      external_id: "ext-root",
      id: "dept-root",
      name: "集团总部",
      order_index: 1,
      parent_id: null,
      path: "/dept-root/",
      status: "active",
      synced_at: NOW,
      updated_at: NOW,
    },
    {
      created_at: "2026-09-01T00:00:00Z",
      external_id: "ext-finance",
      id: "dept-finance",
      name: "财务分析部",
      order_index: 2,
      parent_id: "dept-root",
      path: "/dept-root/dept-finance/",
      status: "active",
      synced_at: NOW,
      updated_at: NOW,
    },
    {
      created_at: "2026-09-01T00:00:00Z",
      external_id: "ext-platform",
      id: "dept-platform",
      name: "平台工程部",
      order_index: 3,
      parent_id: "dept-root",
      path: "/dept-root/dept-platform/",
      status: "active",
      synced_at: NOW,
      updated_at: NOW,
    },
    {
      created_at: "2026-09-01T00:00:00Z",
      external_id: "ext-audit",
      id: "dept-audit",
      name: "预算审计部",
      order_index: 4,
      parent_id: "dept-root",
      path: "/dept-root/dept-audit/",
      status: "active",
      synced_at: NOW,
      updated_at: NOW,
    },
  ];

  const buildCompletedRun = (sessionId: string) => ({
    assistant_message_id: "msg-assistant-mm-1",
    cancel_requested_at: null,
    context_cutoff_sequence: 1,
    created_at: NOW,
    depth: 0,
    event_sequence: 4,
    execution_epoch: 1,
    finished_at: "2026-11-24T09:00:02Z",
    first_started_at: NOW,
    id: "run-phase3-mm-1",
    input_file_ids: state.sessionFiles
      .filter((file) => file.source === "upload")
      .map((file) => file.id),
    input_interactions: [],
    knowledge_base_ids: null,
    model_config_version_id: "model-analysis",
    model_snapshot: {
      id: "model-analysis",
      model: "athena-pro-analysis",
      name: "Athena Pro Analysis",
      scenario_label: "深度分析",
      supports_vision: true,
      version: 1,
    },
    parent_run_id: null,
    parent_tool_call_id: null,
    pending_approvals: [],
    pending_input: null,
    pending_subagent_approvals: [],
    pending_subagent_inputs: [],
    prompt_template_version: "v1",
    queued_at: NOW,
    replayed: false,
    report_message_id: "msg-assistant-mm-1",
    retry_of_run_id: null,
    retry_warning: null,
    root_run_id: null,
    safe_error_code: null,
    session_id: sessionId,
    skill_snapshot: null,
    status: "completed",
    subagent_name: null,
    subagent_profile_id: null,
    subagent_slug: null,
    subagent_tasks: [],
    trigger_message_id: "msg-user-mm-1",
    updated_at: "2026-11-24T09:00:02Z",
    usage: {
      answer_tokens: 164,
      attempts: [],
      complete: true,
      compression_tokens: 0,
      input_tokens: 512,
      output_tokens: 164,
      total_tokens: 676,
    },
  });

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === "/api/v1/auth/me" && method === "GET") {
      return json(route, state.currentUser);
    }

    if (path === "/api/v1/models" && method === "GET") {
      return json(route, {
        default_model_id: "model-general",
        items: [
          {
            id: "model-general",
            is_default: true,
            name: "Athena General",
            scenario_description: "日常问答、邮件起草与制度检索",
            scenario_label: "通用办公",
            supports_vision: false,
          },
          {
            id: "model-analysis",
            is_default: false,
            name: "Athena Pro Analysis",
            scenario_description: "复杂财务分析、长文档比对与图表识图解读",
            scenario_label: "深度分析",
            supports_vision: true,
          },
        ],
      });
    }

    if (path === "/api/v1/projects" && method === "GET") {
      return json(route, {
        items: [],
        page: 1,
        page_size: Number(url.searchParams.get("page_size") ?? 20),
        total: 0,
      });
    }

    // 组织部门目录（超管配置知识库跨部门授权使用）。
    if (path === "/api/v1/admin/org/departments" && method === "GET") {
      return json(route, { items: orgDepartments });
    }

    // 普通用户可见技能目录（遵循契约 3.3：featured 推荐项置顶且带 starter_prompt）。
    if (path === "/api/v1/skills" && method === "GET") {
      return json(route, {
        items: state.adminSkills.filter((skill) => skill.enabled),
      });
    }

    // 管理端技能目录与在线 Markdown 创建 / 详情 / 版本历史（契约 3.5 / 3.6）。
    if (path === "/api/v1/admin/skills" && method === "GET") {
      return json(route, { items: state.adminSkills });
    }
    if (path === "/api/v1/admin/skills/markdown" && method === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.createdMarkdownSkillBodies.push(body);
      state.createdMarkdownSkillIdempotencyKeys.push(
        request.headers()["idempotency-key"] ?? null,
      );
      const rawGrants = Array.isArray(body.visibility_grants)
        ? (body.visibility_grants as Array<{
            include_subdepartments: boolean;
            target_id: string;
            target_type: "department" | "role";
          }>)
        : [];
      const skillId = "skill-md-fin-audit";
      const versionId = "skillver-md-fin-v1";
      const slug =
        typeof body.slug === "string" && body.slug.trim()
          ? body.slug.trim()
          : "md-fin-audit";
      const name = String(body.name ?? "");
      const description = String(body.description ?? "");
      const guide = String(body.guide ?? "");
      const ownerDepartmentId =
        typeof body.owner_department_id === "string"
          ? body.owner_department_id
          : null;
      const dependencyCheck = {
        checked_at: NOW,
        items: [],
        python_implementation: "markdown-only",
        python_version: "not-checked",
        status: "satisfied" as const,
      };
      const createdSkill: Phase3AdminSkillFixture = {
        current_version_id: versionId,
        current_version_label: "v1",
        dependency_check: dependencyCheck,
        description,
        enabled: Boolean(body.enabled ?? true),
        featured: Boolean(body.featured ?? false),
        format: "markdown",
        has_scripts: false,
        id: skillId,
        name,
        owner_department_id: ownerDepartmentId,
        revision: 1,
        scope:
          body.scope === "department" ? "department" : "company",
        script_requires_approval: false,
        slug,
        starter_prompt:
          typeof body.starter_prompt === "string" ? body.starter_prompt : null,
        updated_at: NOW,
        versions: [
          {
            created_at: NOW,
            dependencies: [],
            dependency_check: dependencyCheck,
            description,
            files: [
              {
                path: "SKILL.md",
                sha256: "a1b2c3d4e5f60718293a4b5c6d7e8f90",
                size_bytes: Buffer.byteLength(guide, "utf8"),
                text: true,
              },
            ],
            guide,
            id: versionId,
            label: "v1",
            name,
            ordinal: 1,
            package_sha256: "f0e1d2c3b4a5968778695a4b3c2d1e0f",
            scripts: [],
            skill_id: skillId,
            slug,
          },
        ],
        visibility_grants: rawGrants.map((grant, index) => ({
          ...grant,
          created_at: NOW,
          id: `grant-skill-${index + 1}`,
        })),
      };
      state.adminSkills = [createdSkill, ...state.adminSkills];
      return json(route, createdSkill, 201);
    }

    const adminSkillVersionsMatch =
      /^\/api\/v1\/admin\/skills\/([^/]+)\/versions$/.exec(path);
    if (adminSkillVersionsMatch && method === "GET") {
      const skillId = decodeURIComponent(adminSkillVersionsMatch[1]);
      const matched = state.adminSkills.find((item) => item.id === skillId);
      const versions = matched?.versions ?? [];
      return json(route, {
        items: versions,
        page: 1,
        page_size: 20,
        total: versions.length,
      });
    }
    const adminSkillDetailMatch = /^\/api\/v1\/admin\/skills\/([^/]+)$/.exec(
      path,
    );
    if (adminSkillDetailMatch && method === "GET") {
      const skillId = decodeURIComponent(adminSkillDetailMatch[1]);
      const matched = state.adminSkills.find((item) => item.id === skillId);
      return matched
        ? json(route, matched)
        : json(route, { detail: "skill_not_found" }, 404);
    }

    // 知识库与双栏 Markdown 文本导入接口。
    if (path === "/api/v1/knowledge-bases" && method === "GET") {
      return json(route, {
        items: state.knowledgeBases,
        page: Number(url.searchParams.get("page") ?? 1),
        page_size: Number(url.searchParams.get("page_size") ?? 12),
      });
    }
    if (path === "/api/v1/knowledge-bases" && method === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      state.createdKnowledgeBodies.push(body);
      const rawGrants = Array.isArray(body.visibility_grants)
        ? (body.visibility_grants as Array<{
            include_subdepartments: boolean;
            target_id: string;
            target_type: "department" | "role";
          }>)
        : [];
      const createdBase: Phase3KnowledgeBaseFixture = {
        created_at: NOW,
        description:
          typeof body.description === "string" ? body.description : null,
        id: "kb-phase3-shared",
        name: String(body.name ?? ""),
        owner_department_id:
          typeof body.owner_department_id === "string"
            ? body.owner_department_id
            : null,
        updated_at: NOW,
        visibility_grants: rawGrants.map((grant, index) => ({
          ...grant,
          created_at: NOW,
          id: `grant-kb-${index + 1}`,
        })),
        visibility_scope:
          body.visibility_scope === "department" ||
          body.visibility_scope === "company"
            ? body.visibility_scope
            : "personal",
      };
      state.knowledgeBases = [createdBase, ...state.knowledgeBases];
      state.knowledgeDocumentsByBase.set(createdBase.id, []);
      return json(route, createdBase, 201);
    }
    const kbTextImportMatch =
      /^\/api\/v1\/knowledge-bases\/([^/]+)\/documents\/text$/.exec(path);
    if (kbTextImportMatch && method === "POST") {
      const baseId = decodeURIComponent(kbTextImportMatch[1]);
      const body = request.postDataJSON() as { text: string; title: string };
      const existing = state.knowledgeDocumentsByBase.get(baseId) ?? [];
      const docId = `doc-phase3-${existing.length + 1}`;
      const versionId = `ver-phase3-${existing.length + 1}`;
      const createdDoc: Phase3KnowledgeDocumentFixture = {
        active_version_id: versionId,
        created_at: NOW,
        id: docId,
        knowledge_base_id: baseId,
        latest_version: {
          chunk_count: 4,
          completed_at: "2026-11-24T09:01:00Z",
          created_at: NOW,
          document_id: docId,
          error_code: null,
          id: versionId,
          state: "ready",
          warnings: [],
        },
        pending_version_id: null,
        title: body.title,
        updated_at: "2026-11-24T09:01:00Z",
      };
      state.knowledgeDocumentsByBase.set(baseId, [createdDoc, ...existing]);
      return json(
        route,
        {
          document_id: docId,
          state: "queued",
          status_url: `/api/v1/knowledge-bases/${encodeURIComponent(baseId)}/documents/${encodeURIComponent(docId)}`,
          version_id: versionId,
        },
        202,
      );
    }
    const kbDocumentsMatch =
      /^\/api\/v1\/knowledge-bases\/([^/]+)\/documents$/.exec(path);
    if (kbDocumentsMatch && method === "GET") {
      const baseId = decodeURIComponent(kbDocumentsMatch[1]);
      const docs = state.knowledgeDocumentsByBase.get(baseId) ?? [];
      return json(route, {
        items: docs,
        page: 1,
        page_size: Number(url.searchParams.get("page_size") ?? 10),
      });
    }
    const kbDetailMatch = /^\/api\/v1\/knowledge-bases\/([^/]+)$/.exec(path);
    if (kbDetailMatch && method === "GET") {
      const baseId = decodeURIComponent(kbDetailMatch[1]);
      const matched = state.knowledgeBases.find((item) => item.id === baseId);
      return matched
        ? json(route, matched)
        : json(route, { detail: "knowledge_base_not_found" }, 404);
    }

    // 多模态对话会话、附件提取、位图安全预览与分段读取接口（契约 1.1–1.4）。
    if (path === "/api/v1/sessions" && method === "GET") {
      return json(route, {
        items: state.sessions,
        page: 1,
        page_size: Number(url.searchParams.get("page_size") ?? 20),
        total: state.sessions.length,
      });
    }
    if (path === "/api/v1/sessions" && method === "POST") {
      const created: SessionFixture = {
        created_at: NOW,
        id: "sess-phase3-mm",
        last_message_at: NOW,
        project_id: null,
        title: "Q3 财报多模态分析",
        updated_at: NOW,
      };
      state.sessions = [created, ...state.sessions];
      return json(route, created, 201);
    }
    const sessionDetailMatch = /^\/api\/v1\/sessions\/([^/]+)$/.exec(path);
    if (sessionDetailMatch && method === "GET") {
      const sessionId = decodeURIComponent(sessionDetailMatch[1]);
      const matched = state.sessions.find((item) => item.id === sessionId);
      return matched
        ? json(route, matched)
        : json(route, { detail: "session_not_found" }, 404);
    }

    const filePreviewMatch =
      /^\/api\/v1\/sessions\/([^/]+)\/files\/([^/]+)\/preview$/.exec(path);
    if (filePreviewMatch && method === "GET") {
      state.previewRequests.push(path);
      return route.fulfill({
        body: VALID_1X1_PNG_BUFFER,
        contentType: "image/png",
        headers: {
          "Cache-Control": "private, no-store",
          "Content-Security-Policy": "default-src 'none'; sandbox",
          "X-Content-Type-Options": "nosniff",
        },
        status: 200,
      });
    }

    const fileContentMatch =
      /^\/api\/v1\/sessions\/([^/]+)\/files\/([^/]+)\/content$/.exec(path);
    if (fileContentMatch && method === "GET") {
      const fileId = decodeURIComponent(fileContentMatch[2]);
      state.contentRequests.push(path);
      if (fileId === artifactCsvFile.id) {
        return json(route, {
          content: csvContent,
          eof: true,
          file: artifactCsvFile,
          next_offset: csvBytes,
          offset: 0,
          source_kind: "raw_text",
          truncated: false,
        });
      }
      const matchedFile = state.sessionFiles.find((item) => item.id === fileId);
      if (matchedFile) {
        const extracted = "## 第 1 页\n2026 年第三季度财务分析概要。";
        return json(route, {
          content: extracted,
          eof: true,
          file: matchedFile,
          next_offset: Buffer.byteLength(extracted, "utf8"),
          offset: 0,
          source_kind: "extracted_document",
          truncated: false,
        });
      }
      return json(route, { detail: "session_file_not_found" }, 404);
    }

    const sessionFilesMatch = /^\/api\/v1\/sessions\/([^/]+)\/files$/.exec(
      path,
    );
    if (sessionFilesMatch) {
      const sessionId = decodeURIComponent(sessionFilesMatch[1]);
      if (method === "GET") {
        return json(route, {
          items: state.sessionFiles,
          next_after_file_id: null,
        });
      }
      if (method === "POST") {
        const filename = extractMultipartFilename(request.postDataBuffer());
        const isPdf = filename.toLowerCase().endsWith(".pdf");
        const uploaded: Phase3SessionFileFixture = isPdf
          ? {
              created_at: NOW,
              extracted_text_bytes: 4096,
              extraction_error_code: null,
              extraction_status: "ready",
              filename,
              id: `file-pdf-${state.sessionFiles.length + 1}`,
              label: null,
              mime_type: "application/pdf",
              session_id: sessionId,
              size_bytes: 1280,
              source: "upload",
            }
          : {
              created_at: NOW,
              extracted_text_bytes: null,
              extraction_error_code: null,
              extraction_status: "none",
              filename,
              id: `file-png-${state.sessionFiles.length + 1}`,
              label: null,
              mime_type: "image/png",
              session_id: sessionId,
              size_bytes: VALID_1X1_PNG_BUFFER.length,
              source: "upload",
            };
        state.sessionFiles = [...state.sessionFiles, uploaded];
        return json(route, uploaded, 201);
      }
    }

    const sessionMessagesMatch =
      /^\/api\/v1\/sessions\/([^/]+)\/messages$/.exec(path);
    if (sessionMessagesMatch && method === "GET") {
      const sessionId = decodeURIComponent(sessionMessagesMatch[1]);
      if (!state.runSubmitted) {
        return json(route, { items: [], next_before_sequence: null });
      }
      const submittedIdSet = new Set(state.submittedInputFileIds);
      const uploadedFiles = state.sessionFiles.filter(
        (file) => file.source === "upload" && submittedIdSet.has(file.id),
      );
      return json(route, {
        items: [
          {
            completed_at: NOW,
            created_at: NOW,
            id: "msg-user-mm-1",
            parts: [
              {
                text: "请结合财报 PDF 与营收走势截图生成各部门预算汇总表。",
                type: "text",
              },
              ...uploadedFiles.map((file) => ({
                file_id: file.id,
                filename: file.filename,
                mime_type: file.mime_type,
                size_bytes: file.size_bytes,
                type: "file_ref",
              })),
            ],
            reply_to_message_id: null,
            role: "user",
            run_id: "run-phase3-mm-1",
            sequence: 1,
            session_id: sessionId,
            status: "completed",
          },
          {
            completed_at: "2026-11-24T09:00:02Z",
            created_at: "2026-11-24T09:00:02Z",
            id: "msg-assistant-mm-1",
            parts: [
              {
                text: "已完成 Q3 财报 PDF 文本解析与截图解读，并整理出各部门预算汇总表，请点击下方卡片在线预览或下载。",
                type: "text",
              },
              {
                artifacts: [
                  {
                    file_id: artifactCsvFile.id,
                    filename: artifactCsvFile.filename,
                    label: artifactCsvFile.label,
                    mime_type: artifactCsvFile.mime_type,
                    size_bytes: artifactCsvFile.size_bytes,
                  },
                ],
                output: null,
                status: "completed",
                tool_call_id: "call-export-csv",
                type: "tool_result",
              },
            ],
            reply_to_message_id: "msg-user-mm-1",
            role: "assistant",
            run_id: "run-phase3-mm-1",
            sequence: 2,
            session_id: sessionId,
            status: "completed",
          },
        ],
        next_before_sequence: null,
      });
    }

    const sessionRunsMatch = /^\/api\/v1\/sessions\/([^/]+)\/runs$/.exec(path);
    if (sessionRunsMatch) {
      const sessionId = decodeURIComponent(sessionRunsMatch[1]);
      if (method === "POST") {
        state.runSubmitted = true;
        state.lastSubmittedRunBody = request.postDataJSON() as Record<
          string,
          unknown
        >;
        const inputIds = Array.isArray(state.lastSubmittedRunBody.input_file_ids)
          ? (state.lastSubmittedRunBody.input_file_ids as string[])
          : [];
        state.submittedInputFileIds = [...inputIds];
        if (
          !state.sessionFiles.some((file) => file.id === artifactCsvFile.id)
        ) {
          state.sessionFiles = [...state.sessionFiles, artifactCsvFile];
        }
        return json(route, buildCompletedRun(sessionId), 201);
      }
      if (method === "GET") {
        return json(route, {
          items: state.runSubmitted ? [buildCompletedRun(sessionId)] : [],
          next_cursor: null,
        });
      }
    }

    const runDetailMatch = /^\/api\/v1\/sessions\/([^/]+)\/runs\/([^/]+)$/.exec(
      path,
    );
    if (runDetailMatch && method === "GET") {
      return json(
        route,
        buildCompletedRun(decodeURIComponent(runDetailMatch[1])),
      );
    }

    const runToolCallsMatch =
      /^\/api\/v1\/sessions\/([^/]+)\/runs\/([^/]+)\/tool-calls$/.exec(path);
    if (runToolCallsMatch && method === "GET") {
      return json(route, { items: [], next_after_ordinal: null });
    }

    const streamMatch =
      /^\/api\/v1\/sessions\/([^/]+)\/runs\/([^/]+)\/stream$/.exec(path);
    if (streamMatch && method === "GET") {
      const run = buildCompletedRun(decodeURIComponent(streamMatch[1]));
      return route.fulfill({
        body: `event: snapshot\ndata: ${JSON.stringify({
          payload: {
            event_sequence: run.event_sequence,
            events: [],
            events_after_sequence: null,
            events_complete: true,
            messages: [],
            messages_before_sequence: null,
            messages_complete: true,
            pending_subagent_approvals: [],
            pending_subagent_inputs: [],
            report_message_id: run.report_message_id,
            status: run.status,
            subagent_tasks: [],
          },
        })}\n\n`,
        contentType: "text/event-stream",
        status: 200,
      });
    }

    return json(route, { detail: `phase3_e2e_mock_missing:${path}` }, 404);
  });

  return state;
}

test.describe("Athena v2 第三期多模态、技能双形态与知识库共享冒烟验收", () => {
  test("场景 6：PDF 解析状态芯片、方案 A2 非识图一键切换、消息流原图灯箱与交付产物在线预览", async ({
    page,
  }) => {
    const state = await installPhase3Fixtures(page, MOCK_MEMBER);
    await page.setViewportSize({ height: 900, width: 1280 });
    await page.goto("/chat");

    // 1. 首页默认模型为不支持识图的 Athena General，上传 1 个 PDF 与 1 张真实 1×1 PNG 截图。
    await expect(
      page.getByRole("button", { name: "选择模型：通用办公" }),
    ).toBeVisible();
    await page.getByLabel("选择任务文件").setInputFiles([
      {
        buffer: Buffer.from("%PDF-1.4\n% Athena Q3 Financial Report\n"),
        mimeType: "application/pdf",
        name: "q3-report.pdf",
      },
      {
        buffer: VALID_1X1_PNG_BUFFER,
        mimeType: "image/png",
        name: "revenue-chart.png",
      },
    ]);

    const draftAttachmentBar = page.getByLabel("已附加资料与技能");
    await expect(draftAttachmentBar).toContainText("q3-report.pdf");
    await expect(draftAttachmentBar).toContainText("revenue-chart.png");
    await expect(
      draftAttachmentBar.getByTestId("composer-attachment-thumb"),
    ).toBeVisible();

    // 2. 输入草稿文本后校验方案 A2：展示琥珀色非识图提示条，但发送按钮绝不禁用。
    await page
      .getByLabel("对话草稿")
      .fill("请结合财报 PDF 与营收走势截图生成各部门预算汇总表。");
    const visionBanner = page.getByTestId("composer-vision-unsupported-banner");
    await expect(visionBanner).toBeVisible();
    await expect(visionBanner).toContainText("当前模型暂不支持识图");
    const sendButton = page.getByRole("button", { name: "发送消息" });
    await expect(sendButton).toBeEnabled();

    // 3. 点击一键切换识图模型按钮，断言切至支持视觉的 Athena Pro Analysis（深度分析）且提示条消失。
    await visionBanner
      .getByRole("button", { name: /一键切至「深度分析」/ })
      .click();
    await expect(
      page.getByRole("button", { name: "选择模型：深度分析" }),
    ).toBeVisible();
    await expect(visionBanner).toBeHidden();

    // 4. 提交消息进入 /chat/:sessionId，验证 Run 携带识图模型与两份附件。
    await sendButton.click();
    await expect(page).toHaveURL(/\/chat\/sess-phase3-mm$/);
    expect(state.lastSubmittedRunBody).toMatchObject({
      model_profile_id: "model-analysis",
    });
    expect(
      (state.lastSubmittedRunBody?.input_file_ids as string[] | undefined)
        ?.length,
    ).toBe(2);

    // 5. 校验消息流 PDF「已解析」状态微标，并在会话输入器上传即时解析 PDF 校验附件条「已解析」芯片。
    const userMessage = page.getByRole("article", {
      name: "你的消息，序号 1",
    });
    await expect(userMessage).toContainText("q3-report.pdf");
    await expect(userMessage).toContainText("已解析");

    await page.getByLabel("选择会话文件").setInputFiles({
      buffer: Buffer.from("%PDF-1.4\n% Appendix Budget Table\n"),
      mimeType: "application/pdf",
      name: "appendix-budget.pdf",
    });
    const sessionAttachmentBar = page.getByLabel("已附加资料与技能");
    await expect(sessionAttachmentBar).toContainText("appendix-budget.pdf");
    await expect(sessionAttachmentBar).toContainText("已解析");

    // 6. 点击用户消息流中的图片缩略图打开原图灯箱，按 Escape 关闭后断言焦点回归原触发按钮。
    const imageTrigger = userMessage.getByRole("button", {
      name: "查看大图：revenue-chart.png",
    });
    await expect(imageTrigger).toBeVisible();
    await imageTrigger.click();
    const previewDialog = page.getByTestId("file-preview-dialog");
    await expect(previewDialog).toBeVisible();
    const lightboxImage = previewDialog.getByRole("img", {
      name: "revenue-chart.png",
    });
    await expect(lightboxImage).toBeVisible();
    await expect
      .poll(() =>
        lightboxImage.evaluate((img: HTMLImageElement) => img.naturalWidth),
      )
      .toBeGreaterThan(0);
    const pngFile = state.sessionFiles.find(
      (file) => file.filename === "revenue-chart.png",
    );
    expect(pngFile).toBeDefined();
    expect(state.previewRequests).toContain(
      `/api/v1/sessions/sess-phase3-mm/files/${pngFile!.id}/preview`,
    );
    await page.keyboard.press("Escape");
    await expect(previewDialog).toBeHidden();
    await expect(imageTrigger).toBeFocused();

    // 7. 点击助手交付产物 CSV 卡片「在线预览」，断言拉取 /content 并正确渲染表格，校验零横向溢出。
    const artifactRegion = page.getByRole("region", { name: "交付产物" });
    await expect(artifactRegion).toContainText("部门预算汇总表");
    await artifactRegion
      .getByRole("button", { name: "在线预览：部门预算汇总表" })
      .click();
    await expect(previewDialog).toBeVisible();
    const csvTable = previewDialog.getByTestId("csv-preview-table");
    await expect(csvTable).toBeVisible();
    await expect(csvTable).toContainText("部门");
    await expect(csvTable).toContainText("财务分析部");
    await expect(csvTable).toContainText("114.50");
    expect(state.contentRequests).toContain(
      "/api/v1/sessions/sess-phase3-mm/files/file-artifact-csv/content",
    );

    await page.keyboard.press("Escape");
    await expect(previewDialog).toBeHidden();
    await expectNoHorizontalOverflow(page);
  });

  test("场景 7：部门管理员在线发布 Markdown 模板技能并推荐至首页 → 员工首页快捷应用直达", async ({
    page,
  }) => {
    const state = await installPhase3Fixtures(page, MOCK_DEPT_ADMIN);
    await page.setViewportSize({ height: 900, width: 1280 });
    await page.goto("/admin/skills");

    await expect(
      page.getByRole("heading", { level: 1, name: "部门技能管理" }),
    ).toBeVisible();

    // 1. 打开「新建在线模板技能」弹窗，填写元数据、Markdown 正文预览、跨管辖部门共享与首页推荐。
    await page
      .locator("main header")
      .first()
      .getByRole("button", { name: "新建在线模板技能" })
      .click();
    const createDialog = page.getByTestId("admin-skill-markdown-dialog");
    await expect(createDialog).toBeVisible();

    await createDialog.getByLabel("技能名称").fill("财务报销合规审核助手");
    await createDialog
      .getByLabel("技能简介")
      .fill("依据公司差旅与费用制度逐项核查报销单据合规性");
    await createDialog.getByRole("combobox", { name: "归属部门" }).click();
    await page.getByRole("option", { name: "财务分析部" }).click();

    // 部门管理员仅可追加自身管辖部门，不渲染角色授权入口。
    const grantsEditor = createDialog.getByTestId("visibility-grants-editor");
    await expect(grantsEditor).toBeVisible();
    await expect(grantsEditor.getByLabel("附加授权类型")).toHaveCount(0);
    await grantsEditor
      .getByRole("combobox", { name: "选择附加共享部门" })
      .click();
    await page.getByRole("option", { name: "预算审计部" }).click();
    await grantsEditor.getByRole("button", { name: "添加共享" }).click();

    await createDialog
      .getByLabel("技能指南正文（Markdown）")
      .fill("# 审核步骤\n\n- 核对电子发票真伪与抬头\n- 校验一线城市住宿补贴上限");
    await createDialog.getByRole("tab", { name: "仅预览" }).click();
    const previewPane = createDialog.getByTestId("markdown-split-preview");
    await expect(
      previewPane.getByRole("heading", { name: "审核步骤" }),
    ).toBeVisible();
    await expect(previewPane).toContainText("核对电子发票真伪与抬头");

    await createDialog
      .getByRole("switch", { name: "推荐至工作台首页快捷应用" })
      .click();
    const starterPromptText =
      "请上传你的差旅或费用报销单截图/PDF，我将按公司最新制度逐项核对合规性。";
    await createDialog
      .getByLabel("首页默认引导语（可选）")
      .fill(starterPromptText);

    await createDialog
      .getByRole("button", { name: "创建并启用模板技能" })
      .click();
    await expect(createDialog).toBeHidden();

    // 2. 断言命中 POST /api/v1/admin/skills/markdown 且载荷包含 featured、starter_prompt 与 visibility_grants。
    expect(state.createdMarkdownSkillBodies).toHaveLength(1);
    expect(state.createdMarkdownSkillIdempotencyKeys[0]).toBeTruthy();
    expect(state.createdMarkdownSkillBodies[0]).toMatchObject({
      description: "依据公司差旅与费用制度逐项核查报销单据合规性",
      enabled: true,
      featured: true,
      name: "财务报销合规审核助手",
      owner_department_id: "dept-finance",
      scope: "department",
      starter_prompt: starterPromptText,
      visibility_grants: [
        {
          include_subdepartments: false,
          target_id: "dept-finance",
          target_type: "department",
        },
        {
          include_subdepartments: false,
          target_id: "dept-audit",
          target_type: "department",
        },
      ],
    });

    // 3. 创建成功后自动打开详情弹窗，验证双向格式锁（隐藏 ZIP 上传，仅展示在线编辑新版本入口）。
    const detailDialog = page.getByRole("dialog", {
      name: "技能：财务报销合规审核助手",
    });
    await expect(detailDialog).toBeVisible();
    await expect(
      detailDialog.getByTestId("markdown-version-publish-section"),
    ).toBeVisible();
    await expect(
      detailDialog.getByRole("button", { name: "在线编辑并发布新版本" }),
    ).toBeVisible();
    await expect(detailDialog.getByText("上传完整新版本")).toHaveCount(0);
    await expect(detailDialog.getByLabel("选择新版本 ZIP")).toHaveCount(0);
    await detailDialog.getByRole("button", { name: "关闭", exact: true }).click();
    await expect(detailDialog).toBeHidden();

    // 4. 切换回普通员工访问 /chat 首页，验证推荐技能卡直达挂载并自动预填 starter_prompt。
    state.currentUser = MOCK_MEMBER;
    await page.goto("/chat");
    await expect(
      page.getByRole("heading", { level: 1, name: "你好，林致远" }),
    ).toBeVisible();

    const featuredSkillCard = page.locator(
      '[data-scenario-card="skill"][data-skill-featured="true"]',
    );
    await expect(featuredSkillCard).toBeVisible();
    await expect(featuredSkillCard).toContainText("财务报销合规审核助手");
    await expect(featuredSkillCard).toContainText("推荐");

    await featuredSkillCard.click();
    await expect(
      page.locator('[data-skill-chip-id="skill-md-fin-audit"]'),
    ).toContainText("财务报销合规审核助手");
    await expect(page.getByLabel("对话草稿")).toHaveValue(starterPromptText);
    await expectNoHorizontalOverflow(page);
  });

  test("场景 8：知识库跨部门附加共享授权与双栏 Markdown 文档导入预览", async ({
    page,
  }) => {
    const state = await installPhase3Fixtures(page, MOCK_SUPER_ADMIN);
    await page.setViewportSize({ height: 900, width: 1280 });
    await page.goto("/knowledge");

    await expect(
      page.getByRole("heading", { level: 1, name: "知识库" }),
    ).toBeVisible();

    // 1. super_admin 新建部门共享知识库，通过 VisibilityGrantsEditor 追加 1 个跨部门与 1 个角色授权。
    await page.getByRole("button", { name: "新建知识库" }).first().click();
    const createDialog = page.getByRole("dialog", { name: "新建知识库" });
    await expect(createDialog).toBeVisible();

    await createDialog.getByLabel("知识库名称").fill("跨部门合规知识库");
    await createDialog.getByRole("combobox", { name: "可见范围" }).click();
    await page.getByRole("option", { name: "部门共享" }).click();
    await createDialog.getByRole("combobox", { name: "归属部门" }).click();
    await page.getByRole("option", { name: "财务分析部" }).click();

    const grantsEditor = createDialog.getByTestId("visibility-grants-editor");
    await expect(grantsEditor).toBeVisible();
    await grantsEditor
      .getByRole("combobox", { name: "选择附加共享部门" })
      .click();
    await page.getByRole("option", { name: "平台工程部" }).click();
    await grantsEditor.getByRole("button", { name: "添加共享" }).click();

    await grantsEditor.getByRole("combobox", { name: "附加授权类型" }).click();
    await page.getByRole("option", { name: "指定角色" }).click();
    await grantsEditor
      .getByRole("combobox", { name: "选择附加共享角色" })
      .click();
    await page.getByRole("option", { name: "部门管理员" }).click();
    await grantsEditor.getByRole("button", { name: "添加共享" }).click();

    await createDialog.getByRole("button", { name: "创建知识库" }).click();

    // 2. 断言 POST /api/v1/knowledge-bases 载荷与跳转到详情页的「额外共享 2 项」徽章。
    await expect(page).toHaveURL(/\/knowledge\/kb-phase3-shared$/);
    expect(state.createdKnowledgeBodies).toHaveLength(1);
    expect(state.createdKnowledgeBodies[0]).toEqual({
      name: "跨部门合规知识库",
      owner_department_id: "dept-finance",
      visibility_grants: [
        {
          include_subdepartments: true,
          target_id: "dept-finance",
          target_type: "department",
        },
        {
          include_subdepartments: true,
          target_id: "dept-platform",
          target_type: "department",
        },
        {
          include_subdepartments: false,
          target_id: "dept_admin",
          target_type: "role",
        },
      ],
      visibility_scope: "department",
    });

    await expect(
      page.getByRole("heading", { level: 1, name: "跨部门合规知识库" }),
    ).toBeVisible();
    await expect(page.getByText("额外共享 2 项")).toBeVisible();

    // 3. 点击「添加资料 → 粘贴文本」，在双栏 MarkdownSplitEditor 中输入正文并校验右侧预览渲染。
    await page
      .locator("main header")
      .first()
      .getByRole("button", { name: "添加资料" })
      .click();
    const ingestDialog = page.getByRole("dialog", { name: "添加资料" });
    await expect(ingestDialog).toBeVisible();

    await ingestDialog.getByRole("button", { name: "粘贴文本" }).click();
    await ingestDialog.getByLabel("文档标题").fill("2026 跨部门报销协作规范");
    await ingestDialog
      .getByLabel("文本内容")
      .fill(
        "# 报销协作规范\n\n- 跨部门联合项目须附双方部门负责人审批\n- 统一使用电子发票 PDF 原件",
      );

    const previewPane = ingestDialog.getByTestId("markdown-split-preview");
    await expect(
      previewPane.getByRole("heading", { name: "报销协作规范" }),
    ).toBeVisible();
    await expect(previewPane).toContainText(
      "跨部门联合项目须附双方部门负责人审批",
    );

    await ingestDialog.getByRole("button", { name: "添加资料" }).click();
    await expect(ingestDialog).toBeHidden();
    await expect(page.getByText("2026 跨部门报销协作规范")).toBeVisible();
    await expect(page.getByText("已就绪")).toBeVisible();
    await expectNoHorizontalOverflow(page);
  });
});
