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
    updated_at: "2026-11-16T00:00:00Z",
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
});
