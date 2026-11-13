import {
  act,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { onlineManager } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { jsonResponse, renderApp } from "../../test/testUtils";
import type { User } from "./model";

const USER: User = {
  created_at: "2026-11-10T00:00:00Z",
  display_name: "林知远",
  email: "lin@example.com",
  id: "user-1",
  role: "user",
};

async function clickLogout(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "账户菜单" }));
  await user.click(screen.getByRole("menuitem", { name: "退出登录" }));
}

function getRequestUrl(input: RequestInfo | URL): string {
  if (typeof input === "string") {
    return input;
  }
  if (input instanceof URL) {
    return input.toString();
  }
  return input.url;
}

async function waitForStableLoginScreen(
  app: ReturnType<typeof renderApp>,
) {
  await waitFor(() => {
    expect(app.getLocation().pathname).toBe("/login");
    expect(
      screen.getByRole("heading", { name: "欢迎回来" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("工作邮箱")).toBeEnabled();
    expect(screen.getByLabelText("密码")).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "进入 Athena" }),
    ).toBeEnabled();
  });
}

async function waitForStableChatWorkspace(
  app: ReturnType<typeof renderApp>,
) {
  await waitFor(() => {
    expect(app.getLocation()).toEqual({
      pathname: "/chat",
      search: "",
    });
    expect(
      screen.getByRole("heading", { name: "对话工作台" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("对话草稿")).toBeInTheDocument();
    expect(
      screen.getByRole("navigation", { name: "主导航" }),
    ).toBeInTheDocument();
  });
}

async function waitForStableProjectsPage(
  app: ReturnType<typeof renderApp>,
) {
  await waitFor(() => {
    expect(app.getLocation()).toEqual({
      pathname: "/projects",
      search: "?view=all",
    });
    const main = screen.getByRole("main");
    expect(
      within(main).getByRole("heading", { level: 1, name: "项目" }),
    ).toBeInTheDocument();
    expect(
      within(main).getByRole("button", { name: "新建项目" }),
    ).toBeInTheDocument();
  });
}

async function waitForStableRegisterScreen(
  app: ReturnType<typeof renderApp>,
) {
  await waitFor(() => {
    expect(app.getLocation().pathname).toBe("/register");
    expect(
      screen.getByRole("heading", { name: "创建你的工作身份" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("工作邮箱")).toBeEnabled();
    expect(screen.getByLabelText("密码")).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "创建账号" }),
    ).toBeEnabled();
  });
}

async function waitForStableAdminModelsPage(
  app: ReturnType<typeof renderApp>,
) {
  await waitFor(() => {
    expect(app.getLocation()).toEqual({
      pathname: "/admin/models",
      search: "",
    });
    expect(
      screen.getByRole("heading", {
        level: 1,
        name: "模型与压缩策略",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("navigation", { name: "管理子导航" }),
    ).toBeInTheDocument();
  });
}

describe("认证流程", () => {
  it("登录成功后更新认证缓存并进入工作区", async () => {
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me")) {
          return jsonResponse({ detail: "not_authenticated" }, 401);
        }
        if (url.endsWith("/auth/login")) {
          return jsonResponse(USER);
        }
        throw new Error(`Unexpected request: ${init?.method} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    const app = renderApp("/login");

    await waitForStableLoginScreen(app);
    await user.type(screen.getByLabelText("工作邮箱"), "lin@example.com");
    await user.type(
      screen.getByLabelText("密码"),
      "correct-horse-battery",
    );
    await user.click(screen.getByLabelText("在此设备保持登录 30 天"));
    await user.click(screen.getByRole("button", { name: "进入 Athena" }));

    await waitForStableChatWorkspace(app);
    await user.click(screen.getByRole("button", { name: "账户菜单" }));
    expect(screen.getByText("林知远")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "管理" })).not.toBeInTheDocument();

    const loginCall = fetchMock.mock.calls.find(([input]) =>
      getRequestUrl(input).endsWith("/auth/login"),
    );
    expect(loginCall).toBeDefined();
    expect(loginCall?.[1]).toMatchObject({
      credentials: "same-origin",
      method: "POST",
    });
    expect(JSON.parse(String(loginCall?.[1]?.body))).toEqual({
      email: "lin@example.com",
      password: "correct-horse-battery",
      remember_me: true,
    });
  });

  it("把 invalid_credentials 映射为表单内中文错误", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me")) {
          return jsonResponse({ detail: "not_authenticated" }, 401);
        }
        if (url.endsWith("/auth/login")) {
          return jsonResponse({ detail: "invalid_credentials" }, 401);
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );
    const user = userEvent.setup();

    const app = renderApp("/login");

    await waitForStableLoginScreen(app);
    await user.type(screen.getByLabelText("工作邮箱"), "lin@example.com");
    await user.type(screen.getByLabelText("密码"), "wrong-password-123");
    await user.click(screen.getByRole("button", { name: "进入 Athena" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "邮箱或密码不正确。",
    );
    expect(screen.getByRole("button", { name: "进入 Athena" })).toBeEnabled();
  });

  it("登录可完整输入并提交 128 个非 BMP 密码码点", async () => {
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me")) {
          return jsonResponse({ detail: "not_authenticated" }, 401);
        }
        if (url.endsWith("/auth/login")) {
          return jsonResponse(USER);
        }
        throw new Error(`Unexpected request: ${init?.method} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    const password = "😀".repeat(128);

    const app = renderApp("/login");

    await waitForStableLoginScreen(app);
    await user.type(screen.getByLabelText("工作邮箱"), "lin@example.com");
    await user.type(screen.getByLabelText("密码"), password);
    expect(screen.getByLabelText("密码")).toHaveValue(password);
    await user.click(screen.getByRole("button", { name: "进入 Athena" }));

    await waitForStableChatWorkspace(app);
    const loginCall = fetchMock.mock.calls.find(([input]) =>
      getRequestUrl(input).endsWith("/auth/login"),
    );
    expect(JSON.parse(String(loginCall?.[1]?.body)).password).toBe(password);
  });

  it("未登录访问受保护路由会跳转登录并在成功后回到原路径", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me")) {
          return jsonResponse({ detail: "not_authenticated" }, 401);
        }
        if (url.endsWith("/auth/login")) {
          return jsonResponse(USER);
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );
    const user = userEvent.setup();

    const app = renderApp("/projects?view=all");

    await waitForStableLoginScreen(app);
    await user.type(screen.getByLabelText("工作邮箱"), "lin@example.com");
    await user.type(screen.getByLabelText("密码"), "correct-horse-battery");
    await user.click(screen.getByRole("button", { name: "进入 Athena" }));

    await waitForStableProjectsPage(app);
  });

  it("已登录访问登录页会跳转到对话工作台", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse(USER)),
    );

    const app = renderApp("/login");

    await waitForStableChatWorkspace(app);
    expect(
      screen.queryByRole("heading", { name: "欢迎回来" }),
    ).not.toBeInTheDocument();
  });

  it("注册时在表单内拦截不足 12 个字符的密码", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = getRequestUrl(input);
      if (url.endsWith("/auth/me")) {
        return jsonResponse({ detail: "not_authenticated" }, 401);
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    const app = renderApp("/register");

    await waitForStableRegisterScreen(app);
    await user.type(screen.getByLabelText("工作邮箱"), "new@example.com");
    await user.type(screen.getByLabelText("密码"), "short-pass1");
    await user.click(screen.getByRole("button", { name: "创建账号" }));

    expect(
      screen.getByText("密码需为 12–128 个字符。"),
    ).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([input]) =>
        getRequestUrl(input).endsWith("/auth/register"),
      ),
    ).toBe(false);
  });

  it("退出登录调用真实接口并回到登录页", async () => {
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me")) {
          return jsonResponse(USER);
        }
        if (
          url.endsWith("/auth/logout") &&
          init?.method === "POST"
        ) {
          return new Response(null, { status: 204 });
        }
        throw new Error(`Unexpected request: ${init?.method} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    const app = renderApp("/chat");

    await waitForStableChatWorkspace(app);
    await clickLogout(user);

    await waitForStableLoginScreen(app);
    expect(
      fetchMock.mock.calls.some(
        ([input, init]) =>
          getRequestUrl(input).endsWith("/auth/logout") &&
          init?.method === "POST" &&
          init.credentials === "same-origin",
      ),
    ).toBe(true);
  });

  it("账户菜单可用键盘导航、Escape 与点击外部关闭", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me")) return jsonResponse(USER);
        if (url.endsWith("/projects")) return jsonResponse({ items: [] });
        throw new Error(`Unexpected request: ${url}`);
      }),
    );
    const user = userEvent.setup();
    renderApp("/chat");

    const trigger = await screen.findByRole("button", { name: "账户菜单" });
    await user.click(trigger);
    const menu = screen.getByRole("menu");
    const profileItem = screen.getByRole("menuitem", { name: "个人页面" });
    expect(profileItem).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: "退出登录" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();

    await user.click(trigger);
    await user.click(screen.getByRole("heading", { name: "对话工作台" }));
    expect(menu).not.toBeInTheDocument();

    await user.click(trigger);
    await user.click(screen.getByRole("menuitem", { name: "个人页面" }));
    expect(await screen.findByRole("heading", { name: "个人资料" })).toBeInTheDocument();
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("Tab 和 Shift+Tab 离开账户控件时收起菜单并保留目标焦点", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me")) return jsonResponse(USER);
        if (url.includes("/api/v1/projects")) {
          return jsonResponse({ items: [], page: 1, page_size: 100, total: 0 });
        }
        if (url.includes("/api/v1/sessions")) {
          return jsonResponse({ items: [], page: 1, page_size: 8, total: 0 });
        }
        if (url.endsWith("/api/v1/skills")) {
          return jsonResponse({ items: [] });
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );
    const user = userEvent.setup();
    renderApp("/chat");
    const trigger = await screen.findByRole("button", { name: "账户菜单" });

    await user.click(trigger);
    await user.tab();
    expect(screen.getByRole("menuitem", { name: "退出登录" })).toHaveFocus();
    await user.tab();
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "新建对话" })).toHaveFocus();

    await user.click(trigger);
    await user.keyboard("{Shift>}{Tab}{/Shift}");
    expect(trigger).toHaveFocus();
    expect(screen.getByRole("menu")).toBeInTheDocument();
    await user.keyboard("{Shift>}{Tab}{/Shift}");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "知识库" })).toHaveFocus();
  });

  it("退出接口失败时留在工作区并显示错误", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me")) return jsonResponse(USER);
        if (url.endsWith("/auth/logout")) {
          return jsonResponse({ detail: "http_error" }, 500);
        }
        if (url.includes("/api/v1/projects")) {
          return jsonResponse({ items: [], page: 1, page_size: 100, total: 0 });
        }
        if (url.includes("/api/v1/sessions")) {
          return jsonResponse({ items: [], page: 1, page_size: 8, total: 0 });
        }
        if (url.endsWith("/api/v1/skills")) {
          return jsonResponse({ items: [] });
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );
    const user = userEvent.setup();
    const app = renderApp("/chat");

    await screen.findByRole("heading", { name: "对话工作台" });
    await clickLogout(user);
    expect(await screen.findByRole("alert")).toHaveTextContent("请求未完成");
    expect(app.getLocation().pathname).toBe("/chat");
  });

  it("显示名更新同步认证缓存，密码确认错误不提交且成功后清空表单", async () => {
    const calls: Array<{ path: string; body?: unknown }> = [];
    const updatedUser = { ...USER, display_name: "更新后的名字" };
    let authoritativeUser = USER;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = getRequestUrl(input);
      const path = new URL(url, "http://localhost").pathname;
      if (path.endsWith("/auth/me") && (!init?.method || init.method === "GET")) {
        return jsonResponse(authoritativeUser);
      }
      if (path.endsWith("/auth/me") && init?.method === "PATCH") {
        calls.push({ path, body: JSON.parse(String(init.body)) });
        authoritativeUser = updatedUser;
        return jsonResponse(updatedUser);
      }
      if (path.endsWith("/auth/password") && init?.method === "POST") {
        calls.push({ path, body: JSON.parse(String(init.body)) });
        return new Response(null, { status: 204 });
      }
      if (path === "/api/v1/projects") return jsonResponse({ items: [] });
      throw new Error(`Unexpected request: ${init?.method} ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    const app = renderApp("/profile");

    const displayName = await screen.findByLabelText(/显示名/);
    await user.clear(displayName);
    await user.type(displayName, "更新后的名字");
    await user.click(screen.getByRole("button", { name: "保存显示名" }));
    expect(await screen.findByRole("status")).toHaveTextContent("显示名已更新");
    await user.click(screen.getByRole("button", { name: "账户菜单" }));
    expect(screen.getByText("更新后的名字")).toBeInTheDocument();
    expect(app.client.getQueryData<User>(["auth", "current-user"])?.display_name).toBe("更新后的名字");

    await user.type(screen.getByLabelText("当前密码"), "current-password");
    await user.type(screen.getByLabelText("新密码"), "new-password-long-1");
    await user.type(screen.getByLabelText("确认新密码"), "new-password-long-2");
    await user.click(screen.getByRole("button", { name: "更新密码" }));
    expect(screen.getByText("两次输入的新密码不一致。", { selector: "[role=alert]" })).toBeInTheDocument();
    expect(calls.some((call) => call.path.endsWith("/auth/password"))).toBe(false);

    await user.clear(screen.getByLabelText("确认新密码"));
    await user.type(screen.getByLabelText("确认新密码"), "new-password-long-1");
    await user.click(screen.getByRole("button", { name: "更新密码" }));
    expect(screen.getByText("密码已更新，当前设备仍保持登录。")).toBeInTheDocument();
    expect(screen.getByLabelText("当前密码")).toHaveValue("");
    expect(screen.getByLabelText("新密码")).toHaveValue("");
    expect(screen.getByLabelText("确认新密码")).toHaveValue("");
    expect(calls.find((call) => call.path.endsWith("/auth/password"))?.body).toEqual({
      current_password: "current-password",
      new_password: "new-password-long-1",
    });
  });

  it("显示名写入后刷新遇到网络错误时保留工作区并允许重试", async () => {
    const updatedUser = { ...USER, display_name: "网络恢复后同步" };
    let authMeCalls = 0;
    let patchCalls = 0;
    let refreshUnavailable = true;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = new URL(getRequestUrl(input), "http://localhost")
          .pathname;
        if (path.endsWith("/auth/me") && init?.method === "PATCH") {
          patchCalls += 1;
          return jsonResponse(updatedUser);
        }
        if (path.endsWith("/auth/me")) {
          authMeCalls += 1;
          if (authMeCalls === 1) return jsonResponse(USER);
          if (refreshUnavailable) {
            throw new TypeError("network unavailable");
          }
          return jsonResponse(updatedUser);
        }
        if (path === "/api/v1/projects") {
          return jsonResponse({ items: [] });
        }
        throw new Error(`Unexpected request: ${init?.method} ${path}`);
      }),
    );
    const user = userEvent.setup();
    const app = renderApp("/profile");

    const displayName = await screen.findByLabelText(/显示名/);
    await user.clear(displayName);
    await user.type(displayName, "网络恢复后同步");
    await user.click(screen.getByRole("button", { name: "保存显示名" }));

    expect(
      await screen.findByText(
        "显示名已保存，但登录资料刷新失败。请重试刷新。",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "个人资料" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("暂时无法确认登录状态"),
    ).not.toBeInTheDocument();
    expect(
      app.client.getQueryData<User>(["auth", "current-user"]),
    ).toEqual(USER);

    refreshUnavailable = false;
    await user.click(screen.getByRole("button", { name: "重试刷新" }));

    expect(await screen.findByText("登录资料已刷新。")).toBeInTheDocument();
    expect(
      app.client.getQueryData<User>(["auth", "current-user"]),
    ).toEqual(updatedUser);
    expect(patchCalls).toBe(1);
  });

  it("离线暂停的刷新返回失败，重试仍离线失败且恢复联网后可成功", async () => {
    const updatedUser = {
      ...USER,
      display_name: "恢复联网后的资料",
    };
    let authMeCalls = 0;
    let patchCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = new URL(getRequestUrl(input), "http://localhost")
          .pathname;
        if (path.endsWith("/auth/me") && init?.method === "PATCH") {
          patchCalls += 1;
          // 写入已成功后进入离线，随后的身份刷新必须进入 paused 而非误判成功。
          onlineManager.setOnline(false);
          return jsonResponse(updatedUser);
        }
        if (path.endsWith("/auth/me")) {
          authMeCalls += 1;
          return jsonResponse(authMeCalls === 1 ? USER : updatedUser);
        }
        if (path === "/api/v1/projects") {
          return jsonResponse({ items: [] });
        }
        throw new Error(`Unexpected request: ${init?.method} ${path}`);
      }),
    );
    const user = userEvent.setup();
    const app = renderApp("/profile");

    try {
      const displayName = await screen.findByLabelText(/显示名/);
      await user.clear(displayName);
      await user.type(displayName, "恢复联网后的资料");
      await user.click(screen.getByRole("button", { name: "保存显示名" }));

      expect(
        await screen.findByText(
          "显示名已保存，但登录资料刷新失败。请重试刷新。",
        ),
      ).toBeInTheDocument();
      expect(
        app.client.getQueryState(["auth", "current-user"])?.fetchStatus,
      ).toBe("paused");
      expect(authMeCalls).toBe(1);
      expect(patchCalls).toBe(1);

      await user.click(screen.getByRole("button", { name: "重试刷新" }));
      expect(
        await screen.findByText(
          "登录资料刷新仍然失败。显示名已保存，请稍后重试。",
        ),
      ).toBeInTheDocument();
      expect(
        app.client.getQueryState(["auth", "current-user"])?.fetchStatus,
      ).toBe("paused");
      expect(authMeCalls).toBe(1);
      expect(patchCalls).toBe(1);

      await act(async () => {
        onlineManager.setOnline(true);
      });
      await user.click(screen.getByRole("button", { name: "重试刷新" }));

      expect(await screen.findByText("登录资料已刷新。")).toBeInTheDocument();
      expect(
        app.client.getQueryData<User>(["auth", "current-user"]),
      ).toEqual(updatedUser);
      expect(patchCalls).toBe(1);
    } finally {
      await act(async () => {
        onlineManager.setOnline(true);
      });
    }
  });

  it("恢复联网后的身份刷新失败仍保留可重试状态", async () => {
    let authMeCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = new URL(getRequestUrl(input), "http://localhost")
          .pathname;
        if (path.endsWith("/auth/me") && init?.method === "PATCH") {
          onlineManager.setOnline(false);
          return jsonResponse({ ...USER, display_name: "已保存" });
        }
        if (path.endsWith("/auth/me")) {
          authMeCalls += 1;
          return authMeCalls === 1
            ? jsonResponse(USER)
            : jsonResponse({ detail: "http_error" }, 500);
        }
        if (path === "/api/v1/projects") {
          return jsonResponse({ items: [] });
        }
        throw new Error(`Unexpected request: ${init?.method} ${path}`);
      }),
    );
    const user = userEvent.setup();
    const app = renderApp("/profile");

    try {
      const displayName = await screen.findByLabelText(/显示名/);
      await user.clear(displayName);
      await user.type(displayName, "已保存");
      await user.click(screen.getByRole("button", { name: "保存显示名" }));
      await screen.findByText(
        "显示名已保存，但登录资料刷新失败。请重试刷新。",
      );

      await act(async () => {
        onlineManager.setOnline(true);
      });
      await user.click(screen.getByRole("button", { name: "重试刷新" }));

      expect(
        await screen.findByText(
          "登录资料刷新仍然失败。显示名已保存，请稍后重试。",
        ),
      ).toBeInTheDocument();
      expect(
        app.client.getQueryData<User>(["auth", "current-user"]),
      ).toEqual(USER);
      expect(
        screen.getByRole("button", { name: "重试刷新" }),
      ).toBeInTheDocument();
      expect(
        screen.queryByText("暂时无法确认登录状态"),
      ).not.toBeInTheDocument();
    } finally {
      await act(async () => {
        onlineManager.setOnline(true);
      });
    }
  });

  it("显示名写入后刷新遇到5xx时保留已认证身份并提供重试", async () => {
    const adminUser: User = { ...USER, role: "admin" };
    let authMeCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = new URL(getRequestUrl(input), "http://localhost")
          .pathname;
        if (path.endsWith("/auth/me") && init?.method === "PATCH") {
          return jsonResponse({ ...adminUser, display_name: "已写入" });
        }
        if (path.endsWith("/auth/me")) {
          authMeCalls += 1;
          return authMeCalls === 1
            ? jsonResponse(adminUser)
            : jsonResponse({ detail: "http_error" }, 500);
        }
        if (path === "/api/v1/projects") {
          return jsonResponse({ items: [] });
        }
        throw new Error(`Unexpected request: ${init?.method} ${path}`);
      }),
    );
    const user = userEvent.setup();
    const app = renderApp("/profile");

    const displayName = await screen.findByLabelText(/显示名/);
    await user.clear(displayName);
    await user.type(displayName, "已写入");
    await user.click(screen.getByRole("button", { name: "保存显示名" }));

    expect(
      await screen.findByText(
        "显示名已保存，但登录资料刷新失败。请重试刷新。",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "管理" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "重试刷新" }),
    ).toBeInTheDocument();
    expect(
      app.client.getQueryData<User>(["auth", "current-user"]),
    ).toEqual(adminUser);
    expect(
      screen.queryByText("暂时无法确认登录状态"),
    ).not.toBeInTheDocument();
  });

  it("显示名写入后刷新遇到401时仍清空身份并返回登录", async () => {
    let authMeCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = new URL(getRequestUrl(input), "http://localhost")
          .pathname;
        if (path.endsWith("/auth/me") && init?.method === "PATCH") {
          return jsonResponse({ ...USER, display_name: "已写入" });
        }
        if (path.endsWith("/auth/me")) {
          authMeCalls += 1;
          return authMeCalls === 1
            ? jsonResponse(USER)
            : jsonResponse({ detail: "not_authenticated" }, 401);
        }
        if (path === "/api/v1/projects") {
          return jsonResponse({ items: [] });
        }
        throw new Error(`Unexpected request: ${init?.method} ${path}`);
      }),
    );
    const user = userEvent.setup();
    const app = renderApp("/profile");

    const displayName = await screen.findByLabelText(/显示名/);
    app.client.setQueryData(["knowledge", "directory"], [
      { id: "stale-knowledge", name: "旧知识库" },
    ]);
    await user.clear(displayName);
    await user.type(displayName, "已写入");
    await user.click(screen.getByRole("button", { name: "保存显示名" }));

    await waitForStableLoginScreen(app);
    expect(
      app.client.getQueryData<User | null>(["auth", "current-user"]),
    ).toBeNull();
    expect(
      app.client.getQueryData(["knowledge", "directory"]),
    ).toBeUndefined();
  });

  it("显示名保存失败时保留输入并显示服务端错误", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me") && init?.method === "GET") return jsonResponse(USER);
        if (url.endsWith("/auth/me") && init?.method === "PATCH") {
          return jsonResponse({ detail: "display_name_invalid" }, 422);
        }
        if (url.includes("/api/v1/projects")) {
          return jsonResponse({ items: [], page: 1, page_size: 100, total: 0 });
        }
        if (url.includes("/api/v1/sessions")) {
          return jsonResponse({ items: [], page: 1, page_size: 8, total: 0 });
        }
        throw new Error(`Unexpected request: ${init?.method} ${url}`);
      }),
    );
    const user = userEvent.setup();
    renderApp("/profile");
    const displayName = await screen.findByLabelText(/显示名/);
    await user.clear(displayName);
    await user.type(displayName, "bad-name");
    await user.click(screen.getByRole("button", { name: "保存显示名" }));
    expect(await screen.findByText(/显示名最多 80 个字符/)).toBeInTheDocument();
    expect(displayName).toHaveValue("bad-name");
  });

  it("显示名按Unicode码点校验，较长Emoji名称不会被输入框截断", async () => {
    const emojiName = "😀".repeat(50);
    let savedDisplayName = "";
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me") && init?.method === "GET") return jsonResponse(USER);
        if (url.endsWith("/auth/me") && init?.method === "PATCH") {
          savedDisplayName = JSON.parse(String(init.body)).display_name as string;
          return jsonResponse({ ...USER, display_name: savedDisplayName });
        }
        if (url.includes("/api/v1/projects")) {
          return jsonResponse({ items: [], page: 1, page_size: 100, total: 0 });
        }
        if (url.includes("/api/v1/sessions")) {
          return jsonResponse({ items: [], page: 1, page_size: 8, total: 0 });
        }
        throw new Error(`Unexpected request: ${init?.method} ${url}`);
      }),
    );
    const user = userEvent.setup();
    renderApp("/profile");
    const displayName = await screen.findByLabelText(/显示名/);
    fireEvent.change(displayName, { target: { value: emojiName } });
    expect(displayName).toHaveValue(emojiName);
    await user.click(screen.getByRole("button", { name: "保存显示名" }));
    await screen.findByText("显示名已更新。");
    expect(savedDisplayName).toBe(emojiName);
    expect(Array.from(savedDisplayName)).toHaveLength(50);
  });

  it("管理员在应用外壳中看到管理入口", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({ ...USER, role: "admin" }),
      ),
    );

    const app = renderApp("/chat");

    await waitForStableChatWorkspace(app);
    expect(screen.getByRole("link", { name: "管理" })).toBeInTheDocument();
  });

  it("管理子导航始终渲染在主区域并提供完整管理路由入口", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me")) {
          return jsonResponse({ ...USER, role: "admin" });
        }
        if (url.endsWith("/admin/llm/model-profiles")) {
          return jsonResponse({
            default_profile_id: null,
            items: [],
          });
        }
        if (url.endsWith("/admin/llm/compression-policy")) {
          return jsonResponse({
            compression_model_profile_id: null,
            created_at: null,
            id: null,
            prompt_template_version: null,
            recent_ratio: null,
            recent_token_cap: null,
            summary_ratio: null,
            summary_token_cap: null,
            trigger_tokens: null,
          });
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    const app = renderApp("/admin/models");

    await waitForStableAdminModelsPage(app);
    const subnav = screen.getByRole("navigation", { name: "管理子导航" });
    expect(subnav.closest(".workspace-sidebar")).toBeNull();
    expect(subnav.closest(".main-column")).not.toBeNull();
    expect(
      within(subnav).getByRole("link", { name: "模型与压缩策略" }),
    ).toHaveAttribute("href", "/admin/models");
    expect(
      within(subnav).getByRole("link", { name: "MCP Server" }),
    ).toHaveAttribute("href", "/admin/mcp");
    expect(
      within(subnav).getByRole("link", { name: "工具审批策略" }),
    ).toHaveAttribute("href", "/admin/tools");
    expect(
      within(subnav).getByRole("link", { name: "系统提示词" }),
    ).toHaveAttribute("href", "/admin/system-prompts");
    expect(
      within(subnav).getByRole("link", { name: "技能管理" }),
    ).toHaveAttribute("href", "/admin/skills");
  });
});
