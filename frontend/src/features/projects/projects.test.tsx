import {
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  getRequestUrl,
  listResponse,
  makeSession,
  PROJECT_A,
  PROJECT_B,
  TEST_USER,
} from "../../test/fixtures";
import { jsonResponse, renderApp } from "../../test/testUtils";

const PROJECT_SESSION = makeSession({
  id: "project-session",
  title: "项目真实会话",
});

function baseFetchMock() {
  return vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(getRequestUrl(input), "http://localhost");
      if (url.pathname.endsWith("/auth/me")) {
        return jsonResponse(TEST_USER);
      }
      if (
        url.pathname === "/api/v1/projects" &&
        (!init?.method || init.method === "GET")
      ) {
        return jsonResponse(
          listResponse([PROJECT_A, PROJECT_B], 1, 100, 2),
        );
      }
      if (
        url.pathname === `/api/v1/projects/${PROJECT_A.id}` &&
        (!init?.method || init.method === "GET")
      ) {
        return jsonResponse(PROJECT_A);
      }
      if (url.pathname === "/api/v1/sessions") {
        const pageSize = Number(url.searchParams.get("page_size"));
        if (pageSize === 5 || pageSize === 10) {
          return jsonResponse(
            listResponse(
              [PROJECT_SESSION],
              Number(url.searchParams.get("page") ?? 1),
              pageSize,
              1,
            ),
          );
        }
        if (pageSize === 1) {
          return jsonResponse(listResponse([], 1, 1, 0));
        }
        return jsonResponse(listResponse([]));
      }
      if (
        url.pathname ===
        `/api/v1/projects/${PROJECT_A.id}/knowledge-bases`
      ) {
        return jsonResponse([]);
      }
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
    },
  );
}

describe("项目中心", () => {
  it("主内容提供移动端可访问的真实项目列表", async () => {
    vi.stubGlobal("fetch", baseFetchMock());

    renderApp("/projects");

    await screen.findByRole("heading", { level: 1, name: "项目" });
    const main = screen.getByRole("main");
    const projectLink = await within(main).findByRole("link", {
      name: new RegExp(PROJECT_A.name),
    });
    expect(projectLink).toHaveAttribute(
      "href",
      `/projects/${PROJECT_A.id}/overview`,
    );
    expect(within(main).getAllByText(/创建于/).length).toBeGreaterThan(0);
    expect(within(main).getAllByText(/更新于/).length).toBeGreaterThan(0);
  });

  it("四个 Tab 使用独立 URL、active 状态并在站内切换", async () => {
    vi.stubGlobal("fetch", baseFetchMock());
    const user = userEvent.setup();

    renderApp(`/projects/${PROJECT_A.id}/overview`);

    const tabs = await screen.findByRole("navigation", {
      name: "项目页面",
    });
    expect(
      within(tabs).getByRole("link", { name: "概览" }),
    ).toHaveAttribute("href", `/projects/${PROJECT_A.id}/overview`);
    expect(
      within(tabs).getByRole("link", { name: "会话" }),
    ).toHaveAttribute("href", `/projects/${PROJECT_A.id}/sessions`);
    expect(
      within(tabs).getByRole("link", { name: "知识库" }),
    ).toHaveAttribute("href", `/projects/${PROJECT_A.id}/knowledge`);
    expect(
      within(tabs).getByRole("link", { name: "设置" }),
    ).toHaveAttribute("href", `/projects/${PROJECT_A.id}/settings`);
    expect(
      within(tabs).getByRole("link", { name: "概览" }),
    ).toHaveAttribute("aria-current", "page");
    expect(
      within(
        screen.getByRole("navigation", { name: "主导航" }),
      ).getByRole("link", { name: PROJECT_A.name }),
    ).toHaveAttribute("aria-current", "page");

    await user.click(within(tabs).getByRole("link", { name: "会话" }));
    expect(await screen.findByText("项目真实会话")).toBeInTheDocument();
    expect(
      within(tabs).getByRole("link", { name: "会话" }),
    ).toHaveAttribute("aria-current", "page");

    await user.click(within(tabs).getByRole("link", { name: "知识库" }));
    expect(
      await screen.findByRole("heading", {
        name: "还没有关联知识库",
      }),
    ).toBeInTheDocument();
    expect(
      within(tabs).getByRole("link", { name: "知识库" }),
    ).toHaveAttribute("aria-current", "page");

    await user.click(within(tabs).getByRole("link", { name: "设置" }));
    expect(
      await screen.findByText("项目名称", { selector: "strong" }),
    ).toBeInTheDocument();
    expect(
      within(tabs).getByRole("link", { name: "设置" }),
    ).toHaveAttribute("aria-current", "page");
  });

  it("未知 Tab 回到 overview", async () => {
    vi.stubGlobal("fetch", baseFetchMock());

    renderApp(`/projects/${PROJECT_A.id}/not-a-tab`);

    const tabs = await screen.findByRole("navigation", {
      name: "项目页面",
    });
    await waitFor(() => {
      expect(
        within(tabs).getByRole("link", { name: "概览" }),
      ).toHaveAttribute("aria-current", "page");
    });
  });

  it("项目 404 显示明确不可访问状态", async () => {
    const fetchMock = baseFetchMock();
    fetchMock.mockImplementation(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/projects") {
          return jsonResponse(listResponse([PROJECT_A], 1, 100, 1));
        }
        if (url.pathname === "/api/v1/projects/missing") {
          return jsonResponse({ detail: "project_not_found" }, 404);
        }
        if (url.pathname === "/api/v1/sessions") {
          return jsonResponse(listResponse([]));
        }
        throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    renderApp("/projects/missing/overview");

    expect(
      await screen.findByRole("heading", {
        name: "项目不存在或不可访问",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "返回项目中心" }),
    ).toHaveAttribute("href", "/projects");
  });
});

describe("新建项目", () => {
  function installCreateFetch(postResponse: () => Response) {
    const createdProjects: typeof PROJECT_A[] = [];
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/projects" && init?.method === "POST") {
          const body = JSON.parse(String(init.body)) as { name: string };
          const response = postResponse();
          if (response.status === 201) {
            createdProjects.push({
              ...PROJECT_A,
              id: "created-project",
              name: body.name,
            });
          }
          return response;
        }
        if (url.pathname === "/api/v1/projects") {
          return jsonResponse(
            listResponse(
              createdProjects,
              1,
              100,
              createdProjects.length,
            ),
          );
        }
        if (url.pathname === "/api/v1/projects/created-project") {
          return jsonResponse({
            ...PROJECT_A,
            id: "created-project",
            name: createdProjects[0]?.name ?? "创建项目",
          });
        }
        if (url.pathname === "/api/v1/sessions") {
          return jsonResponse(listResponse([]));
        }
        throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  it("空名称和 121 个 Unicode 码点被拦截，120 个码点成功并跳转", async () => {
    const fetchMock = installCreateFetch(() =>
      jsonResponse(
        {
          ...PROJECT_A,
          id: "created-project",
          name: "😀".repeat(120),
        },
        201,
      ),
    );
    const user = userEvent.setup();

    renderApp("/projects");
    await screen.findByRole("heading", { level: 1, name: "项目" });
    const main = screen.getByRole("main");
    const openButtons = await within(main).findAllByRole("button", {
      name: "新建项目",
    });
    await user.click(openButtons[0]);

    const dialog = screen.getByRole("dialog", { name: "新建项目" });
    await user.click(
      within(dialog).getByRole("button", { name: "创建项目" }),
    );
    expect(
      within(dialog).getByText("请输入项目名称。"),
    ).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(
        ([input, init]) =>
          new URL(getRequestUrl(input), "http://localhost").pathname ===
            "/api/v1/projects" && init?.method === "POST",
      ),
    ).toBe(false);

    const input = within(dialog).getByLabelText("项目名称");
    fireEvent.change(input, { target: { value: "😀".repeat(121) } });
    await user.click(
      within(dialog).getByRole("button", { name: "创建项目" }),
    );
    expect(
      within(dialog).getByText(
        "项目名称需为 1–120 个字符，且不能包含控制字符。",
      ),
    ).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(
        ([inputElement, init]) =>
          new URL(getRequestUrl(inputElement), "http://localhost").pathname ===
            "/api/v1/projects" && init?.method === "POST",
      ),
    ).toBe(false);

    fireEvent.change(input, { target: { value: "😀".repeat(120) } });
    await user.click(
      within(dialog).getByRole("button", { name: "创建项目" }),
    );

    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "😀".repeat(120),
      }),
    ).toBeInTheDocument();
    const postCall = fetchMock.mock.calls.find(
      ([request, init]) =>
        new URL(getRequestUrl(request), "http://localhost").pathname ===
          "/api/v1/projects" && init?.method === "POST",
    );
    expect(JSON.parse(String(postCall?.[1]?.body))).toEqual({
      name: "😀".repeat(120),
    });
  });

  it("服务端失败保留弹窗并在弹窗内显示冲突", async () => {
    installCreateFetch(() =>
      jsonResponse({ detail: "project_name_invalid" }, 422),
    );
    const user = userEvent.setup();

    renderApp("/projects");
    await screen.findByRole("heading", { level: 1, name: "项目" });
    const main = screen.getByRole("main");
    const openButtons = await within(main).findAllByRole("button", {
      name: "新建项目",
    });
    await user.click(openButtons[0]);
    const dialog = screen.getByRole("dialog", { name: "新建项目" });
    await user.type(within(dialog).getByLabelText("项目名称"), "有效名称");
    await user.click(
      within(dialog).getByRole("button", { name: "创建项目" }),
    );

    expect(
      await within(dialog).findByText(
        "项目名称需为 1–120 个字符，且不能包含控制字符。",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("dialog", { name: "新建项目" }),
    ).toBeInTheDocument();
  });
});

describe("项目设置与删除边界", () => {
  function installSettingsFetch(sessionsTotal: number, deleteResponse: () => Response) {
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/projects") {
          return jsonResponse(listResponse([PROJECT_A], 1, 100, 1));
        }
        if (url.pathname === `/api/v1/projects/${PROJECT_A.id}`) {
          if (init?.method === "DELETE") {
            return deleteResponse();
          }
          return jsonResponse(PROJECT_A);
        }
        if (url.pathname === "/api/v1/sessions") {
          return jsonResponse(
            listResponse([], 1, 1, sessionsTotal),
          );
        }
        throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  it("sessions total 大于 0 时禁用删除并说明原因", async () => {
    installSettingsFetch(2, () => new Response(null, { status: 204 }));

    renderApp(`/projects/${PROJECT_A.id}/settings`);

    const deleteButton = await screen.findByRole("button", {
      name: "删除项目",
    });
    await waitFor(() => {
      expect(deleteButton).toBeDisabled();
    });
    expect(
      screen.getByText(/仍有 2 个会话/),
    ).toBeInTheDocument();
  });

  it("前端视为可删时仍展示后端 project_has_sessions 冲突", async () => {
    installSettingsFetch(0, () =>
      jsonResponse({ detail: "project_has_sessions" }, 409),
    );
    const user = userEvent.setup();

    renderApp(`/projects/${PROJECT_A.id}/settings`);

    const deleteButton = await screen.findByRole("button", {
      name: "删除项目",
    });
    await waitFor(() => {
      expect(deleteButton).toBeEnabled();
    });
    await user.click(deleteButton);
    const dialog = screen.getByRole("dialog", { name: "删除项目" });
    await user.click(
      within(dialog).getByLabelText("我已确认要删除这个项目"),
    );
    await user.click(
      within(dialog).getByRole("button", { name: "删除项目" }),
    );

    expect(
      await within(dialog).findByText(
        "该项目仍有会话，请先删除全部会话后再删除项目。",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("dialog", { name: "删除项目" }),
    ).toBeInTheDocument();
  });

  it("项目改名成功后更新页面并刷新缓存", async () => {
    let currentProject = PROJECT_A;
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/projects") {
          return jsonResponse(
            listResponse([currentProject], 1, 100, 1),
          );
        }
        if (url.pathname === `/api/v1/projects/${PROJECT_A.id}`) {
          if (init?.method === "PATCH") {
            const body = JSON.parse(String(init.body)) as { name: string };
            currentProject = {
              ...currentProject,
              name: body.name,
              updated_at: "2026-11-10T05:00:00Z",
            };
            return jsonResponse(currentProject);
          }
          return jsonResponse(currentProject);
        }
        if (url.pathname === "/api/v1/sessions") {
          return jsonResponse(listResponse([], 1, 1, 0));
        }
        throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp(`/projects/${PROJECT_A.id}/settings`);

    await screen.findByRole("heading", { name: PROJECT_A.name });
    await user.click(
      screen.getByRole("button", { name: "重命名" }),
    );
    const dialog = screen.getByRole("dialog", { name: "重命名项目" });
    const input = within(dialog).getByLabelText("项目名称");
    await user.clear(input);
    await user.type(input, "更新后的项目");
    await user.click(
      within(dialog).getByRole("button", { name: "保存名称" }),
    );

    expect(
      await screen.findByRole("heading", { name: "更新后的项目" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("dialog", { name: "重命名项目" }),
    ).not.toBeInTheDocument();
    expect(
      fetchMock.mock.calls.filter(
        ([inputElement, init]) =>
          new URL(getRequestUrl(inputElement), "http://localhost").pathname ===
            `/api/v1/projects/${PROJECT_A.id}` &&
          (!init?.method || init.method === "GET"),
      ).length,
    ).toBeGreaterThan(1);
  });

  it("项目删除成功后返回项目中心空态", async () => {
    let deleted = false;
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/projects") {
          return jsonResponse(
            listResponse(deleted ? [] : [PROJECT_A], 1, 100, deleted ? 0 : 1),
          );
        }
        if (url.pathname === `/api/v1/projects/${PROJECT_A.id}`) {
          if (init?.method === "DELETE") {
            deleted = true;
            return new Response(null, { status: 204 });
          }
          return jsonResponse(PROJECT_A);
        }
        if (url.pathname === "/api/v1/sessions") {
          return jsonResponse(listResponse([], 1, 1, 0));
        }
        throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp(`/projects/${PROJECT_A.id}/settings`);

    const deleteButton = await screen.findByRole("button", {
      name: "删除项目",
    });
    await waitFor(() => {
      expect(deleteButton).toBeEnabled();
    });
    await user.click(deleteButton);
    const dialog = screen.getByRole("dialog", { name: "删除项目" });
    await user.click(
      within(dialog).getByLabelText("我已确认要删除这个项目"),
    );
    await user.click(
      within(dialog).getByRole("button", { name: "删除项目" }),
    );

    expect(
      await screen.findByRole("heading", { level: 1, name: "项目" }),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { name: "还没有项目" }),
    ).toBeInTheDocument();
  });
});

