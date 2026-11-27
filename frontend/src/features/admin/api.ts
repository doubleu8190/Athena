import {
  apiDelete,
  apiGet,
  apiPatch,
  apiPost,
  apiPut,
  type ApiSchemas,
} from "../../shared/api/client";
import type {
  AdminPageQuery,
  AdminTool,
  AdminToolBatchApprovalInput,
  AdminToolBatchApprovalResponse,
  AdminToolListResponse,
  AdminToolUpdateInput,
  CompressionPolicy,
  CompressionPolicyInput,
  McpServer,
  McpServerEnabledInput,
  McpServerListResponse,
  McpServerPatchInput,
  McpServerUpdateInput,
  McpServerWriteInput,
  ModelProfile,
  ModelProfileCreateInput,
  ModelProfileListResponse,
  ModelProfileUpdateInput,
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

export function listModelProfiles(): Promise<ModelProfileListResponse> {
  return apiGet("/api/v1/admin/llm/model-profiles");
}

export function createModelProfile(
  input: ModelProfileCreateInput,
): Promise<ModelProfile> {
  return apiPost("/api/v1/admin/llm/model-profiles", {
    body: input,
  });
}

export function updateModelProfile(
  profileId: string,
  input: ModelProfileUpdateInput,
): Promise<ModelProfile> {
  return apiPatch("/api/v1/admin/llm/model-profiles/{profile_id}", {
    body: input,
    params: { path: { profile_id: profileId } },
  });
}

export function deleteModelProfile(profileId: string): Promise<void> {
  return apiDelete("/api/v1/admin/llm/model-profiles/{profile_id}", {
    params: { path: { profile_id: profileId } },
  });
}

export function setModelProfileEnabled(
  profileId: string,
  enabled: boolean,
): Promise<ModelProfile> {
  return apiPut("/api/v1/admin/llm/model-profiles/{profile_id}/enabled", {
    body: { enabled },
    params: { path: { profile_id: profileId } },
  });
}

export function setDefaultModelProfile(
  profileId: string,
): Promise<ModelProfile> {
  return apiPut("/api/v1/admin/llm/model-profiles/default", {
    body: { profile_id: profileId },
  });
}

export function getCompressionPolicy(): Promise<CompressionPolicy> {
  return apiGet("/api/v1/admin/llm/compression-policy");
}

export function updateCompressionPolicy(
  input: CompressionPolicyInput,
): Promise<CompressionPolicy> {
  return apiPut("/api/v1/admin/llm/compression-policy", {
    body: input,
  });
}

export function listMcpServers(
  query: AdminPageQuery,
): Promise<McpServerListResponse> {
  return apiGet("/api/v1/admin/mcp/servers", {
    params: { query: { page: query.page, page_size: query.pageSize } },
  });
}

export function getMcpServer(serverId: string): Promise<McpServer> {
  return apiGet("/api/v1/admin/mcp/servers/{server_id}", {
    params: { path: { server_id: serverId } },
  });
}

export function createMcpServer(
  input: McpServerWriteInput,
): Promise<McpServer> {
  return apiPost("/api/v1/admin/mcp/servers", {
    body: input,
  });
}

export function updateMcpServer(
  serverId: string,
  input: McpServerUpdateInput,
): Promise<McpServer> {
  // 按传输类型收窄判别联合，确保 OpenAPI 请求体在编译期受严格字段校验。
  const body:
    | ApiSchemas["HttpServerUpdateRequest"]
    | ApiSchemas["StdioServerUpdateRequest"] =
    input.server.transport === "streamable_http"
      ? {
          ...input.server,
          expected_config_version: input.expected_config_version,
        }
      : {
          ...input.server,
          expected_config_version: input.expected_config_version,
        };
  return apiPut("/api/v1/admin/mcp/servers/{server_id}", {
    body,
    params: { path: { server_id: serverId } },
  });
}

export function patchMcpServer(
  serverId: string,
  input: McpServerPatchInput,
): Promise<McpServer> {
  return apiPatch("/api/v1/admin/mcp/servers/{server_id}", {
    body: input,
    params: { path: { server_id: serverId } },
  });
}

export function setMcpServerEnabled(
  serverId: string,
  input: McpServerEnabledInput,
): Promise<McpServer> {
  return apiPut("/api/v1/admin/mcp/servers/{server_id}/enabled", {
    body: input,
    params: { path: { server_id: serverId } },
  });
}

export function rediscoverMcpServer(serverId: string): Promise<McpServer> {
  return apiPost("/api/v1/admin/mcp/servers/{server_id}/rediscover", {
    params: { path: { server_id: serverId } },
  });
}

export function deleteMcpServer(serverId: string): Promise<void> {
  return apiDelete("/api/v1/admin/mcp/servers/{server_id}", {
    params: { path: { server_id: serverId } },
  });
}

export function listAdminTools(
  query: AdminPageQuery,
): Promise<AdminToolListResponse> {
  return apiGet("/api/v1/admin/tools", {
    params: { query: { page: query.page, page_size: query.pageSize } },
  });
}

export function updateToolApprovalPolicy(
  toolId: string,
  requiresApproval: boolean,
): Promise<AdminTool> {
  return apiPatch("/api/v1/admin/tools/{tool_id}", {
    body: { requires_approval: requiresApproval },
    params: { path: { tool_id: toolId } },
  });
}

export function updateAdminToolMetadata(
  toolId: string,
  input: AdminToolUpdateInput,
): Promise<AdminTool> {
  return apiPatch("/api/v1/admin/tools/{tool_id}", {
    body: input,
    params: { path: { tool_id: toolId } },
  });
}

export function batchUpdateAdminToolApproval(
  input: AdminToolBatchApprovalInput,
): Promise<AdminToolBatchApprovalResponse> {
  return apiPatch("/api/v1/admin/tools/batch-approval", {
    body: input,
  });
}

export function getSystemPrompt(
  kind: SystemPromptKind,
): Promise<SystemPromptVersion> {
  return apiGet("/api/v1/admin/system-prompts/{kind}", {
    params: { path: { kind } },
  });
}

export function listSystemPromptVersions(
  kind: SystemPromptKind,
  query: AdminPageQuery,
): Promise<SystemPromptVersionListResponse> {
  return apiGet("/api/v1/admin/system-prompts/{kind}/versions", {
    params: {
      path: { kind },
      query: { page: query.page, page_size: query.pageSize },
    },
  });
}

export function getSystemPromptVersion(
  kind: SystemPromptKind,
  versionId: string,
): Promise<SystemPromptVersion> {
  return apiGet("/api/v1/admin/system-prompts/{kind}/versions/{version_id}", {
    params: { path: { kind, version_id: versionId } },
  });
}

export function saveSystemPrompt(
  kind: SystemPromptKind,
  content: string,
  expectedCurrentVersionId: string,
): Promise<SystemPromptVersion> {
  return apiPost("/api/v1/admin/system-prompts/{kind}/versions", {
    body: {
      content,
      expected_current_version_id: expectedCurrentVersionId,
    },
    params: { path: { kind } },
  });
}

export function restoreSystemPrompt(
  kind: SystemPromptKind,
  sourceVersionId: string,
  expectedCurrentVersionId: string,
): Promise<SystemPromptVersion> {
  return apiPost("/api/v1/admin/system-prompts/{kind}/restore", {
    body: {
      expected_current_version_id: expectedCurrentVersionId,
      source_version_id: sourceVersionId,
    },
    params: { path: { kind } },
  });
}

// ---------------- Subagent 管理 API ----------------

export function listSubagents(
  query: AdminPageQuery & { enabled?: boolean },
): Promise<SubagentListResponse> {
  const offset = Math.max(0, (query.page - 1) * query.pageSize);
  const queryParams: { offset: number; limit: number; enabled?: boolean } = {
    offset,
    limit: query.pageSize,
  };
  if (query.enabled !== undefined) {
    queryParams.enabled = query.enabled;
  }
  return apiGet("/api/v1/admin/subagents", {
    params: { query: queryParams },
  });
}

export function getSubagent(subagentId: string): Promise<SubagentProfile> {
  return apiGet("/api/v1/admin/subagents/{subagent_id}", {
    params: { path: { subagent_id: subagentId } },
  });
}

export function createSubagent(
  input: SubagentCreateInput,
): Promise<SubagentProfile> {
  return apiPost("/api/v1/admin/subagents", {
    body: input,
  });
}

export function updateSubagent(
  subagentId: string,
  input: SubagentUpdateInput,
): Promise<SubagentProfile> {
  return apiPatch("/api/v1/admin/subagents/{subagent_id}", {
    body: input,
    params: { path: { subagent_id: subagentId } },
  });
}

export function setSubagentEnabled(
  subagentId: string,
  enabled: boolean,
): Promise<SubagentProfile> {
  return apiPut("/api/v1/admin/subagents/{subagent_id}/enabled", {
    body: { enabled },
    params: { path: { subagent_id: subagentId } },
  });
}

export function deleteSubagent(subagentId: string): Promise<void> {
  return apiDelete("/api/v1/admin/subagents/{subagent_id}", {
    params: { path: { subagent_id: subagentId } },
  });
}

export function listSubagentAvailableTools(): Promise<SubagentAvailableToolsResponse> {
  return apiGet("/api/v1/admin/subagents/available-tools");
}

/** 查询企业组织部门树扁平列表（仅超管可调用，供部门共享知识库与部门技能选择器复用）。 */
export function listAdminOrgDepartments(
  includeArchived = false,
): Promise<OrgDepartmentListResponse> {
  return apiGet("/api/v1/admin/org/departments", {
    params: { query: { include_archived: includeArchived } },
  });
}
