import type {
  AdminTool,
  CompressionPolicy,
  McpServer,
  ModelProfile,
  SystemPromptVersion,
  SystemPromptVersionSummary,
} from "../features/admin/model";
import type { User } from "../features/auth/model";

export const TEST_ADMIN: User = {
  created_at: "2026-11-01T00:00:00Z",
  display_name: "管理员",
  email: "admin@example.com",
  id: "admin-1",
  role: "admin",
};

export function makeModelProfile(
  overrides: Partial<ModelProfile> & Pick<ModelProfile, "id" | "name">,
): ModelProfile {
  return {
    base_url: "https://llm.example/v1",
    context_window_tokens: 128_000,
    created_at: "2026-11-08T08:00:00Z",
    has_api_key: true,
    enabled: true,
    execution_timeout_seconds: 300,
    max_concurrency: 4,
    max_output_tokens: 8_192,
    model: "atlas-main",
    protocol: "chat_completions",
    request_timeout_seconds: 120,
    supports_parallel_tool_calls: false,
    supports_stream: true,
    supports_tools: true,
    temperature: null,
    tokenizer_encoding: "o200k_base",
    ...overrides,
  };
}

export function makeCompressionPolicy(
  overrides: Partial<CompressionPolicy> = {},
): CompressionPolicy {
  return {
    compression_model_profile_id: "model-primary",
    created_at: "2026-11-08T09:00:00Z",
    id: "policy-1",
    prompt_template_version: "compression_prompt_v1",
    recent_ratio: 0.2,
    summary_ratio: 0.1,
    trigger_ratio: 0.8,
    ...overrides,
  };
}

export function makeMcpServer(
  overrides: Partial<McpServer> & Pick<McpServer, "id" | "name">,
): McpServer {
  const { id, name, ...rest } = overrides;
  return {
    config: { url: "https://mcp.example/mcp" },
    config_version: 1,
    created_at: "2026-11-08T08:30:00Z",
    created_by: "admin-1",
    id,
    name,
    secrets: [{ has_value: true, name: "Authorization" }],
    tools_discovered_at: "2026-11-10T03:00:00Z",
    transport: "streamable_http",
    updated_at: "2026-11-10T03:00:00Z",
    ...rest,
  };
}

export function makeAdminTool(
  overrides: Partial<AdminTool> & Pick<AdminTool, "tool_id">,
): AdminTool {
  const { tool_id, ...rest } = overrides;
  return {
    description: "查询内部知识索引",
    input_schema: { type: "object" },
    model_name: "mcp__0123456789abcdef0123456789abcdef",
    remote_name: "knowledge_search",
    requires_approval: false,
    server_id: "mcp-1",
    tool_id,
    ...rest,
  };
}

export function makeSystemPromptVersion(
  overrides: Partial<SystemPromptVersion> &
    Pick<SystemPromptVersion, "id" | "kind">,
): SystemPromptVersion {
  const { id, kind, ...rest } = overrides;
  return {
    content: "你是 Athena 的主对话系统提示词。",
    created_at: "2026-11-11T01:00:00Z",
    created_by: "admin-1",
    id,
    kind,
    restored_from_version_id: null,
    ...rest,
  };
}

export function makeSystemPromptSummary(
  version: SystemPromptVersion,
): SystemPromptVersionSummary {
  const { content: _content, ...summary } = version;
  return summary;
}
