import type { Page, Route } from "@playwright/test";

const NOW = "2026-11-10T08:00:00Z";

const ADMIN = {
  created_at: "2026-11-01T00:00:00Z",
  display_name: "端到端管理员",
  email: "e2e-admin@example.com",
  id: "admin-e2e",
  role: "admin",
};

interface ModelFixture {
  base_url: string;
  context_window_tokens: number;
  created_at: string;
  credential_env: string;
  enabled: boolean;
  execution_timeout_seconds: number;
  id: string;
  max_concurrency: number;
  max_output_tokens: number;
  model: string;
  name: string;
  protocol: string;
  request_timeout_seconds: number;
  supports_parallel_tool_calls: boolean;
  supports_stream: boolean;
  supports_tools: boolean;
  temperature: number | null;
  tokenizer_encoding: string;
}

interface McpFixture {
  config: Record<string, unknown>;
  config_version: number;
  created_at: string;
  created_by: string;
  id: string;
  name: string;
  secrets: { has_value: boolean; name: string }[];
  tools_discovered_at: string | null;
  transport: string;
  updated_at: string;
}

interface ToolFixture {
  description: string;
  input_schema: Record<string, unknown>;
  model_name: string;
  remote_name: string;
  requires_approval: boolean;
  server_id: string;
  tool_id: string;
}

interface FixtureState {
  models: ModelFixture[];
  mcpServers: McpFixture[];
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
    ...(body as Omit<ModelFixture, "created_at" | "enabled" | "id" | "protocol">),
    created_at: NOW,
    enabled: true,
    id: "model-e2e",
    protocol: "chat_completions",
    temperature: body.temperature as number | null,
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
    tools: [
      {
        description: "查询端到端知识索引",
        input_schema: { type: "object" },
        model_name: "mcp__eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        remote_name: "knowledge_search",
        requires_approval: false,
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

    if (path === "/api/v1/auth/me") {
      return json(route, ADMIN);
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
        prompt_template_version: null,
        recent_ratio: null,
        summary_ratio: null,
        trigger_ratio: null,
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
        id: "mcp-e2e",
        name: body.name,
        secrets: Object.keys(body.secrets).map((name) => ({
          has_value: true,
          name,
        })),
        tools_discovered_at: NOW,
        transport: body.transport,
        updated_at: NOW,
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
