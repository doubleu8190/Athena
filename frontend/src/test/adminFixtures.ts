import type {
  AdminTool,
  CompressionPolicy,
  McpServer,
  ModelProfile,
  SystemPromptVersion,
  SystemPromptVersionSummary,
} from "../features/admin/model";
import type { User } from "../features/auth/model";
import type {
  DependencyCheck,
  Skill,
  SkillAdminDetails,
  SkillVersion,
} from "../features/skills/model";
import { makeSuperAdmin } from "./fixtures";

export const TEST_ADMIN: User = makeSuperAdmin({
  display_name: "管理员",
  email: "admin@example.com",
  id: "admin-1",
});

export function makeModelProfile(
  overrides: Partial<ModelProfile> & Pick<ModelProfile, "id" | "name">,
): ModelProfile {
  return {
    base_url: "https://llm.example/v1",
    context_window_tokens: 128_000,
    created_at: "2026-11-08T08:00:00Z",
    enabled: true,
    execution_timeout_seconds: 300,
    max_concurrency: 4,
    max_output_tokens: 8_192,
    model: "atlas-main",
    protocol: "chat_completions",
    request_timeout_seconds: 120,
    scenario_description: null,
    scenario_label: null,
    supports_parallel_tool_calls: false,
    supports_stream: true,
    supports_tools: true,
    supports_vision: false,
    temperature: null,
    tokenizer_encoding: "o200k_base",
    updated_at: "2026-11-08T08:00:00Z",
    version: 1,
    visible_to_users: true,
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
    recent_ratio: 0.2,
    recent_token_cap: 32_768,
    summary_ratio: 0.1,
    summary_token_cap: 8_192,
    trigger_tokens: 262_144,
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
    enabled: true,
    id,
    name,
    owner_department_id: null,
    secrets: [{ has_value: true, name: "Authorization" }],
    tools_discovered_at: "2026-11-10T03:00:00Z",
    transport: "streamable_http",
    updated_at: "2026-11-10T03:00:00Z",
    visibility_grants: [],
    visibility_scope: "company",
    ...rest,
  };
}

export function makeAdminTool(
  overrides: Partial<AdminTool> & Pick<AdminTool, "tool_id">,
): AdminTool {
  const { tool_id, ...rest } = overrides;
  return {
    approval_summary_template: null,
    description: "查询内部知识索引",
    display_name: null,
    input_schema: { type: "object" },
    model_name: "mcp__0123456789abcdef0123456789abcdef",
    parameter_labels: {},
    remote_name: "knowledge_search",
    requires_approval: false,
    result_labels: {},
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

export function makeDependencyCheck(
  overrides: Partial<DependencyCheck> = {},
): DependencyCheck {
  return {
    checked_at: "2026-11-12T01:00:00Z",
    items: [],
    python_implementation: "CPython",
    python_version: "3.14.0",
    status: "satisfied",
    ...overrides,
  };
}

export function makeSkillVersion(
  overrides: Partial<SkillVersion> &
    Pick<SkillVersion, "id" | "skill_id" | "ordinal">,
): SkillVersion {
  const { id, ordinal, skill_id, ...rest } = overrides;
  return {
    created_at: "2026-11-12T01:00:00Z",
    dependency_check: makeDependencyCheck(),
    dependencies: [],
    description: "生成月度销售报表与趋势图。",
    files: [
      {
        path: "SKILL.md",
        sha256: "a".repeat(64),
        size_bytes: 128,
        text: true,
      },
      {
        path: "scripts/build_report.py",
        sha256: "b".repeat(64),
        size_bytes: 64,
        text: true,
      },
    ],
    guide: "# 月度销售报表\n\n1. 读取输入文件。\n2. 生成交付。",
    id,
    label: `v${ordinal}`,
    name: "销售报表",
    ordinal,
    package_sha256: "c".repeat(64),
    scripts: [
      {
        label: "生成报表",
        parameters: { type: "object" },
        path: "scripts/build_report.py",
        purpose: "根据销售明细生成交付文件。",
      },
    ],
    skill_id,
    slug: "sales-report",
    ...rest,
  };
}

export function makeSkill(
  overrides: Partial<Skill> & Pick<Skill, "id">,
): Skill {
  const { id, ...rest } = overrides;
  return {
    current_version_id: "skill-version-1",
    current_version_label: "v1",
    dependency_check: makeDependencyCheck(),
    description: "生成月度销售报表与趋势图。",
    enabled: false,
    featured: false,
    format: "package",
    has_scripts: true,
    id,
    name: "销售报表",
    owner_department_id: null,
    revision: 1,
    scope: "company",
    script_requires_approval: false,
    slug: "sales-report",
    starter_prompt: null,
    updated_at: "2026-11-12T01:00:00Z",
    visibility_grants: [],
    ...rest,
  };
}

export function makeSkillDetails(
  overrides: Partial<SkillAdminDetails> = {},
  versionOverrides: Partial<SkillVersion> = {},
): SkillAdminDetails {
  const skill = makeSkill({
    id: "skill-1",
    ...overrides,
  });
  return {
    ...skill,
    versions: [
      makeSkillVersion({
        id: skill.current_version_id ?? "skill-version-1",
        ordinal: 1,
        skill_id: skill.id,
        ...versionOverrides,
      }),
    ],
  };
}
