import {
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  getRequestUrl,
  listResponse,
  PROJECT_A,
  TEST_USER,
} from "../../test/fixtures";
import {
  makeAdminTool,
  TEST_ADMIN,
} from "../../test/adminFixtures";
import { jsonResponse, renderApp } from "../../test/testUtils";
import { projectKeys } from "../projects/queryKeys";
import { adminKeys } from "./queryKeys";

function requestUrl(input: RequestInfo | URL): URL {
  return new URL(getRequestUrl(input), "http://localhost");
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

const TOOL_A = makeAdminTool({
  description: "查询内部知识库",
  model_name: "mcp__aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  remote_name: "knowledge_search",
  requires_approval: false,
  tool_id: "tool-a",
});
const TOOL_B = makeAdminTool({
  description: "创建外部工单",
  model_name: "mcp__bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  remote_name: "ticket_create",
  requires_approval: true,
  server_id: "mcp-2",
  tool_id: "tool-b",
});

describe("管理工具页", () => {
  it("展示真实字段与 total，并按后端 total 翻页", async () => {
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (
          url.pathname === "/api/v1/admin/tools" &&
          (!init?.method || init.method === "GET")
        ) {
          const page = Number(url.searchParams.get("page"));
          return jsonResponse(
            listResponse(
              page === 1 ? [TOOL_A] : [TOOL_B],
              page,
              20,
              21,
            ),
          );
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/admin/tools");

    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "工具审批策略",
      }),
    ).toBeInTheDocument();
    expect(await screen.findByText("后端返回 21 个工具")).toBeInTheDocument();
    expect(screen.getByText(TOOL_A.model_name)).toBeInTheDocument();
    expect(screen.getByText(TOOL_A.description)).toBeInTheDocument();
    expect(screen.getByText(TOOL_A.tool_id)).toBeInTheDocument();
    expect(screen.getByText(TOOL_A.server_id)).toBeInTheDocument();
    expect(screen.getByText(TOOL_A.remote_name)).toBeInTheDocument();
    expect(
      screen.getByRole("switch", {
        name: `${TOOL_A.model_name} 需要审批`,
      }),
    ).toHaveAttribute("aria-checked", "false");
    expect(
      screen.getByRole("button", { name: "批量开启审批" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "批量关闭审批" }),
    ).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "下一页" }));
    expect(await screen.findByText(TOOL_B.model_name)).toBeInTheDocument();
    expect(
      screen.getByRole("switch", {
        name: `${TOOL_B.model_name} 需要审批`,
      }),
    ).toHaveAttribute("aria-checked", "true");
  });

  it("页码超过真实末页时按 total 回到最后有效页", async () => {
    const requestedPages: number[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = requestUrl(input);
          if (url.pathname.endsWith("/auth/me")) {
            return jsonResponse(TEST_ADMIN);
          }
          if (
            url.pathname === "/api/v1/admin/tools" &&
            (!init?.method || init.method === "GET")
          ) {
            const page = Number(url.searchParams.get("page"));
            requestedPages.push(page);
            return jsonResponse(
              listResponse(
                page === 2
                  ? [TOOL_B]
                  : [
                      makeAdminTool({
                        tool_id: "stale-tool",
                        model_name:
                          "mcp__ffffffffffffffffffffffffffffffff",
                      }),
                    ],
                page,
                20,
                21,
              ),
            );
          }
          throw new Error(
            `Unexpected request: ${init?.method ?? "GET"} ${url}`,
          );
        },
      ),
    );

    const app = renderApp("/admin/tools?page=4");

    expect(
      await screen.findByText(TOOL_B.model_name),
    ).toBeInTheDocument();
    expect(app.getLocation().search).toBe("?page=2");
    expect(requestedPages).toEqual([4, 2]);
  });

  it("单工具提交期间锁定全部开关，失败后回滚并显示真实错误", async () => {
    const patchGate = deferred<Response>();
    let patchCalls = 0;
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (
          url.pathname === "/api/v1/admin/tools" &&
          (!init?.method || init.method === "GET")
        ) {
          return jsonResponse(
            listResponse([TOOL_A, TOOL_B], 1, 20, 2),
          );
        }
        if (
          url.pathname === `/api/v1/admin/tools/${TOOL_A.tool_id}` &&
          init?.method === "PATCH"
        ) {
          patchCalls += 1;
          return patchGate.promise;
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/admin/tools");

    await screen.findByText(TOOL_A.model_name);
    const switchA = screen.getByRole("switch", {
      name: `${TOOL_A.model_name} 需要审批`,
    });
    const switchB = screen.getByRole("switch", {
      name: `${TOOL_B.model_name} 需要审批`,
    });
    await user.click(switchA);

    await waitFor(() => {
      expect(switchA).toHaveAttribute("aria-checked", "true");
      expect(switchA).toBeDisabled();
      expect(switchB).toBeDisabled();
    });
    await user.click(switchB);
    expect(patchCalls).toBe(1);
    expect(
      screen.getByText(/正在逐项保存工具审批策略/),
    ).toBeInTheDocument();

    patchGate.resolve(
      jsonResponse({ detail: "mcp_tool_policy_invalid" }, 422),
    );

    expect(
      await screen.findByText(/审批策略保存失败，界面已恢复服务端原状态/),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(switchA).toHaveAttribute("aria-checked", "false");
      expect(switchA).toBeEnabled();
      expect(switchB).toBeEnabled();
    });
    expect(patchCalls).toBe(1);
    const patchCall = fetchMock.mock.calls.find(
      ([input, init]) =>
        requestUrl(input).pathname ===
          `/api/v1/admin/tools/${TOOL_A.tool_id}` &&
        init?.method === "PATCH",
    );
    expect(JSON.parse(String(patchCall?.[1]?.body))).toEqual({
      requires_approval: true,
    });
  });

  it("无后端工具时展示真实空态", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = requestUrl(input);
          if (url.pathname.endsWith("/auth/me")) {
            return jsonResponse(TEST_ADMIN);
          }
          if (
            url.pathname === "/api/v1/admin/tools" &&
            (!init?.method || init.method === "GET")
          ) {
            return jsonResponse(listResponse([], 1, 20, 0));
          }
          throw new Error(
            `Unexpected request: ${init?.method ?? "GET"} ${url}`,
          );
        },
      ),
    );

    renderApp("/admin/tools");

    expect(
      await screen.findByRole("heading", {
        name: "还没有已注册工具",
      }),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/后端返回 0 个工具/),
    ).not.toBeInTheDocument();
    expect(
      within(screen.getByRole("main")).queryByRole("switch"),
    ).not.toBeInTheDocument();
  });

  it("工具 PATCH 遇到 403 时不回写乐观缓存并进入无权限状态", async () => {
    let authCalls = 0;
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          authCalls += 1;
          return jsonResponse(
            authCalls === 1 ? TEST_ADMIN : TEST_USER,
          );
        }
        if (
          url.pathname === "/api/v1/admin/tools" &&
          (!init?.method || init.method === "GET")
        ) {
          return jsonResponse(
            listResponse([TOOL_A], 1, 20, 1),
          );
        }
        if (
          url.pathname === `/api/v1/admin/tools/${TOOL_A.tool_id}` &&
          init?.method === "PATCH"
        ) {
          return jsonResponse({ detail: "admin_required" }, 403);
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    const app = renderApp("/admin/tools");

    await screen.findByText(TOOL_A.model_name);
    app.client.setQueryData(projectKeys.directory(), [PROJECT_A]);
    await user.click(
      screen.getByRole("switch", {
        name: `${TOOL_A.model_name} 需要审批`,
      }),
    );

    expect(
      await screen.findByRole("heading", {
        name: "当前账号没有管理员权限",
      }),
    ).toBeInTheDocument();
    expect(authCalls).toBeGreaterThanOrEqual(2);
    expect(
      app.client.getQueryCache().findAll({
        queryKey: adminKeys.all,
      }),
    ).toHaveLength(0);
    expect(
      app.client.getQueryData(projectKeys.directory()),
    ).toEqual([PROJECT_A]);
    expect(screen.queryByText(TOOL_A.model_name)).not.toBeInTheDocument();
  });

  it("兼容展示一期工具中文展示名 display_name", async () => {
    const namedTool = {
      ...TOOL_A,
      display_name: "文档检索工具",
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (
          url.pathname === "/api/v1/admin/tools" &&
          (!init?.method || init.method === "GET")
        ) {
          return jsonResponse(listResponse([namedTool], 1, 20, 1));
        }
        throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
      }),
    );

    renderApp("/admin/tools");

    expect(await screen.findByText("文档检索工具")).toBeInTheDocument();
    expect(screen.getByText(TOOL_A.model_name)).toBeInTheDocument();
  });

  it("支持编辑工具业务展示元信息，一键带入 Schema 参数与占位符", async () => {
    let tool = makeAdminTool({
      approval_summary_template: null,
      display_name: null,
      input_schema: {
        type: "object",
        properties: {
          payee: { type: "string" },
          amount: { type: "number" },
        },
      },
      model_name: "mcp__11111111111111111111111111111111",
      parameter_labels: {},
      remote_name: "pay_invoice",
      tool_id: "tool-pay",
    });
    let patchedBody: Record<string, unknown> | null = null;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (url.pathname === "/api/v1/admin/tools" && method === "GET") {
          return jsonResponse(listResponse([tool], 1, 20, 1));
        }
        if (
          url.pathname === `/api/v1/admin/tools/${tool.tool_id}` &&
          method === "PATCH"
        ) {
          patchedBody = JSON.parse(String(init?.body ?? "{}")) as Record<
            string,
            unknown
          >;
          tool = {
            ...tool,
            display_name: (patchedBody.display_name as string | null) ?? null,
            approval_summary_template:
              (patchedBody.approval_summary_template as string | null) ?? null,
            parameter_labels:
              (patchedBody.parameter_labels as Record<string, string>) ?? {},
          };
          return jsonResponse(tool);
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );
    const user = userEvent.setup();

    renderApp("/admin/tools");

    await user.click(
      await screen.findByRole("button", {
        name: `编辑业务展示：${tool.model_name}`,
      }),
    );
    const dialog = screen.getByRole("dialog", { name: "编辑工具业务展示" });

    await user.type(
      within(dialog).getByLabelText("中文展示名（可选）"),
      "发起付款",
    );
    await user.click(within(dialog).getByRole("button", { name: "{payee}" }));
    await user.click(within(dialog).getByRole("button", { name: "{amount}" }));
    expect(
      within(dialog).getByLabelText("审批摘要模板（可选）"),
    ).toHaveValue("{payee} {amount}");

    await user.click(
      within(dialog).getByRole("button", { name: "一键带入 Schema 参数" }),
    );
    expect(within(dialog).getByLabelText("参数键名 1")).toHaveValue("payee");
    expect(within(dialog).getByLabelText("参数键名 2")).toHaveValue("amount");
    await user.type(within(dialog).getByLabelText("中文标签 1"), "收款人");
    await user.type(within(dialog).getByLabelText("中文标签 2"), "付款金额");

    await user.click(
      within(dialog).getByRole("button", { name: "保存业务展示" }),
    );
    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", { name: "编辑工具业务展示" }),
      ).not.toBeInTheDocument();
    });

    expect(patchedBody).toEqual({
      display_name: "发起付款",
      approval_summary_template: "{payee} {amount}",
      parameter_labels: {
        payee: "收款人",
        amount: "付款金额",
      },
    });
    expect(await screen.findByText("发起付款")).toBeInTheDocument();
    expect(
      screen.getByText("payee: 收款人、amount: 付款金额"),
    ).toBeInTheDocument();
  });

  it("支持全选当前页与批量开启审批，提交期间锁定控件并刷新目录", async () => {
    const batchGate = deferred<Response>();
    let toolsList = [TOOL_A, TOOL_B];
    let batchBody: Record<string, unknown> | null = null;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (url.pathname === "/api/v1/admin/tools" && method === "GET") {
          return jsonResponse(listResponse(toolsList, 1, 20, 2));
        }
        if (
          url.pathname === "/api/v1/admin/tools/batch-approval" &&
          method === "PATCH"
        ) {
          batchBody = JSON.parse(String(init?.body ?? "{}")) as Record<
            string,
            unknown
          >;
          const response = await batchGate.promise;
          return response;
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );
    const user = userEvent.setup();

    renderApp("/admin/tools");

    await screen.findByText(TOOL_A.model_name);
    const selectAll = screen.getByRole("checkbox", { name: "全选当前页工具" });
    await user.click(selectAll);
    expect(screen.getByText("已选 2 项")).toBeInTheDocument();

    const batchEnableBtn = screen.getByRole("button", { name: "批量开启审批" });
    expect(batchEnableBtn).toBeEnabled();
    await user.click(batchEnableBtn);

    expect(
      await screen.findByText(/正在批量保存工具审批策略/),
    ).toBeInTheDocument();
    expect(batchEnableBtn).toBeDisabled();
    expect(
      screen.getByRole("switch", { name: `${TOOL_A.model_name} 需要审批` }),
    ).toBeDisabled();

    const updatedItems = [
      { ...TOOL_A, requires_approval: true },
      { ...TOOL_B, requires_approval: true },
    ];
    toolsList = updatedItems;
    batchGate.resolve(
      jsonResponse({
        items: updatedItems,
        updated_count: 2,
      }),
    );

    await waitFor(() => {
      expect(screen.getByText("已选 0 项")).toBeInTheDocument();
      expect(
        screen.getByRole("switch", { name: `${TOOL_A.model_name} 需要审批` }),
      ).toHaveAttribute("aria-checked", "true");
    });
    expect(batchBody).toEqual({
      requires_approval: true,
      tool_ids: [TOOL_A.tool_id, TOOL_B.tool_id],
    });
  });
});
