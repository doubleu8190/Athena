import {
  screen,
  waitFor,
  within,
} from "@testing-library/react";
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
    expect(screen.getAllByText("林知远").length).toBeGreaterThan(0);
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
    await user.click(
      screen.getAllByRole("button", { name: "退出登录" })[0],
    );

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

  it("管理子导航始终渲染在主区域并提供三类管理路由入口", async () => {
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
            summary_ratio: null,
            trigger_ratio: null,
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
  });
});
