import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../../shared/api/client";
import {
  getRequestUrl,
  listResponse,
} from "../../test/fixtures";
import {
  makeAdminTool,
  makeCompressionPolicy,
  makeMcpServer,
  makeModelProfile,
} from "../../test/adminFixtures";
import { jsonResponse } from "../../test/testUtils";
import {
  createMcpServer,
  createModelProfile,
  getCompressionPolicy,
  getMcpServer,
  listAdminTools,
  listMcpServers,
  listModelProfiles,
  setDefaultModelProfile,
  setModelProfileEnabled,
  updateCompressionPolicy,
  updateMcpServer,
  updateToolApprovalPolicy,
} from "./api";
import { adminErrorMessage, isMcpConflict } from "./errorMessages";
import type {
  McpServerWriteInput,
  ModelProfileCreateInput,
} from "./model";

describe("管理 API 契约", () => {
  it("读取模型版本并提交不可变版本、启停和默认指向", async () => {
    const profile = makeModelProfile({
      id: "model-primary",
      name: "主力模型",
    });
    const list = {
      default_profile_id: profile.id,
      items: [profile],
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(list))
      .mockResolvedValueOnce(jsonResponse(profile, 201))
      .mockResolvedValueOnce(jsonResponse({ ...profile, enabled: false }))
      .mockResolvedValueOnce(jsonResponse(profile));
    vi.stubGlobal("fetch", fetchMock);
    const input: ModelProfileCreateInput = {
      base_url: profile.base_url,
      context_window_tokens: profile.context_window_tokens,
      credential_env: profile.credential_env,
      execution_timeout_seconds: profile.execution_timeout_seconds,
      max_concurrency: profile.max_concurrency,
      max_output_tokens: profile.max_output_tokens,
      model: profile.model,
      name: profile.name,
      request_timeout_seconds: profile.request_timeout_seconds,
      supports_parallel_tool_calls:
        profile.supports_parallel_tool_calls,
      supports_stream: profile.supports_stream,
      supports_tools: profile.supports_tools,
      temperature: profile.temperature,
      tokenizer_encoding: profile.tokenizer_encoding,
    };

    await expect(listModelProfiles()).resolves.toEqual(list);
    await createModelProfile(input);
    await setModelProfileEnabled(profile.id, false);
    await setDefaultModelProfile(profile.id);

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/v1/admin/llm/model-profiles",
    );
    expect(fetchMock.mock.calls[1][1]).toMatchObject({
      body: JSON.stringify(input),
      method: "POST",
    });
    expect(getRequestUrl(fetchMock.mock.calls[2][0])).toBe(
      `/api/v1/admin/llm/model-profiles/${profile.id}/enabled`,
    );
    expect(fetchMock.mock.calls[2][1]).toMatchObject({
      body: JSON.stringify({ enabled: false }),
      method: "PUT",
    });
    expect(fetchMock.mock.calls[3][1]).toMatchObject({
      body: JSON.stringify({ profile_id: profile.id }),
      method: "PUT",
    });
  });

  it("读取并保存真实压缩策略", async () => {
    const unconfigured = makeCompressionPolicy({
      compression_model_profile_id: null,
      created_at: null,
      id: null,
      prompt_template_version: null,
      recent_ratio: null,
      summary_ratio: null,
      trigger_ratio: null,
    });
    const configured = makeCompressionPolicy();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(unconfigured))
      .mockResolvedValueOnce(jsonResponse(configured));
    vi.stubGlobal("fetch", fetchMock);
    const input = {
      compression_model_profile_id: "model-primary",
      recent_ratio: 0.2,
      summary_ratio: 0.1,
      trigger_ratio: 0.8,
    };

    await expect(getCompressionPolicy()).resolves.toEqual(unconfigured);
    await expect(updateCompressionPolicy(input)).resolves.toEqual(
      configured,
    );

    expect(fetchMock.mock.calls[0]).toEqual([
      "/api/v1/admin/llm/compression-policy",
      expect.objectContaining({ method: "GET" }),
    ]);
    expect(fetchMock.mock.calls[1][1]).toMatchObject({
      body: JSON.stringify(input),
      method: "PUT",
    });
  });

  it("MCP 分页使用真实 total，更新携带 expected_config_version", async () => {
    const server = makeMcpServer({
      id: "mcp-1",
      name: "检索服务",
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse(listResponse([server], 2, 10, 11)),
      )
      .mockResolvedValueOnce(jsonResponse(server))
      .mockResolvedValueOnce(jsonResponse(server, 201))
      .mockResolvedValueOnce(
        jsonResponse({ ...server, config_version: 2 }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const createInput: McpServerWriteInput = {
      name: "检索服务",
      secrets: { Authorization: "Bearer secret" },
      transport: "streamable_http",
      url: "https://mcp.example/mcp",
    };

    const page = await listMcpServers({ page: 2, pageSize: 10 });
    await getMcpServer(server.id);
    await createMcpServer(createInput);
    await updateMcpServer(server.id, {
      expected_config_version: 1,
      server: createInput,
    });

    expect(page.total).toBe(11);
    expect(getRequestUrl(fetchMock.mock.calls[0][0])).toBe(
      "/api/v1/admin/mcp/servers?page=2&page_size=10",
    );
    expect(getRequestUrl(fetchMock.mock.calls[1][0])).toBe(
      `/api/v1/admin/mcp/servers/${server.id}`,
    );
    expect(fetchMock.mock.calls[2][1]).toMatchObject({
      body: JSON.stringify(createInput),
      method: "POST",
    });
    expect(fetchMock.mock.calls[3][1]).toMatchObject({
      body: JSON.stringify({
        ...createInput,
        expected_config_version: 1,
      }),
      method: "PUT",
    });
  });

  it("工具目录使用真实分页并只 PATCH 审批字段", async () => {
    const tool = makeAdminTool({ tool_id: "tool-1" });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse(listResponse([tool], 1, 20, 1)),
      )
      .mockResolvedValueOnce(
        jsonResponse({ ...tool, requires_approval: true }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await listAdminTools({ page: 1, pageSize: 20 });
    await updateToolApprovalPolicy(tool.tool_id, true);

    expect(getRequestUrl(fetchMock.mock.calls[0][0])).toBe(
      "/api/v1/admin/tools?page=1&page_size=20",
    );
    expect(fetchMock.mock.calls[1][1]).toMatchObject({
      body: JSON.stringify({ requires_approval: true }),
      method: "PATCH",
    });
  });

  it("映射管理错误且不泄露后端原始异常文本", () => {
    expect(
      adminErrorMessage(new ApiError(403, "admin_required")),
    ).toContain("权限");
    expect(
      adminErrorMessage(new ApiError(409, "mcp_conflict")),
    ).toContain("刷新");
    expect(
      adminErrorMessage(new ApiError(422, "mcp_request_invalid")),
    ).toContain("transport");
    expect(
      adminErrorMessage(new ApiError(502, "mcp_discovery_failed")),
    ).toContain("连接");
    expect(
      adminErrorMessage(new ApiError(504, "mcp_discovery_timeout")),
    ).toContain("超时");
    expect(
      adminErrorMessage(new ApiError(422, "raw-secret-value")),
    ).not.toContain("raw-secret-value");
    expect(isMcpConflict(new ApiError(409, "mcp_conflict"))).toBe(true);
  });
});
