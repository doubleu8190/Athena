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
} from "../../test/fixtures";
import {
  makeMcpServer,
  TEST_ADMIN,
} from "../../test/adminFixtures";
import { jsonResponse, renderApp } from "../../test/testUtils";

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

const SERVER = makeMcpServer({
  id: "mcp-1",
  name: "内部检索服务",
});

describe("管理 MCP 页", () => {
  it("展示真实分页、配置、秘密状态和发现时间，并提供启停、重发现与删除入口", async () => {
    const second = makeMcpServer({
      config: {
        args: ["--stdio"],
        command: "/usr/local/bin/mcp",
        cwd: "/srv/mcp",
      },
      config_version: 2,
      id: "mcp-2",
      name: "本地工具桥",
      secrets: [],
      tools_discovered_at: null,
      transport: "stdio",
    });
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (
          url.pathname === "/api/v1/admin/mcp/servers" &&
          (!init?.method || init.method === "GET")
        ) {
          const page = Number(url.searchParams.get("page"));
          return jsonResponse(
            listResponse(
              page === 1 ? [SERVER] : [second],
              page,
              10,
              11,
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

    renderApp("/admin/mcp");

    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "MCP Server",
      }),
    ).toBeInTheDocument();
    expect(await screen.findByText("后端返回 11 个 Server")).toBeInTheDocument();
    expect(screen.getByText(SERVER.name)).toBeInTheDocument();
    expect(screen.getByText("HTTPS")).toBeInTheDocument();
    expect(screen.getByText("v1")).toBeInTheDocument();
    expect(screen.getByText("https://mcp.example/mcp")).toBeInTheDocument();
    expect(
      screen.getByText("Authorization（已保存值）"),
    ).toBeInTheDocument();
    expect(screen.getByText("admin-1")).toBeInTheDocument();
    expect(
      screen.getByText("第 1 页 · 共 11 项"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: `重新发现工具：${SERVER.name}` }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: `停用 MCP Server：${SERVER.name}` }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: `删除 MCP Server：${SERVER.name}` }),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/后端未提供“重新发现”接口/),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "下一页" }));
    expect(
      await screen.findByText("本地工具桥"),
    ).toBeInTheDocument();
    expect(screen.getByText("STDIO")).toBeInTheDocument();
    expect(
      screen.getByText("/usr/local/bin/mcp --stdio · cwd=/srv/mcp"),
    ).toBeInTheDocument();
    expect(screen.getByText("无已保存秘密")).toBeInTheDocument();
    expect(
      screen.getByText("后端尚无成功发现时间"),
    ).toBeInTheDocument();
  });

  it("页码超过后端 total 计算出的最后一页时回到真实末页", async () => {
    const pageTwoServer = makeMcpServer({
      id: "mcp-page-2",
      name: "第二页服务",
    });
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
            url.pathname === "/api/v1/admin/mcp/servers" &&
            (!init?.method || init.method === "GET")
          ) {
            const page = Number(url.searchParams.get("page"));
            requestedPages.push(page);
            return jsonResponse(
              listResponse(
                page === 2
                  ? [pageTwoServer]
                  : [
                      makeMcpServer({
                        id: "stale-page",
                        name: "越界页旧数据",
                      }),
                    ],
                page,
                10,
                15,
              ),
            );
          }
          throw new Error(
            `Unexpected request: ${init?.method ?? "GET"} ${url}`,
          );
        },
      ),
    );

    const app = renderApp("/admin/mcp?page=3");

    expect(
      await screen.findByText(pageTwoServer.name),
    ).toBeInTheDocument();
    expect(app.getLocation().search).toBe("?page=2");
    expect(requestedPages).toEqual([3, 2]);
  });

  it("创建期间冻结全部输入并展示等待连接状态，提交真实完整配置", async () => {
    const createGate = deferred<Response>();
    let created = false;
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (
          url.pathname === "/api/v1/admin/mcp/servers" &&
          (!init?.method || init.method === "GET")
        ) {
          return jsonResponse(
            listResponse(
              created ? [SERVER] : [],
              1,
              10,
              created ? 1 : 0,
            ),
          );
        }
        if (
          url.pathname === "/api/v1/admin/mcp/servers" &&
          init?.method === "POST"
        ) {
          const response = await createGate.promise;
          created = true;
          return response;
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/admin/mcp");

    await screen.findByRole("heading", {
      name: "还没有 MCP Server",
    });
    await user.click(
      screen.getByRole("button", { name: "新建 MCP Server" }),
    );
    const dialog = screen.getByRole("dialog", {
      name: "新建 MCP Server",
    });
    await user.type(
      within(dialog).getByLabelText("Server 名称"),
      SERVER.name,
    );
    await user.type(
      within(dialog).getByLabelText(/^HTTPS MCP URL/),
      "http://mcp.example/mcp",
    );
    await user.click(
      within(dialog).getByRole("button", {
        name: "添加秘密",
      }),
    );
    await user.type(
      within(dialog).getByLabelText("秘密名称 1"),
      "Authorization",
    );
    await user.type(
      within(dialog).getByLabelText("秘密值"),
      "Bearer plain-input",
    );
    await user.click(
      within(dialog).getByRole("button", {
        name: "创建并发现工具",
      }),
    );
    expect(
      within(dialog).getByText("地址必须使用 HTTPS。"),
    ).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(
        ([input, init]) =>
          requestUrl(input).pathname === "/api/v1/admin/mcp/servers" &&
          init?.method === "POST",
      ),
    ).toBe(false);

    const urlInput = within(dialog).getByLabelText(/^HTTPS MCP URL/);
    await user.clear(urlInput);
    await user.type(urlInput, "https://mcp.example/mcp");
    await user.click(
      within(dialog).getByRole("button", {
        name: "创建并发现工具",
      }),
    );

    expect(
      await within(dialog).findByText(
        /正在连接 MCP Server 并发现工具/,
      ),
    ).toBeInTheDocument();
    expect(
      within(dialog).queryByText("连接成功"),
    ).not.toBeInTheDocument();
    expect(within(dialog).getByLabelText("Server 名称")).toBeDisabled();
    expect(within(dialog).getByLabelText(/^HTTPS MCP URL/)).toBeDisabled();
    expect(
      within(dialog).getByRole("radio", {
        name: "Streamable HTTP",
      }),
    ).toBeDisabled();
    expect(
      within(dialog).getByRole("button", { name: "关闭弹窗" }),
    ).toBeDisabled();

    createGate.resolve(jsonResponse(SERVER, 201));
    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", {
          name: "新建 MCP Server",
        }),
      ).not.toBeInTheDocument();
    });
    const postCall = fetchMock.mock.calls.find(
      ([input, init]) =>
        requestUrl(input).pathname === "/api/v1/admin/mcp/servers" &&
        init?.method === "POST",
    );
    expect(JSON.parse(String(postCall?.[1]?.body))).toEqual({
      name: SERVER.name,
      owner_department_id: null,
      secrets: { Authorization: "Bearer plain-input" },
      transport: "streamable_http",
      url: "https://mcp.example/mcp",
      visibility_grants: [],
      visibility_scope: "company",
    });
  });

  it("编辑支持保密局部更新（留空保留原值、增删改凭据与 rediscover），409 后刷新版本并重载", async () => {
    let detailCalls = 0;
    let patchCalls = 0;
    let updated = SERVER;
    const detailRefreshGate = deferred<Response>();
    const concurrentServer = makeMcpServer({
      config: { url: "https://mcp-concurrent.example/mcp" },
      config_version: 2,
      id: SERVER.id,
      name: "并发更新后的服务",
      secrets: [{ has_value: true, name: "X-Concurrent-Token" }],
      updated_at: "2026-11-10T04:00:00Z",
    });
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (
          url.pathname === "/api/v1/admin/mcp/servers" &&
          (!init?.method || init.method === "GET")
        ) {
          return jsonResponse(listResponse([updated], 1, 10, 1));
        }
        if (
          url.pathname === "/api/v1/admin/mcp/servers/mcp-1" &&
          (!init?.method || init.method === "GET")
        ) {
          detailCalls += 1;
          if (detailCalls === 1) {
            return jsonResponse(SERVER);
          }
          return detailRefreshGate.promise;
        }
        if (
          url.pathname === "/api/v1/admin/mcp/servers/mcp-1" &&
          init?.method === "PATCH"
        ) {
          patchCalls += 1;
          if (patchCalls === 1) {
            return jsonResponse(
              { detail: "mcp_config_version_conflict" },
              409,
            );
          }
          updated = {
            ...concurrentServer,
            config_version: 3,
            updated_at: "2026-11-10T04:10:00Z",
          };
          return jsonResponse(updated);
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/admin/mcp");

    await screen.findByText(SERVER.name);
    await user.click(
      screen.getByRole("button", {
        name: `编辑 MCP Server：${SERVER.name}`,
      }),
    );
    const dialog = screen.getByRole("dialog", {
      name: "编辑 MCP Server",
    });
    expect(
      await within(dialog).findByText(
        "秘密值（已加密保存，留空保留原值）",
      ),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByLabelText("秘密名称 1"),
    ).toHaveValue("Authorization");
    const secretValue = within(dialog).getByLabelText(
      "秘密值（已加密保存，留空保留原值）",
    );
    expect(secretValue).toHaveValue("");

    // 修改名称不填秘密值，并勾选保存后重新发现工具，应保留原秘密不纳入 upsert/remove
    const nameInput = within(dialog).getByLabelText("Server 名称");
    await user.clear(nameInput);
    await user.type(nameInput, "新名称服务");
    await user.click(
      within(dialog).getByRole("checkbox", {
        name: "保存后重新发现工具（rediscover）",
      }),
    );
    await user.click(
      within(dialog).getByRole("button", {
        name: "保存修改",
      }),
    );

    const submitButton = within(dialog).getByRole("button", {
      name: "保存修改",
    });
    expect(
      await within(dialog).findByText(
        "配置已被并发修改，正在读取最新详情；读取完成前不能提交。",
      ),
    ).toBeInTheDocument();
    expect(submitButton).toBeDisabled();
    await user.click(submitButton);
    expect(patchCalls).toBe(1);

    detailRefreshGate.resolve(jsonResponse(concurrentServer));
    expect(
      await within(dialog).findByText(
        /配置已被并发修改。已载入最新配置与版本号/,
      ),
    ).toBeInTheDocument();
    expect(submitButton).toBeEnabled();
    expect(within(dialog).getByLabelText("Server 名称")).toHaveValue(
      concurrentServer.name,
    );
    expect(within(dialog).getByLabelText("秘密名称 1")).toHaveValue(
      "X-Concurrent-Token",
    );
    expect(detailCalls).toBe(2);

    // 留空既有秘密 X-Concurrent-Token，新增 X-New-Secret 并开启 rediscover 提交
    await user.click(
      within(dialog).getByRole("button", { name: "添加秘密" }),
    );
    await user.type(
      within(dialog).getByLabelText("秘密名称 2"),
      "X-New-Secret",
    );
    await user.type(
      within(dialog).getByLabelText("秘密值"),
      "new-token-value",
    );
    await user.click(submitButton);

    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", {
          name: "编辑 MCP Server",
        }),
      ).not.toBeInTheDocument();
    });
    const patches = fetchMock.mock.calls.filter(
      ([input, init]) =>
        requestUrl(input).pathname ===
          "/api/v1/admin/mcp/servers/mcp-1" &&
        init?.method === "PATCH",
    );
    expect(patches).toHaveLength(2);
    expect(JSON.parse(String(patches[0][1]?.body))).toEqual({
      expected_config_version: 1,
      name: "新名称服务",
      rediscover: true,
    });
    expect(JSON.parse(String(patches[1][1]?.body))).toEqual({
      expected_config_version: 2,
      rediscover: true,
      secrets_upsert: { "X-New-Secret": "new-token-value" },
    });
  });

  it("409 后详情刷新失败时禁止用旧版本重提", async () => {
    let detailCalls = 0;
    let updateCalls = 0;
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (
          url.pathname === "/api/v1/admin/mcp/servers" &&
          (!init?.method || init.method === "GET")
        ) {
          return jsonResponse(listResponse([SERVER], 1, 10, 1));
        }
        if (
          url.pathname === "/api/v1/admin/mcp/servers/mcp-1" &&
          (!init?.method || init.method === "GET")
        ) {
          detailCalls += 1;
          if (detailCalls === 1) {
            return jsonResponse(SERVER);
          }
          if (detailCalls === 2) {
            return jsonResponse(
              { detail: "mcp_unavailable" },
              503,
            );
          }
          return jsonResponse({
            ...SERVER,
            config_version: 2,
            name: "重新打开后的服务",
            updated_at: "2026-11-10T04:00:00Z",
          });
        }
        if (
          url.pathname === "/api/v1/admin/mcp/servers/mcp-1" &&
          init?.method === "PATCH"
        ) {
          updateCalls += 1;
          return jsonResponse(
            { detail: "mcp_config_version_conflict" },
            409,
          );
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/admin/mcp");

    await screen.findByText(SERVER.name);
    await user.click(
      screen.getByRole("button", {
        name: `编辑 MCP Server：${SERVER.name}`,
      }),
    );
    let dialog = screen.getByRole("dialog", {
      name: "编辑 MCP Server",
    });
    const secretValue = await within(dialog).findByLabelText(
      "秘密值（已加密保存，留空保留原值）",
    );
    await user.type(secretValue, "replacement-secret");
    await user.click(
      within(dialog).getByRole("button", {
        name: "保存修改",
      }),
    );

    expect(
      await within(dialog).findByText(
        "配置已被并发修改，且暂时无法读取最新详情。请关闭后重新打开编辑。",
      ),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("button", {
        name: "保存修改",
      }),
    ).toBeDisabled();
    expect(secretValue).toBeDisabled();
    expect(updateCalls).toBe(1);

    await user.click(
      within(dialog).getByRole("button", { name: "取消" }),
    );
    expect(
      screen.queryByRole("dialog", {
        name: "编辑 MCP Server",
      }),
    ).not.toBeInTheDocument();

    await user.click(
      screen.getByRole("button", {
        name: `编辑 MCP Server：${SERVER.name}`,
      }),
    );
    dialog = screen.getByRole("dialog", {
      name: "编辑 MCP Server",
    });
    expect(
      await within(dialog).findByText(
        "本次提交的 expected_config_version：2",
      ),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("button", {
        name: "保存修改",
      }),
    ).toBeEnabled();
    expect(
      within(dialog).getByLabelText("秘密值（已加密保存，留空保留原值）"),
    ).toHaveValue("");
  });

  it("切换 STDIO 后校验绝对 command/cwd 并提交逐行参数", async () => {
    let posted: unknown = null;
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (
          url.pathname === "/api/v1/admin/mcp/servers" &&
          (!init?.method || init.method === "GET")
        ) {
          return jsonResponse(listResponse([], 1, 10, 0));
        }
        if (
          url.pathname === "/api/v1/admin/mcp/servers" &&
          init?.method === "POST"
        ) {
          posted = JSON.parse(String(init.body));
          return jsonResponse(
            makeMcpServer({
              config: {
                args: ["--stdio", "--trace"],
                command: "/usr/local/bin/mcp",
                cwd: "/srv/mcp",
              },
              id: "mcp-stdio",
              name: "本地工具桥",
              secrets: [],
              transport: "stdio",
            }),
            201,
          );
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/admin/mcp");

    await screen.findByRole("heading", {
      name: "还没有 MCP Server",
    });
    await user.click(
      screen.getByRole("button", { name: "新建 MCP Server" }),
    );
    const dialog = screen.getByRole("dialog", {
      name: "新建 MCP Server",
    });
    await user.type(
      within(dialog).getByLabelText("Server 名称"),
      "本地工具桥",
    );
    await user.click(
      within(dialog).getByRole("radio", { name: "STDIO" }),
    );
    expect(
      within(dialog).queryByLabelText("HTTPS MCP URL"),
    ).not.toBeInTheDocument();
    await user.type(
      within(dialog).getByLabelText("绝对命令路径"),
      "bin/mcp",
    );
    await user.type(
      within(dialog).getByLabelText(
        "工作目录（可选，必须是绝对路径）",
      ),
      "relative/path",
    );
    await user.click(
      within(dialog).getByRole("button", {
        name: "创建并发现工具",
      }),
    );
    expect(
      within(dialog).getByText(
        "stdio command 必须是绝对路径，且不能包含 NUL。",
      ),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByText(
        "工作目录必须是绝对路径，且不能包含 NUL。",
      ),
    ).toBeInTheDocument();

    const command = within(dialog).getByLabelText("绝对命令路径");
    await user.clear(command);
    await user.type(command, "/usr/local/bin/mcp");
    const cwd = within(dialog).getByLabelText(
      "工作目录（可选，必须是绝对路径）",
    );
    await user.clear(cwd);
    await user.type(cwd, "/srv/mcp");
    await user.type(
      within(dialog).getByLabelText(
        "参数（每行一个，不使用 shell 引号）",
      ),
      "--stdio\n--trace",
    );
    await user.click(
      within(dialog).getByRole("button", {
        name: "创建并发现工具",
      }),
    );
    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", {
          name: "新建 MCP Server",
        }),
      ).not.toBeInTheDocument();
    });
    expect(posted).toEqual({
      args: ["--stdio", "--trace"],
      command: "/usr/local/bin/mcp",
      cwd: "/srv/mcp",
      name: "本地工具桥",
      owner_department_id: null,
      secrets: {},
      transport: "stdio",
      visibility_grants: [],
      visibility_scope: "company",
    });
  });

  it("部门共享 Server 映射部门中文名，并支持启停、重发现与删除确认", async () => {
    let deptServer = makeMcpServer({
      id: "mcp-dept",
      name: "研发部代码网关",
      owner_department_id: "dept-rd",
      visibility_scope: "department",
      config_version: 2,
      enabled: true,
    });
    let deleted = false;
    const calls: Array<{ method: string; path: string; body?: unknown }> = [];

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (url.pathname === "/api/v1/admin/org/departments") {
          return jsonResponse({
            items: [
              {
                id: "dept-rd",
                name: "研发中心",
                parent_id: null,
                status: "active",
                path_ids: ["dept-rd"],
              },
              {
                id: "dept-fin",
                name: "财务部",
                parent_id: null,
                status: "active",
                path_ids: ["dept-fin"],
              },
            ],
          });
        }
        if (
          url.pathname === "/api/v1/admin/mcp/servers" &&
          method === "GET"
        ) {
          return jsonResponse(
            listResponse(deleted ? [] : [deptServer], 1, 10, deleted ? 0 : 1),
          );
        }
        if (
          url.pathname === `/api/v1/admin/mcp/servers/${deptServer.id}/enabled` &&
          method === "PUT"
        ) {
          const body = JSON.parse(String(init?.body ?? "{}")) as {
            enabled: boolean;
            expected_config_version: number;
          };
          calls.push({ method, path: url.pathname, body });
          deptServer = {
            ...deptServer,
            enabled: body.enabled,
            config_version: 3,
          };
          return jsonResponse(deptServer);
        }
        if (
          url.pathname ===
            `/api/v1/admin/mcp/servers/${deptServer.id}/rediscover` &&
          method === "POST"
        ) {
          calls.push({ method, path: url.pathname });
          deptServer = {
            ...deptServer,
            tools_discovered_at: "2026-11-20T05:00:00Z",
          };
          return jsonResponse(deptServer);
        }
        if (
          url.pathname === `/api/v1/admin/mcp/servers/${deptServer.id}` &&
          method === "DELETE"
        ) {
          calls.push({ method, path: url.pathname });
          deleted = true;
          return new Response(null, { status: 204 });
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );
    const user = userEvent.setup();

    renderApp("/admin/mcp");

    const card = (
      await screen.findByRole("heading", { name: "研发部代码网关" })
    ).closest("li") as HTMLElement;
    expect(await within(card).findByText("研发中心")).toBeInTheDocument();

    await user.click(
      within(card).getByRole("button", {
        name: "停用 MCP Server：研发部代码网关",
      }),
    );
    expect(await within(card).findByText("已停用")).toBeInTheDocument();
    expect(calls[0]).toEqual({
      method: "PUT",
      path: `/api/v1/admin/mcp/servers/${deptServer.id}/enabled`,
      body: { enabled: false, expected_config_version: 2 },
    });

    await user.click(
      within(card).getByRole("button", {
        name: "重新发现工具：研发部代码网关",
      }),
    );
    await waitFor(() => {
      expect(calls[1]).toEqual({
        method: "POST",
        path: `/api/v1/admin/mcp/servers/${deptServer.id}/rediscover`,
      });
    });

    await user.click(
      within(card).getByRole("button", {
        name: "删除 MCP Server：研发部代码网关",
      }),
    );
    const confirmDialog = await screen.findByRole("dialog", {
      name: "删除 MCP Server「研发部代码网关」",
    });
    await user.click(
      within(confirmDialog).getByRole("button", { name: "确认删除" }),
    );
    expect(await screen.findByText("还没有 MCP Server")).toBeInTheDocument();
  });

  it("创建 MCP Server 支持配置部门共享与附加跨部门授权", async () => {
    let createdBody: Record<string, unknown> | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (url.pathname === "/api/v1/admin/org/departments") {
          return jsonResponse({
            items: [
              {
                id: "dept-rd",
                name: "研发中心",
                parent_id: null,
                status: "active",
                path_ids: ["dept-rd"],
              },
              {
                id: "dept-fin",
                name: "财务部",
                parent_id: null,
                status: "active",
                path_ids: ["dept-fin"],
              },
            ],
          });
        }
        if (url.pathname === "/api/v1/admin/mcp/servers" && method === "GET") {
          return jsonResponse(listResponse([], 1, 10, 0));
        }
        if (url.pathname === "/api/v1/admin/mcp/servers" && method === "POST") {
          createdBody = JSON.parse(String(init?.body ?? "{}")) as Record<
            string,
            unknown
          >;
          return jsonResponse(
            makeMcpServer({
              id: "mcp-dept-created",
              name: "财务网关",
              owner_department_id: "dept-fin",
              visibility_scope: "department",
            }),
            201,
          );
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );
    const user = userEvent.setup();

    renderApp("/admin/mcp");

    await screen.findByRole("heading", { name: "还没有 MCP Server" });
    await user.click(screen.getByRole("button", { name: "新建 MCP Server" }));
    const dialog = screen.getByRole("dialog", { name: "新建 MCP Server" });

    await user.type(within(dialog).getByLabelText("Server 名称"), "财务网关");
    await user.type(
      within(dialog).getByLabelText(/^HTTPS MCP URL/),
      "https://fin.example/mcp",
    );

    await user.click(
      within(dialog).getByRole("combobox", { name: "可见范围" }),
    );
    await user.click(screen.getByRole("option", { name: "部门共享" }));

    const deptCombo = await within(dialog).findByRole("combobox", {
      name: "归属部门",
    });
    await user.click(deptCombo);
    await user.click(screen.getByRole("option", { name: "财务部" }));

    expect(
      await within(dialog).findByText("归属部门（默认）"),
    ).toBeInTheDocument();

    await user.click(
      within(dialog).getByRole("button", { name: "创建并发现工具" }),
    );
    await waitFor(() => {
      expect(createdBody).toEqual({
        name: "财务网关",
        transport: "streamable_http",
        url: "https://fin.example/mcp",
        secrets: {},
        visibility_scope: "department",
        owner_department_id: "dept-fin",
        visibility_grants: [
          {
            target_type: "department",
            target_id: "dept-fin",
            include_subdepartments: true,
          },
        ],
      });
    });
  });
});
