import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { makeModelProfile, TEST_ADMIN } from "../../test/adminFixtures";
import { getRequestUrl } from "../../test/fixtures";
import { jsonResponse, renderApp } from "../../test/testUtils";
import type {
  SubagentAvailableToolsResponse,
  SubagentProfile,
} from "./model";

function requestUrl(input: RequestInfo | URL): URL {
  return new URL(getRequestUrl(input), "http://localhost");
}

function makeSubagent(
  overrides: Partial<SubagentProfile> = {},
): SubagentProfile {
  return {
    id: "subagent-1",
    slug: "research_assistant",
    name: "调研助手",
    description: "检索资料并整理结构化摘要。",
    system_prompt: "你是一名严谨的调研助手。",
    model_profile_id: null,
    allowed_tool_ids: ["builtin_knowledge_search"],
    enabled: true,
    max_llm_requests: 12,
    timeout_seconds: 180,
    version: 1,
    created_by: TEST_ADMIN.id,
    created_at: "2026-11-16T00:00:00Z",
    owner_department_id: null,
    updated_at: "2026-11-16T00:00:00Z",
    visibility_grants: [],
    visibility_scope: "company",
    ...overrides,
  };
}

const AVAILABLE_TOOLS: SubagentAvailableToolsResponse = {
  groups: [
    {
      group_key: "builtin",
      label: "内置工具",
      items: [
        {
          tool_id: "builtin_knowledge_search",
          name: "知识库检索",
          description: "检索会话与项目绑定的知识库。",
          source_type: "builtin",
          requires_approval: false,
          server_id: null,
          server_name: null,
        },
        {
          tool_id: "builtin_skill_execute",
          name: "执行技能",
          description: "执行受控技能脚本。",
          source_type: "builtin",
          requires_approval: true,
          server_id: null,
          server_name: null,
        },
      ],
    },
    {
      group_key: "mcp:mcp-1",
      label: "MCP · 内部检索服务",
      items: [
        {
          tool_id: "mcp_search_docs",
          name: "search_docs",
          description: "查询内部文档库。",
          source_type: "mcp",
          requires_approval: true,
          server_id: "mcp-1",
          server_name: "内部检索服务",
        },
      ],
    },
  ],
};

const MODEL_A = makeModelProfile({
  id: "model-1",
  name: "主力模型",
  model: "gpt-4o",
  enabled: true,
});

describe("管理子智能体页", () => {
  it("展示列表、创建表单校验与工具白名单分组并成功创建", async () => {
    let createdPayload: Record<string, unknown> | null = null;
    let currentItems: SubagentProfile[] = [makeSubagent()];

    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (url.pathname === "/api/v1/admin/llm/model-profiles") {
          return jsonResponse({
            items: [MODEL_A],
            default_profile_id: MODEL_A.id,
          });
        }
        if (url.pathname === "/api/v1/admin/subagents/available-tools") {
          return jsonResponse(AVAILABLE_TOOLS);
        }
        if (
          url.pathname === "/api/v1/admin/subagents" &&
          method === "GET"
        ) {
          return jsonResponse({
            items: currentItems,
            total: currentItems.length,
            offset: 0,
            limit: 10,
          });
        }
        if (
          url.pathname === "/api/v1/admin/subagents" &&
          method === "POST"
        ) {
          createdPayload = JSON.parse(String(init?.body ?? "{}")) as Record<
            string,
            unknown
          >;
          const created = makeSubagent({
            id: "subagent-2",
            slug: String(createdPayload.slug),
            name: String(createdPayload.name),
            description: String(createdPayload.description),
            system_prompt: String(createdPayload.system_prompt),
            allowed_tool_ids: createdPayload.allowed_tool_ids as string[],
            model_profile_id:
              (createdPayload.model_profile_id as string | null) ?? null,
          });
          currentItems = [created, ...currentItems];
          return jsonResponse(created, 201);
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/admin/subagents");

    expect(await screen.findByText("调研助手")).toBeInTheDocument();
    expect(screen.getByText("research_assistant")).toBeInTheDocument();
    expect(screen.getByText("跟随系统默认")).toBeInTheDocument();
    expect(screen.getByText("1 个")).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "新建子智能体" }),
    );

    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByText(/系统已自动禁止子智能体嵌套调度/),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByText(/main、default、system、subagent/),
    ).toBeInTheDocument();
    expect(within(dialog).getByText("内置工具")).toBeInTheDocument();
    expect(within(dialog).getByText("MCP · 内部检索服务")).toBeInTheDocument();
    expect(within(dialog).getAllByText("需人工审批")).toHaveLength(2);

    // 校验保留字拦截
    await user.type(within(dialog).getByLabelText("Slug 标识"), "system");
    await user.type(within(dialog).getByLabelText("显示名称"), "代码助手");
    await user.type(
      within(dialog).getByLabelText("能力描述"),
      "协助分析代码库。{enter}输出风险摘要。",
    );
    await user.type(
      within(dialog).getByLabelText("System Prompt"),
      "你是一名代码分析专家。",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "创建子智能体" }),
    );
    expect(
      await within(dialog).findByText(/「system」为系统保留字/),
    ).toBeInTheDocument();

    // 修正为合法 slug 并勾选工具
    const slugInput = within(dialog).getByLabelText("Slug 标识");
    await user.clear(slugInput);
    await user.type(slugInput, "code_helper");
    await user.click(within(dialog).getByText("search_docs"));
    await user.click(
      within(dialog).getByRole("button", { name: "创建子智能体" }),
    );

    await waitFor(() => {
      expect(createdPayload).toEqual({
        slug: "code_helper",
        name: "代码助手",
        description: "协助分析代码库。\n输出风险摘要。",
        system_prompt: "你是一名代码分析专家。",
        model_profile_id: null,
        allowed_tool_ids: ["mcp_search_docs"],
        enabled: true,
        max_llm_requests: null,
        timeout_seconds: null,
        visibility_scope: "company",
        owner_department_id: null,
        visibility_grants: [],
      });
    });

    expect(await screen.findByText("代码助手")).toBeInTheDocument();
  });

  it("优雅处理 409 slug 冲突与 409 乐观锁版本冲突", async () => {
    const existing = makeSubagent({ version: 2 });
    const latest = makeSubagent({
      version: 3,
      name: "调研助手 v3",
    });
    let detailCalls = 0;

    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (url.pathname === "/api/v1/admin/llm/model-profiles") {
          return jsonResponse({
            items: [MODEL_A],
            default_profile_id: MODEL_A.id,
          });
        }
        if (url.pathname === "/api/v1/admin/subagents/available-tools") {
          return jsonResponse(AVAILABLE_TOOLS);
        }
        if (
          url.pathname === "/api/v1/admin/subagents" &&
          method === "GET"
        ) {
          return jsonResponse({
            items: [existing],
            total: 1,
            offset: 0,
            limit: 10,
          });
        }
        if (
          url.pathname === "/api/v1/admin/subagents/subagent-1" &&
          method === "GET"
        ) {
          detailCalls += 1;
          return jsonResponse(detailCalls === 1 ? existing : latest);
        }
        if (
          url.pathname === "/api/v1/admin/subagents/subagent-1" &&
          method === "PATCH"
        ) {
          return jsonResponse(
            { detail: "subagent_version_conflict" },
            409,
          );
        }
        if (
          url.pathname === "/api/v1/admin/subagents" &&
          method === "POST"
        ) {
          return jsonResponse({ detail: "subagent_slug_conflict" }, 409);
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/admin/subagents");

    await user.click(
      await screen.findByRole("button", { name: "编辑子智能体：调研助手" }),
    );
    const editDialog = await screen.findByRole("dialog");
    await waitFor(() => {
      expect(
        within(editDialog).getByLabelText("显示名称"),
      ).toHaveValue("调研助手");
    });

    await user.click(
      within(editDialog).getByRole("button", { name: "保存修改" }),
    );
    expect(
      await within(editDialog).findByText(/版本冲突/),
    ).toBeInTheDocument();

    // 点击重新加载最新配置
    await user.click(
      within(editDialog).getByRole("button", { name: "重新加载最新配置" }),
    );
    await waitFor(() => {
      expect(
        within(editDialog).getByLabelText("显示名称"),
      ).toHaveValue("调研助手 v3");
    });

    await user.click(within(editDialog).getByRole("button", { name: "取消" }));

    // 测试新建时的 409 slug 冲突
    await user.click(screen.getByRole("button", { name: "新建子智能体" }));
    const createDialog = await screen.findByRole("dialog");
    await user.type(
      within(createDialog).getByLabelText("Slug 标识"),
      "research_assistant",
    );
    await user.type(within(createDialog).getByLabelText("显示名称"), "重名");
    await user.type(within(createDialog).getByLabelText("能力描述"), "描述");
    await user.type(
      within(createDialog).getByLabelText("System Prompt"),
      "提示词",
    );
    await user.click(
      within(createDialog).getByRole("button", { name: "创建子智能体" }),
    );
    expect(
      await within(createDialog).findAllByText(/已被其他启用或停用中的 Subagent 占用/),
    ).not.toHaveLength(0);
  });

  it("编辑已有 100 项工具时明确提示上限并禁止继续勾选", async () => {
    const allowedToolIds = Array.from(
      { length: 100 },
      (_, index) => `tool_${index + 1}`,
    );
    const existing = makeSubagent({
      allowed_tool_ids: allowedToolIds,
      version: 4,
    });
    const tools = Array.from({ length: 101 }, (_, index) => ({
      tool_id: `tool_${index + 1}`,
      name: `工具 ${index + 1}`,
      description: `工具 ${index + 1} 的描述`,
      source_type: "builtin",
      requires_approval: false,
      server_id: null,
      server_name: null,
    }));

    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (url.pathname === "/api/v1/admin/llm/model-profiles") {
          return jsonResponse({ items: [], default_profile_id: null });
        }
        if (url.pathname === "/api/v1/admin/subagents/available-tools") {
          return jsonResponse({
            groups: [
              {
                group_key: "builtin",
                label: "内置工具",
                items: tools,
              },
            ],
          });
        }
        if (
          url.pathname === "/api/v1/admin/subagents" &&
          method === "GET"
        ) {
          return jsonResponse({
            items: [existing],
            total: 1,
            offset: 0,
            limit: 10,
          });
        }
        if (
          url.pathname === "/api/v1/admin/subagents/subagent-1" &&
          method === "GET"
        ) {
          return jsonResponse(existing);
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/admin/subagents");

    await user.click(
      await screen.findByRole("button", {
        name: "编辑子智能体：调研助手",
      }),
    );
    const dialog = await screen.findByRole("dialog");

    expect(
      await within(dialog).findByText(
        "已选择 100 / 100 项，已达到上限；取消已选工具后才能继续选择。",
      ),
    ).toBeInTheDocument();
    const unselectedTool = within(dialog).getByRole("checkbox", {
      name: /工具 101/,
    });
    expect(unselectedTool).toBeDisabled();

    await user.click(
      within(dialog).getByRole("checkbox", { name: /工具 1 / }),
    );
    expect(
      within(dialog).getByText("已选择 99 / 100 项。"),
    ).toBeInTheDocument();
    expect(unselectedTool).toBeEnabled();
  });

  it("支持启停切换与软删除确认", async () => {
    let subagent = makeSubagent({ enabled: true });
    let deleted = false;

    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (url.pathname === "/api/v1/admin/llm/model-profiles") {
          return jsonResponse({ items: [], default_profile_id: null });
        }
        if (
          url.pathname === "/api/v1/admin/subagents" &&
          method === "GET"
        ) {
          return jsonResponse({
            items: deleted ? [] : [subagent],
            total: deleted ? 0 : 1,
            offset: 0,
            limit: 10,
          });
        }
        if (
          url.pathname === "/api/v1/admin/subagents/subagent-1/enabled" &&
          method === "PUT"
        ) {
          const body = JSON.parse(String(init?.body ?? "{}")) as {
            enabled: boolean;
          };
          subagent = { ...subagent, enabled: body.enabled, version: 2 };
          return jsonResponse(subagent);
        }
        if (
          url.pathname === "/api/v1/admin/subagents/subagent-1" &&
          method === "DELETE"
        ) {
          deleted = true;
          return new Response(null, { status: 204 });
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/admin/subagents");

    await user.click(
      await screen.findByRole("button", { name: "停用子智能体：调研助手" }),
    );
    expect(await screen.findByText("已停用")).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "删除子智能体：调研助手" }),
    );
    const confirmDialog = await screen.findByRole("dialog");
    await user.click(
      within(confirmDialog).getByRole("button", { name: "确认删除" }),
    );

    expect(await screen.findByText("还没有子智能体")).toBeInTheDocument();
  });

  it("未打开子智能体弹窗时不提前请求可选工具目录，打开后才按需请求", async () => {
    let availableToolsCalls = 0;
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (url.pathname === "/api/v1/admin/llm/model-profiles") {
          return jsonResponse({
            items: [MODEL_A],
            default_profile_id: MODEL_A.id,
          });
        }
        if (url.pathname === "/api/v1/admin/subagents/available-tools") {
          availableToolsCalls += 1;
          return jsonResponse(AVAILABLE_TOOLS);
        }
        if (
          url.pathname === "/api/v1/admin/subagents" &&
          method === "GET"
        ) {
          return jsonResponse({
            items: [makeSubagent()],
            total: 1,
            offset: 0,
            limit: 10,
          });
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/admin/subagents");

    expect(await screen.findByText("调研助手")).toBeInTheDocument();
    // 隐藏弹窗的 available-tools 查询必须保持禁用，进入管理页 0 次调用。
    expect(availableToolsCalls).toBe(0);

    await user.click(screen.getByRole("button", { name: "新建子智能体" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("内置工具")).toBeInTheDocument();
    expect(availableToolsCalls).toBe(1);
  });

  it("兼容展示子智能体一期可见范围标签", async () => {
    const companySubagent = makeSubagent({
      id: "subagent-company",
      name: "全公司合规审核助手",
      visibility_scope: "company",
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (url.pathname === "/api/v1/admin/llm/model-profiles") {
          return jsonResponse({
            default_profile_id: MODEL_A.id,
            items: [MODEL_A],
          });
        }
        if (url.pathname === "/api/v1/admin/subagents" && method === "GET") {
          return jsonResponse({
            items: [companySubagent],
            limit: 10,
            offset: 0,
            total: 1,
          });
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );

    renderApp("/admin/subagents");

    const row = (await screen.findByText("全公司合规审核助手")).closest(
      "li",
    ) as HTMLElement;
    expect(within(row).getByText("全公司")).toBeInTheDocument();
  });

  it("支持选择具体绑定模型、拦截已停用模型选项，并支持切回跟随系统默认提交 null", async () => {
    const MODEL_B = makeModelProfile({
      id: "model-2",
      name: "推理模型",
      model: "o3-mini",
      enabled: true,
    });
    const MODEL_DISABLED = makeModelProfile({
      id: "model-disabled",
      name: "已停用旧模型",
      model: "gpt-3.5",
      enabled: false,
    });
    const createdSubagent = makeSubagent({
      id: "subagent-model",
      slug: "reasoning_helper",
      name: "推理助手",
      model_profile_id: MODEL_B.id,
      version: 1,
    });

    let currentItems: SubagentProfile[] = [];
    let createdPayload: Record<string, unknown> | null = null;
    let patchedPayload: Record<string, unknown> | null = null;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (url.pathname === "/api/v1/admin/llm/model-profiles") {
          return jsonResponse({
            items: [MODEL_A, MODEL_B, MODEL_DISABLED],
            default_profile_id: MODEL_A.id,
          });
        }
        if (url.pathname === "/api/v1/admin/subagents/available-tools") {
          return jsonResponse(AVAILABLE_TOOLS);
        }
        if (
          url.pathname === "/api/v1/admin/subagents" &&
          method === "GET"
        ) {
          return jsonResponse({
            items: currentItems,
            total: currentItems.length,
            offset: 0,
            limit: 10,
          });
        }
        if (
          url.pathname === "/api/v1/admin/subagents" &&
          method === "POST"
        ) {
          createdPayload = JSON.parse(String(init?.body ?? "{}")) as Record<
            string,
            unknown
          >;
          currentItems = [createdSubagent];
          return jsonResponse(createdSubagent, 201);
        }
        if (
          url.pathname === `/api/v1/admin/subagents/${createdSubagent.id}` &&
          method === "GET"
        ) {
          return jsonResponse(createdSubagent);
        }
        if (
          url.pathname === `/api/v1/admin/subagents/${createdSubagent.id}` &&
          method === "PATCH"
        ) {
          patchedPayload = JSON.parse(String(init?.body ?? "{}")) as Record<
            string,
            unknown
          >;
          const updated = makeSubagent({
            ...createdSubagent,
            model_profile_id: null,
            version: 2,
          });
          currentItems = [updated];
          return jsonResponse(updated);
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );
    const user = userEvent.setup();

    renderApp("/admin/subagents");

    await user.click(
      await screen.findByRole("button", { name: "新建子智能体" }),
    );
    const createDialog = await screen.findByRole("dialog", {
      name: "新建子智能体",
    });

    await user.type(
      within(createDialog).getByLabelText("Slug 标识"),
      "reasoning_helper",
    );
    await user.type(within(createDialog).getByLabelText("显示名称"), "推理助手");
    await user.type(
      within(createDialog).getByLabelText("能力描述"),
      "处理复杂推理任务。",
    );
    await user.type(
      within(createDialog).getByLabelText("System Prompt"),
      "你是一名推理助手。",
    );

    const createModelTrigger = within(createDialog).getByRole("combobox", {
      name: "绑定模型",
    });
    await waitFor(() => expect(createModelTrigger).not.toBeDisabled());
    expect(createModelTrigger).toHaveTextContent("跟随系统默认");

    await user.click(createModelTrigger);
    const disabledOption = screen.getByRole("option", {
      name: /已停用旧模型.*（已停用）/,
    });
    expect(disabledOption).toHaveAttribute("aria-disabled", "true");
    await user.click(disabledOption);
    // 已停用选项不可被选中，列表仍保持打开或未选中该项。
    expect(createModelTrigger).toHaveTextContent("跟随系统默认");

    await user.click(
      screen.getByRole("option", { name: new RegExp(MODEL_B.name) }),
    );
    expect(createModelTrigger).toHaveTextContent(MODEL_B.name);

    await user.click(
      within(createDialog).getByRole("button", { name: "创建子智能体" }),
    );
    await waitFor(() => {
      expect(createdPayload).toMatchObject({
        model_profile_id: MODEL_B.id,
      });
    });

    // 编辑已绑定具体模型的子智能体，切回“跟随系统默认”后应提交 model_profile_id: null。
    await user.click(
      await screen.findByRole("button", { name: "编辑子智能体：推理助手" }),
    );
    const editDialog = await screen.findByRole("dialog", {
      name: "编辑子智能体",
    });
    const editModelTrigger = within(editDialog).getByRole("combobox", {
      name: "绑定模型",
    });
    await waitFor(() => {
      expect(editModelTrigger).not.toBeDisabled();
      expect(editModelTrigger).toHaveTextContent(MODEL_B.name);
    });

    await user.click(editModelTrigger);
    await user.click(screen.getByRole("option", { name: "跟随系统默认" }));
    expect(editModelTrigger).toHaveTextContent("跟随系统默认");

    await user.click(
      within(editDialog).getByRole("button", { name: "保存修改" }),
    );
    await waitFor(() => {
      expect(patchedPayload).toMatchObject({
        model_profile_id: null,
      });
    });
  });

  it("支持配置部门共享可见范围、附加授权并映射展示部门中文名", async () => {
    let currentItems: SubagentProfile[] = [];
    let createdPayload: Record<string, unknown> | null = null;
    let patchedPayload: Record<string, unknown> | null = null;
    const deptSubagent = makeSubagent({
      id: "subagent-dept",
      slug: "fin_auditor",
      name: "财务审计助手",
      visibility_scope: "department",
      owner_department_id: "dept-fin",
      visibility_grants: [
        {
          id: "grant-1",
          target_type: "department",
          target_id: "dept-fin",
          include_subdepartments: true,
          created_at: "2026-11-20T00:00:00Z",
        },
      ],
    });

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (url.pathname === "/api/v1/admin/org/departments") {
          return jsonResponse({
            items: [
              {
                id: "dept-fin",
                name: "财务部",
                parent_id: null,
                status: "active",
                path_ids: ["dept-fin"],
              },
              {
                id: "dept-rd",
                name: "研发中心",
                parent_id: null,
                status: "active",
                path_ids: ["dept-rd"],
              },
            ],
          });
        }
        if (url.pathname === "/api/v1/admin/llm/model-profiles") {
          return jsonResponse({
            items: [MODEL_A],
            default_profile_id: MODEL_A.id,
          });
        }
        if (url.pathname === "/api/v1/admin/subagents/available-tools") {
          return jsonResponse(AVAILABLE_TOOLS);
        }
        if (url.pathname === "/api/v1/admin/subagents" && method === "GET") {
          return jsonResponse({
            items: currentItems,
            total: currentItems.length,
            offset: 0,
            limit: 10,
          });
        }
        if (url.pathname === "/api/v1/admin/subagents" && method === "POST") {
          createdPayload = JSON.parse(String(init?.body ?? "{}")) as Record<
            string,
            unknown
          >;
          currentItems = [deptSubagent];
          return jsonResponse(deptSubagent, 201);
        }
        if (
          url.pathname === `/api/v1/admin/subagents/${deptSubagent.id}` &&
          method === "GET"
        ) {
          return jsonResponse(deptSubagent);
        }
        if (
          url.pathname === `/api/v1/admin/subagents/${deptSubagent.id}` &&
          method === "PATCH"
        ) {
          patchedPayload = JSON.parse(String(init?.body ?? "{}")) as Record<
            string,
            unknown
          >;
          const updated = makeSubagent({
            ...deptSubagent,
            visibility_scope: "company",
            owner_department_id: null,
            visibility_grants: [],
            version: 2,
          });
          currentItems = [updated];
          return jsonResponse(updated);
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );
    const user = userEvent.setup();

    renderApp("/admin/subagents");

    await user.click(
      await screen.findByRole("button", { name: "新建子智能体" }),
    );
    const createDialog = await screen.findByRole("dialog", {
      name: "新建子智能体",
    });

    await user.type(
      within(createDialog).getByLabelText("Slug 标识"),
      "fin_auditor",
    );
    await user.type(
      within(createDialog).getByLabelText("显示名称"),
      "财务审计助手",
    );
    await user.type(
      within(createDialog).getByLabelText("能力描述"),
      "财务凭证审计。",
    );
    await user.type(
      within(createDialog).getByLabelText("System Prompt"),
      "你是财务审计专家。",
    );

    await user.click(
      within(createDialog).getByRole("combobox", { name: "可见范围" }),
    );
    await user.click(screen.getByRole("option", { name: "部门共享" }));

    const deptCombo = await within(createDialog).findByRole("combobox", {
      name: "归属部门",
    });
    await user.click(deptCombo);
    await user.click(screen.getByRole("option", { name: "财务部" }));

    expect(
      await within(createDialog).findByText("归属部门（默认）"),
    ).toBeInTheDocument();

    await user.click(
      within(createDialog).getByRole("button", { name: "创建子智能体" }),
    );
    await waitFor(() => {
      expect(createdPayload).toMatchObject({
        visibility_scope: "department",
        owner_department_id: "dept-fin",
        visibility_grants: [
          {
            target_type: "department",
            target_id: "dept-fin",
            include_subdepartments: true,
          },
        ],
      });
    });

    const card = (await screen.findByText("财务审计助手")).closest(
      "li",
    ) as HTMLElement;
    expect(await within(card).findByText("财务部")).toBeInTheDocument();

    // 编辑并切换回全公司可见
    await user.click(
      within(card).getByRole("button", { name: "编辑子智能体：财务审计助手" }),
    );
    const editDialog = await screen.findByRole("dialog", {
      name: "编辑子智能体",
    });
    await waitFor(() => {
      expect(within(editDialog).getByLabelText("显示名称")).toHaveValue(
        "财务审计助手",
      );
    });
    await user.click(
      within(editDialog).getByRole("combobox", { name: "可见范围" }),
    );
    await user.click(screen.getByRole("option", { name: "全公司" }));

    await user.click(
      within(editDialog).getByRole("button", { name: "保存修改" }),
    );
    await waitFor(() => {
      expect(patchedPayload).toMatchObject({
        visibility_scope: "company",
        owner_department_id: null,
        visibility_grants: [],
      });
    });
  });
});
