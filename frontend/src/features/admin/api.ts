import { apiRequest } from "../../shared/api/client";
import type {
  AdminPageQuery,
  AdminTool,
  AdminToolListResponse,
  CompressionPolicy,
  CompressionPolicyInput,
  McpServer,
  McpServerListResponse,
  McpServerUpdateInput,
  McpServerWriteInput,
  ModelProfile,
  ModelProfileCreateInput,
  ModelProfileListResponse,
  OrgDepartmentListResponse,
  SubagentAvailableToolsResponse,
  SubagentCreateInput,
  SubagentListResponse,
  SubagentProfile,
  SubagentUpdateInput,
  SystemPromptKind,
  SystemPromptVersion,
  SystemPromptVersionListResponse,
} from "./model";

function pageQuery(query: AdminPageQuery): string {
  return `?page=${query.page}&page_size=${query.pageSize}`;
}

export function listModelProfiles(): Promise<ModelProfileListResponse> {
  return apiRequest<ModelProfileListResponse>(
    "/admin/llm/model-profiles",
    { method: "GET" },
  );
}

export function createModelProfile(
  input: ModelProfileCreateInput,
): Promise<ModelProfile> {
  return apiRequest<ModelProfile>("/admin/llm/model-profiles", {
    method: "POST",
    body: input,
  });
}

export function setModelProfileEnabled(
  profileId: string,
  enabled: boolean,
): Promise<ModelProfile> {
  return apiRequest<ModelProfile>(
    `/admin/llm/model-profiles/${encodeURIComponent(profileId)}/enabled`,
    { method: "PUT", body: { enabled } },
  );
}

export function setDefaultModelProfile(
  profileId: string,
): Promise<ModelProfile> {
  return apiRequest<ModelProfile>(
    "/admin/llm/model-profiles/default",
    { method: "PUT", body: { profile_id: profileId } },
  );
}

export function getCompressionPolicy(): Promise<CompressionPolicy> {
  return apiRequest<CompressionPolicy>(
    "/admin/llm/compression-policy",
    { method: "GET" },
  );
}

export function updateCompressionPolicy(
  input: CompressionPolicyInput,
): Promise<CompressionPolicy> {
  return apiRequest<CompressionPolicy>(
    "/admin/llm/compression-policy",
    { method: "PUT", body: input },
  );
}

export function listMcpServers(
  query: AdminPageQuery,
): Promise<McpServerListResponse> {
  return apiRequest<McpServerListResponse>(
    `/admin/mcp/servers${pageQuery(query)}`,
    { method: "GET" },
  );
}

export function getMcpServer(serverId: string): Promise<McpServer> {
  return apiRequest<McpServer>(
    `/admin/mcp/servers/${encodeURIComponent(serverId)}`,
    { method: "GET" },
  );
}

export function createMcpServer(
  input: McpServerWriteInput,
): Promise<McpServer> {
  return apiRequest<McpServer>("/admin/mcp/servers", {
    method: "POST",
    body: input,
  });
}

export function updateMcpServer(
  serverId: string,
  input: McpServerUpdateInput,
): Promise<McpServer> {
  return apiRequest<McpServer>(
    `/admin/mcp/servers/${encodeURIComponent(serverId)}`,
    {
      method: "PUT",
      body: {
        ...input.server,
        expected_config_version: input.expected_config_version,
      },
    },
  );
}

export function listAdminTools(
  query: AdminPageQuery,
): Promise<AdminToolListResponse> {
  return apiRequest<AdminToolListResponse>(
    `/admin/tools${pageQuery(query)}`,
    { method: "GET" },
  );
}

export function updateToolApprovalPolicy(
  toolId: string,
  requiresApproval: boolean,
): Promise<AdminTool> {
  return apiRequest<AdminTool>(
    `/admin/tools/${encodeURIComponent(toolId)}`,
    {
      method: "PATCH",
      body: { requires_approval: requiresApproval },
    },
  );
}

export function getSystemPrompt(
  kind: SystemPromptKind,
): Promise<SystemPromptVersion> {
  return apiRequest<SystemPromptVersion>(
    `/admin/system-prompts/${kind}`,
    { method: "GET" },
  );
}

export function listSystemPromptVersions(
  kind: SystemPromptKind,
  query: AdminPageQuery,
): Promise<SystemPromptVersionListResponse> {
  return apiRequest<SystemPromptVersionListResponse>(
    `/admin/system-prompts/${kind}/versions${pageQuery(query)}`,
    { method: "GET" },
  );
}

export function getSystemPromptVersion(
  kind: SystemPromptKind,
  versionId: string,
): Promise<SystemPromptVersion> {
  return apiRequest<SystemPromptVersion>(
    `/admin/system-prompts/${kind}/versions/${encodeURIComponent(versionId)}`,
    { method: "GET" },
  );
}

export function saveSystemPrompt(
  kind: SystemPromptKind,
  content: string,
  expectedCurrentVersionId: string,
): Promise<SystemPromptVersion> {
  return apiRequest<SystemPromptVersion>(
    `/admin/system-prompts/${kind}/versions`,
    {
      method: "POST",
      body: {
        content,
        expected_current_version_id: expectedCurrentVersionId,
      },
    },
  );
}

export function restoreSystemPrompt(
  kind: SystemPromptKind,
  sourceVersionId: string,
  expectedCurrentVersionId: string,
): Promise<SystemPromptVersion> {
  return apiRequest<SystemPromptVersion>(
    `/admin/system-prompts/${kind}/restore`,
    {
      method: "POST",
      body: {
        source_version_id: sourceVersionId,
        expected_current_version_id: expectedCurrentVersionId,
      },
    },
  );
}

// ---------------- Subagent 管理 API ----------------

export function listSubagents(
  query: AdminPageQuery & { enabled?: boolean },
): Promise<SubagentListResponse> {
  const offset = Math.max(0, (query.page - 1) * query.pageSize);
  const params = new URLSearchParams({
    offset: String(offset),
    limit: String(query.pageSize),
  });
  if (query.enabled !== undefined) {
    params.set("enabled", String(query.enabled));
  }
  return apiRequest<SubagentListResponse>(
    `/admin/subagents?${params.toString()}`,
    { method: "GET" },
  );
}

export function getSubagent(subagentId: string): Promise<SubagentProfile> {
  return apiRequest<SubagentProfile>(
    `/admin/subagents/${encodeURIComponent(subagentId)}`,
    { method: "GET" },
  );
}

export function createSubagent(
  input: SubagentCreateInput,
): Promise<SubagentProfile> {
  return apiRequest<SubagentProfile>("/admin/subagents", {
    method: "POST",
    body: input,
  });
}

export function updateSubagent(
  subagentId: string,
  input: SubagentUpdateInput,
): Promise<SubagentProfile> {
  return apiRequest<SubagentProfile>(
    `/admin/subagents/${encodeURIComponent(subagentId)}`,
    {
      method: "PATCH",
      body: input,
    },
  );
}

export function setSubagentEnabled(
  subagentId: string,
  enabled: boolean,
): Promise<SubagentProfile> {
  return apiRequest<SubagentProfile>(
    `/admin/subagents/${encodeURIComponent(subagentId)}/enabled`,
    {
      method: "PUT",
      body: { enabled },
    },
  );
}

export function deleteSubagent(subagentId: string): Promise<void> {
  return apiRequest<void>(
    `/admin/subagents/${encodeURIComponent(subagentId)}`,
    { method: "DELETE" },
  );
}

export function listSubagentAvailableTools(): Promise<SubagentAvailableToolsResponse> {
  return apiRequest<SubagentAvailableToolsResponse>(
    "/admin/subagents/available-tools",
    { method: "GET" },
  );
}

/** 查询企业组织部门树扁平列表（仅超管可调用，供部门共享知识库与部门技能选择器复用）。 */
export function listAdminOrgDepartments(
  includeArchived = false,
): Promise<OrgDepartmentListResponse> {
  return apiRequest<OrgDepartmentListResponse>(
    `/admin/org/departments?include_archived=${String(includeArchived)}`,
    { method: "GET" },
  );
}

