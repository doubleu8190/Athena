import {
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import {
  act,
  renderHook,
  waitFor,
} from "@testing-library/react";
import type { PropsWithChildren } from "react";
import { describe, expect, it, vi } from "vitest";

import {
  makeAdminTool,
  makeMcpServer,
  makeSystemPromptVersion,
  TEST_ADMIN,
} from "../../test/adminFixtures";
import { listResponse } from "../../test/fixtures";
import {
  createTestQueryClient,
  jsonResponse,
} from "../../test/testUtils";
import {
  clearAuthenticatedIdentity,
  setAuthenticatedIdentity,
} from "../auth/cacheBoundary";
import { getAuthIdentityEpoch } from "../../shared/api/client";
import {
  useCreateMcpServerMutation,
  useSaveSystemPromptMutation,
  useUpdateToolApprovalMutation,
  useUpdateMcpServerMutation,
} from "./hooks";
import type {
  AdminToolListResponse,
  McpServerUpdateInput,
  McpServerWriteInput,
} from "./model";
import { adminKeys } from "./queryKeys";

const TOOL_QUERY = { page: 1, pageSize: 20 };
const SERVER_QUERY = { page: 1, pageSize: 10 };
const ADMIN_B = {
  ...TEST_ADMIN,
  display_name: "管理员 B",
  email: "admin-b@example.com",
  id: "admin-b",
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function wrapper(client: QueryClient) {
  return function QueryWrapper({ children }: PropsWithChildren) {
    return (
      <QueryClientProvider client={client}>
        {children}
      </QueryClientProvider>
    );
  };
}

function seedCatalogs(client: QueryClient): void {
  client.setQueryData(
    adminKeys.mcpServers(SERVER_QUERY),
    listResponse([], 1, 10, 0),
  );
  client.setQueryData(
    adminKeys.tools(TOOL_QUERY),
    listResponse(
      [makeAdminTool({ tool_id: "tool-before-discovery" })],
      1,
      20,
      1,
    ),
  );
}

function expectCatalogsInvalidated(client: QueryClient): void {
  expect(
    client.getQueryState(adminKeys.mcpServers(SERVER_QUERY))
      ?.isInvalidated,
  ).toBe(true);
  expect(
    client.getQueryState(adminKeys.tools(TOOL_QUERY))
      ?.isInvalidated,
  ).toBe(true);
}

const WRITE_INPUT: McpServerWriteInput = {
  name: "新的检索服务",
  secrets: { Authorization: "Bearer secret" },
  transport: "streamable_http",
  url: "https://mcp.example/mcp",
};

describe("MCP 管理缓存失效", () => {
  it("创建成功同时失效 Server 与工具目录", async () => {
    const client = createTestQueryClient();
    seedCatalogs(client);
    const server = makeMcpServer({
      id: "mcp-created",
      name: WRITE_INPUT.name,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse(server, 201)),
    );
    const { result } = renderHook(
      () => useCreateMcpServerMutation(),
      { wrapper: wrapper(client) },
    );

    await act(async () => {
      await result.current.mutateAsync(WRITE_INPUT);
    });

    expectCatalogsInvalidated(client);
  });

  it("完整替换成功同时失效 Server 与工具目录", async () => {
    const client = createTestQueryClient();
    seedCatalogs(client);
    const server = makeMcpServer({
      config_version: 2,
      id: "mcp-updated",
      name: WRITE_INPUT.name,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse(server)),
    );
    const { result } = renderHook(
      () => useUpdateMcpServerMutation(server.id),
      { wrapper: wrapper(client) },
    );
    const updateInput: McpServerUpdateInput = {
      expected_config_version: 1,
      server: WRITE_INPUT,
    };

    await act(async () => {
      await result.current.mutateAsync(updateInput);
    });

    expectCatalogsInvalidated(client);
  });
});

describe("工具审批身份边界", () => {
  it("延迟取消查询期间切换身份会在发送 PATCH 前中止", async () => {
    const client = createTestQueryClient();
    const toolA = makeAdminTool({
      model_name: "mcp__aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      requires_approval: false,
      tool_id: "shared-tool",
    });
    const toolB = makeAdminTool({
      description: "B 管理员工具快照",
      model_name: "mcp__bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      requires_approval: true,
      tool_id: "shared-tool",
    });
    client.setQueryData(
      adminKeys.tools(TOOL_QUERY),
      listResponse([toolA], 1, 20, 1),
    );

    const cancelGate = deferred<void>();
    const cancelSpy = vi
      .spyOn(client, "cancelQueries")
      .mockReturnValue(cancelGate.promise);
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(
      () => useUpdateToolApprovalMutation(),
      { wrapper: wrapper(client) },
    );

    let mutationPromise!: Promise<unknown>;
    const identityEpoch = getAuthIdentityEpoch();
    act(() => {
      mutationPromise = result.current.mutateAsync({
        identityEpoch,
        requiresApproval: true,
        toolId: toolA.tool_id,
      });
    });
    await waitFor(() => {
      expect(cancelSpy).toHaveBeenCalled();
    });

    act(() => {
      clearAuthenticatedIdentity(client);
      setAuthenticatedIdentity(client, ADMIN_B);
      client.setQueryData(
        adminKeys.tools(TOOL_QUERY),
        listResponse([toolB], 1, 20, 1),
      );
    });

    await act(async () => {
      cancelGate.resolve();
      await expect(mutationPromise).rejects.toMatchObject({
        name: "AbortError",
      });
    });

    const bTools = listResponse([toolB], 1, 20, 1);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(
      client.getQueryData(adminKeys.tools(TOOL_QUERY)),
    ).toEqual(bTools);
    expect(
      client.getQueryState(adminKeys.tools(TOOL_QUERY))
        ?.isInvalidated,
    ).toBe(false);
  });

  it("旧身份的迟到失败不会回滚 A 快照或覆盖 B 缓存", async () => {
    const client = createTestQueryClient();
    const toolA = makeAdminTool({
      model_name: "mcp__aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      requires_approval: false,
      tool_id: "shared-tool",
    });
    const toolB = makeAdminTool({
      description: "B 管理员看到的工具",
      model_name: "mcp__bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      requires_approval: true,
      tool_id: "shared-tool",
    });
    client.setQueryData(
      adminKeys.tools(TOOL_QUERY),
      listResponse([toolA], 1, 20, 1),
    );
    const patchGate = deferred<Response>();
    let patchStarted = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        patchStarted = true;
        return patchGate.promise;
      }),
    );
    const { result } = renderHook(
      () => useUpdateToolApprovalMutation(),
      { wrapper: wrapper(client) },
    );

    let mutationPromise!: Promise<unknown>;
    const identityEpoch = getAuthIdentityEpoch();
    act(() => {
      mutationPromise = result.current.mutateAsync({
        identityEpoch,
        requiresApproval: true,
        toolId: toolA.tool_id,
      });
    });
    await waitFor(() => {
      expect(patchStarted).toBe(true);
    });
    expect(
      client.getQueryData<AdminToolListResponse>(
        adminKeys.tools(TOOL_QUERY),
      )?.items[0]?.requires_approval,
    ).toBe(true);

    act(() => {
      clearAuthenticatedIdentity(client);
      setAuthenticatedIdentity(client, ADMIN_B);
      client.setQueryData(
        adminKeys.tools(TOOL_QUERY),
        listResponse([toolB], 1, 20, 1),
      );
    });

    await act(async () => {
      patchGate.resolve(
        jsonResponse({ detail: "mcp_tool_policy_invalid" }, 422),
      );
      await expect(mutationPromise).rejects.toMatchObject({
        name: "AbortError",
      });
    });

    const bTools = listResponse([toolB], 1, 20, 1);
    expect(
      client.getQueryData(adminKeys.tools(TOOL_QUERY)),
    ).toEqual(bTools);
    expect(
      client.getQueryState(adminKeys.tools(TOOL_QUERY))
        ?.isInvalidated,
    ).toBe(false);
  });
});

describe("系统提示词保存身份边界", () => {
  it("旧管理员迟到响应不会覆盖新管理员的当前正文缓存", async () => {
    const client = createTestQueryClient();
    const promptA = makeSystemPromptVersion({
      content: "A 管理员看到的正文",
      id: "prompt-a",
      kind: "main",
    });
    const promptB = makeSystemPromptVersion({
      content: "B 管理员看到的正文",
      id: "prompt-b",
      kind: "main",
    });
    client.setQueryData(adminKeys.systemPrompt("main"), promptA);
    const saveGate = deferred<Response>();
    vi.stubGlobal(
      "fetch",
      vi.fn(() => saveGate.promise),
    );
    const { result } = renderHook(
      () => useSaveSystemPromptMutation("main"),
      { wrapper: wrapper(client) },
    );

    let mutationPromise!: Promise<unknown>;
    const identityEpoch = getAuthIdentityEpoch();
    act(() => {
      mutationPromise = result.current.mutateAsync({
        content: "A 的新正文",
        expectedCurrentVersionId: promptA.id,
        identityEpoch,
      });
    });
    await waitFor(() => {
      expect(fetch).toHaveBeenCalledTimes(1);
    });

    act(() => {
      clearAuthenticatedIdentity(client);
      setAuthenticatedIdentity(client, ADMIN_B);
      client.setQueryData(adminKeys.systemPrompt("main"), promptB);
    });
    await act(async () => {
      saveGate.resolve(
        jsonResponse({
          ...promptA,
          content: "A 已保存但已迟到的正文",
          id: "prompt-a-saved",
        }),
      );
      await expect(mutationPromise).rejects.toMatchObject({
        name: "AbortError",
      });
    });

    expect(
      client.getQueryData(adminKeys.systemPrompt("main")),
    ).toEqual(promptB);
    expect(
      client.getQueryState(adminKeys.systemPromptVersionsAll("main"))
        ?.isInvalidated ?? false,
    ).toBe(false);
  });
});
