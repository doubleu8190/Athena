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
      cancel_requested_at: null,
      context_cutoff_sequence: 1,
      created_at: NOW,
      event_sequence: approved ? 6 : 3,
      execution_epoch: 1,
      finished_at: approved ? "2026-11-24T09:00:02Z" : null,
      first_started_at: "2026-11-24T09:00:00Z",
      id: "run-workbench-1",
      input_file_ids: state.uploadedFiles.map((file) => file.id),
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
      report_message_id: approved ? "msg-assistant-final" : null,
      retry_of_run_id: null,
      safe_error_code: null,
      session_id: sessionId,
      skill_snapshot: null,
      status: approved ? "completed" : "waiting_for_approval",
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

  test("场景 4：单页双栏项目文件夹、旧路由重定向与温和解绑删除项目", async ({
    page,
  }) => {
    const state = await installWorkbenchFixtures(page);
    await page.setViewportSize({ height: 900, width: 1280 });

    // 验证历史 4-Tab 路由 /projects/:id/overview 自动重定向到单页工作文件夹 /projects/:id。
    await page.goto("/projects/proj-q4-budget/overview");
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
