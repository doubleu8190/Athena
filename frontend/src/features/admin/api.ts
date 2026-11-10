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

