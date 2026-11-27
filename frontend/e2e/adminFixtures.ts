import type { Page, Route } from "@playwright/test";

const NOW = "2026-11-10T08:00:00Z";

// 对齐二期三层角色与企业组织身份投影契约（super_admin）。
const ADMIN = {
  auth_provider: "mock" as const,
  avatar_url: null,
  created_at: "2026-11-01T00:00:00Z",
  departments: [
    {
      id: "dept-platform",
      is_primary: true,
      name: "平台工程部",
    },
  ],
  display_name: "端到端管理员",
  email: "e2e-admin@example.com",
  employee_no: "E0001",
  id: "admin-e2e",
  job_title: "企业平台管理员",
  managed_department_ids: [] as string[],
  role: "super_admin" as const,
};

interface ModelFixture {
  base_url: string;
  context_window_tokens: number;
  created_at: string;
  enabled: boolean;
  execution_timeout_seconds: number;
  id: string;
  max_concurrency: number;
  max_output_tokens: number;
  model: string;
  name: string;
  protocol: string;
  request_timeout_seconds: number;
  scenario_description: string | null;
  scenario_label: string | null;
  supports_parallel_tool_calls: boolean;
  supports_stream: boolean;
  supports_tools: boolean;
  supports_vision: boolean;
  temperature: number | null;
  tokenizer_encoding: string;
  updated_at: string;
  version: number;
  visible_to_users: boolean;
}

interface McpFixture {
  config: Record<string, unknown>;
  config_version: number;
  created_at: string;
  created_by: string;
  enabled: boolean;
  id: string;
  name: string;
  owner_department_id: string | null;
  secrets: { has_value: boolean; name: string }[];
  tools_discovered_at: string | null;
  transport: string;
  updated_at: string;
  visibility_grants: unknown[];
  visibility_scope: "company" | "department";
}

interface ToolFixture {
  approval_summary_template: string | null;
  description: string;
  display_name: string | null;
  input_schema: Record<string, unknown>;
  model_name: string;
  parameter_labels: Record<string, string>;
  remote_name: string;
  requires_approval: boolean;
  result_labels: Record<string, string>;
  server_id: string;
  tool_id: string;
}

interface SubagentFixture {
  allowed_tool_ids: string[];
  created_at: string;
  created_by: string;
  description: string;
  enabled: boolean;
  id: string;
  max_llm_requests: number | null;
  model_profile_id: string | null;
  name: string;
  owner_department_id: string | null;
  slug: string;
  system_prompt: string;
  timeout_seconds: number | null;
  updated_at: string;
  version: number;
  visibility_grants: unknown[];
  visibility_scope: "company" | "department";
}

interface FixtureState {
  mcpServers: McpFixture[];
  models: ModelFixture[];
  subagents: SubagentFixture[];
  tools: ToolFixture[];
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    body: JSON.stringify(body),
    contentType: "application/json",
    status,
  });
}

function makeModel(body: Record<string, unknown>): ModelFixture {
  return {
    ...(body as Omit<
      ModelFixture,
      | "created_at"
      | "enabled"
      | "id"
      | "protocol"
      | "scenario_description"
      | "scenario_label"
      | "supports_vision"
      | "updated_at"
      | "version"
      | "visible_to_users"
    >),
    created_at: NOW,
    enabled: true,
    id: "model-e2e",
    protocol: "chat_completions",
    scenario_description:
      (body.scenario_description as string | null | undefined) ??
      "日常公文起草与通用企业问答",
    scenario_label:
      (body.scenario_label as string | null | undefined) ?? "通用办公",
    supports_vision: Boolean(body.supports_vision ?? false),
    temperature: (body.temperature as number | null | undefined) ?? null,
    updated_at: NOW,
    version: 1,
    visible_to_users: Boolean(body.visible_to_users ?? true),
  };
}

/**
 * 管理端 E2E 只使用确定性 HTTP fixture，避免把未启动的真实 LLM/MCP 服务当成已验收集成。
 */
export async function installAdminFixtures(
  page: Page,
): Promise<FixtureState> {
  const state: FixtureState = {
    mcpServers: [],
    models: [],
    subagents: [
      {
        allowed_tool_ids: ["tool-e2e"],
        created_at: NOW,
        created_by: ADMIN.id,
        description:
          "负责分析较长的研究材料，并在复杂布局下保持内容、状态与操作清晰可见。",
        enabled: true,
        id: "subagent-e2e",
        max_llm_requests: 8,
        model_profile_id: null,
        name: "端到端子智能体与研究资料分析助手",
        owner_department_id: null,
        slug: "e2e-research-subagent-with-a-long-stable-slug",
        system_prompt: "只提交经过核验的研究结论。",
        timeout_seconds: 300,
        updated_at: NOW,
        version: 3,
        visibility_grants: [],
        visibility_scope: "company",
      },
    ],
    tools: [
      {
        approval_summary_template: "将检索「{query}」对应的企业知识条目",
        description: "查询端到端知识索引",
        display_name: "企业知识索引检索",
        input_schema: { type: "object" },
        model_name: "mcp__eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        parameter_labels: { query: "检索关键词" },
        remote_name: "knowledge_search",
        requires_approval: false,
        result_labels: {},
        server_id: "mcp-e2e",
        tool_id: "tool-e2e",
      },
    ],
  };

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    const path = url.pathname;

    if (path === "/api/v1/auth/providers" && method === "GET") {
      return json(route, {
        feishu_enabled: false,
        local_login_enabled: true,
        mock_enabled: true,
      });
    }
    if (path === "/api/v1/auth/me") {
      return json(route, ADMIN);
    }
    if (path === "/api/v1/models" && method === "GET") {
      return json(route, {
        default_model_id: state.models[0]?.id ?? null,
        items: state.models.map((model, index) => ({
          id: model.id,
          is_default: index === 0,
          name: model.name,
          scenario_description: model.scenario_description,
          scenario_label: model.scenario_label,
          supports_vision: model.supports_vision,
        })),
      });
    }
    if (path === "/api/v1/sessions" && method === "GET") {
      return json(route, {
        items: [],
        page: Number(url.searchParams.get("page") ?? 1),
        page_size: Number(url.searchParams.get("page_size") ?? 20),
        total: 0,
      });
    }
    if (path === "/api/v1/projects" && method === "GET") {
      return json(route, {
        items: [],
        page: Number(url.searchParams.get("page") ?? 1),
        page_size: Number(url.searchParams.get("page_size") ?? 20),
        total: 0,
      });
    }
    if (path === "/api/v1/admin/org/departments" && method === "GET") {
      return json(route, {
        items: [
          {
            created_at: NOW,
            external_id: "feishu-dept-platform",
            id: "dept-platform",
            name: "平台工程部",
            order_index: 1,
            parent_id: null,
            path: "/dept-platform",
            status: "active",
            synced_at: NOW,
            updated_at: NOW,
          },
        ],
      });
    }
    if (
      path === "/api/v1/admin/llm/model-profiles" &&
      method === "GET"
    ) {
      return json(route, {
        default_profile_id: null,
        items: state.models,
      });
    }
    if (
      path === "/api/v1/admin/llm/model-profiles" &&
      method === "POST"
    ) {
      const model = makeModel(
        request.postDataJSON() as Record<string, unknown>,
      );
      state.models = [model];
      return json(route, model, 201);
    }
    if (
      path === "/api/v1/admin/llm/compression-policy" &&
      method === "GET"
    ) {
      return json(route, {
        compression_model_profile_id: null,
        created_at: null,
        id: null,
        recent_ratio: null,
        recent_token_cap: null,
        summary_ratio: null,
        summary_token_cap: null,
        trigger_tokens: null,
      });
    }
    if (
      path === "/api/v1/admin/mcp/servers" &&
      method === "GET"
    ) {
      return json(route, {
        items: state.mcpServers,
        page: Number(url.searchParams.get("page") ?? 1),
        page_size: Number(url.searchParams.get("page_size") ?? 10),
        total: state.mcpServers.length,
      });
    }
    if (
      path === "/api/v1/admin/mcp/servers" &&
      method === "POST"
    ) {
      const body = request.postDataJSON() as {
        args?: string[];
        command?: string;
        cwd?: string | null;
        name: string;
        secrets: Record<string, string>;
        transport: string;
        url?: string;
      };
      const server: McpFixture = {
        config:
          body.transport === "stdio"
            ? {
                args: body.args ?? [],
                command: body.command ?? "",
                cwd: body.cwd ?? null,
              }
            : { url: body.url ?? "" },
        config_version: 1,
        created_at: NOW,
        created_by: ADMIN.id,
        enabled: true,
        id: "mcp-e2e",
        name: body.name,
        owner_department_id: null,
        secrets: Object.keys(body.secrets).map((name) => ({
          has_value: true,
          name,
        })),
        tools_discovered_at: NOW,
        transport: body.transport,
        updated_at: NOW,
        visibility_grants: [],
        visibility_scope: "company",
      };
      state.mcpServers = [server];
      return json(route, server, 201);
    }
    if (
      path === "/api/v1/admin/tools" &&
      method === "GET"
    ) {
      return json(route, {
        items: state.tools,
        page: Number(url.searchParams.get("page") ?? 1),
        page_size: Number(url.searchParams.get("page_size") ?? 20),
        total: state.tools.length,
      });
    }
    if (
      path === "/api/v1/admin/subagents/available-tools" &&
      method === "GET"
    ) {
      return json(route, {
        groups: [
          {
            group_key: "mcp",
            items: [
              {
                description: "查询知识资料并返回可核验来源。",
                name: "知识检索",
                requires_approval: true,
                server_id: "mcp-e2e",
                server_name: "知识服务",
                source_type: "mcp",
                tool_id: "tool-e2e",
              },
            ],
            label: "MCP 工具",
          },
        ],
      });
    }
    if (
      path === "/api/v1/admin/subagents" &&
      method === "GET"
    ) {
      return json(route, {
        items: state.subagents,
        limit: Number(url.searchParams.get("limit") ?? 10),
        offset: Number(url.searchParams.get("offset") ?? 0),
        total: state.subagents.length,
      });
    }
    if (
      path === "/api/v1/admin/skills" &&
      method === "GET"
    ) {
      return json(route, { items: [] });
    }
    if (
      path === "/api/v1/admin/tools/tool-e2e" &&
      method === "PATCH"
    ) {
      const body = request.postDataJSON() as {
        requires_approval: boolean;
      };
      state.tools = state.tools.map((tool) =>
        tool.tool_id === "tool-e2e"
          ? { ...tool, requires_approval: body.requires_approval }
          : tool,
      );
      return json(route, state.tools[0]);
    }
    return json(route, { detail: "e2e_fixture_not_found" }, 404);
  });

  return state;
}
