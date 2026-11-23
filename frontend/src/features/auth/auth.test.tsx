import {
  act,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { onlineManager } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, apiRequest } from "../../shared/api/client";
import { makeDeptAdmin, makeSuperAdmin, makeUser } from "../../test/fixtures";
import { adminKeys } from "../admin/queryKeys";
import { jsonResponse, renderApp } from "../../test/testUtils";
import { buildFeishuAuthorizeUrl } from "./api";
import { authErrorCodeMessage, authErrorMessage } from "./errorMessages";
import { authBrowserNavigation } from "./LoginPage";
import {
  canManageSkills,
  getPrimaryDepartment,
  getUserDisplayName,
  getUserRoleLabel,
  isDeptAdmin,
  isSuperAdmin,
  type AuthProvidersResponse,
  type MockUser,
  type User,
} from "./model";
import { getSafeReturnTo, sanitizeReturnTo } from "./returnTo";

const USER: User = makeUser();
const SUPER_ADMIN: User = makeSuperAdmin({
  display_name: "系统超管",
  id: "super-admin-1",
});
const DEPT_ADMIN: User = makeDeptAdmin({
  display_name: "财务主管",
  id: "dept-admin-1",
});

const LOCAL_ONLY_PROVIDERS: AuthProvidersResponse = {
  feishu_enabled: false,
  local_login_enabled: true,
  mock_enabled: false,
};

const MOCK_USERS_FIXTURE: MockUser[] = [
  {
    avatar_url: null,
    departments: [
      {
        id: "dept-root",
        is_primary: true,
        name: "集团总部",
        path: "/dept-root/",
      },
    ],
    display_name: "超级管理员",
    email: "super@mock.athena.local",
    employee_no: "MOCK001",
    id: "mock_super_admin",
    job_title: "信息安全负责人",
    managed_department_ids: [],
    role: "super_admin",
  },
  {
    avatar_url: null,
    departments: [
      {
        id: "dept-other",
        is_primary: false,
        name: "审计组",
        path: "/dept-root/dept-audit/",
      },
      {
        id: "dept-fin",
        is_primary: true,
        name: "财务部",
        path: "/dept-root/dept-fin/",
      },
    ],
    display_name: "财务主管",
    email: null,
    employee_no: "MOCK002",
    id: "mock_fin_admin",
    job_title: "财务部负责人",
    managed_department_ids: ["dept-fin"],
    role: "dept_admin",
  },
  {
    avatar_url: null,
    departments: [
      {
        id: "dept-rd",
        is_primary: false,
        name: "研发部",
        path: "/dept-root/dept-rd/",
      },
    ],
    display_name: null,
    email: null,
    employee_no: "MOCK003",
    id: "mock_rd_member",
    job_title: "后端工程师",
    managed_department_ids: [],
    role: "member",
  },
];

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

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

async function waitForStableLocalLoginScreen(
  app: ReturnType<typeof renderApp>,
) {
  await waitFor(() => {
    expect(app.getLocation().pathname).toBe("/login");
    expect(
      screen.getByRole("heading", { name: "登录工作台" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("工作邮箱")).toBeEnabled();
    expect(screen.getByLabelText("密码")).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "使用邮箱登录" }),
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
      within(main).getAllByRole("button", { name: "新建项目" }).length,
    ).toBeGreaterThan(0);
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

afterEach(() => {
  vi.restoreAllMocks();
});

describe("认证领域工具与安全路径校验", () => {
  it("准确判断企业三级角色、中文标签、主部门与可空邮箱展示名", () => {
    expect(isSuperAdmin(SUPER_ADMIN)).toBe(true);
    expect(isDeptAdmin(SUPER_ADMIN)).toBe(false);
    expect(canManageSkills(SUPER_ADMIN)).toBe(true);

    expect(isSuperAdmin(DEPT_ADMIN)).toBe(false);
    expect(isDeptAdmin(DEPT_ADMIN)).toBe(true);
    expect(canManageSkills(DEPT_ADMIN)).toBe(true);

    expect(isSuperAdmin(USER)).toBe(false);
    expect(isDeptAdmin(USER)).toBe(false);
    expect(canManageSkills(USER)).toBe(false);

    expect(getUserRoleLabel("super_admin")).toBe("超级管理员");
    expect(getUserRoleLabel("dept_admin")).toBe("部门管理员");
    expect(getUserRoleLabel("member")).toBe("员工");

    expect(getPrimaryDepartment(MOCK_USERS_FIXTURE[1])?.name).toBe("财务部");
    expect(getPrimaryDepartment(MOCK_USERS_FIXTURE[2])?.name).toBe("研发部");
    expect(
      getPrimaryDepartment({ departments: [] }),
    ).toBeNull();

    expect(
      getUserDisplayName({
        display_name: "  林知远 ",
        email: "lin@example.com",
        employee_no: "E01",
      }),
    ).toBe("林知远");
    expect(
      getUserDisplayName({
        display_name: null,
        email: "feishu@company.com",
        employee_no: "E01",
      }),
    ).toBe("feishu");
    expect(
      getUserDisplayName({
        display_name: " ",
        email: null,
        employee_no: "MOCK003",
      }),
    ).toBe("MOCK003");
    expect(
      getUserDisplayName({
        display_name: null,
        email: null,
        employee_no: null,
      }),
    ).toBe("Athena 员工");
  });

  it("拦截外链、协议相对路径、反斜杠、控制字符以及 /login 与 /register", () => {
    expect(sanitizeReturnTo("/projects?view=all#top")).toBe(
      "/projects?view=all#top",
    );
    expect(sanitizeReturnTo("//evil.com/phish")).toBe("/chat");
    expect(sanitizeReturnTo("https://evil.com")).toBe("/chat");
    expect(sanitizeReturnTo("/\\evil.com")).toBe("/chat");
    expect(sanitizeReturnTo("/chat\r\nSet-Cookie: bad=1")).toBe("/chat");
    expect(sanitizeReturnTo("/login")).toBe("/chat");
    expect(sanitizeReturnTo("/login?reason=expired")).toBe("/chat");
    expect(sanitizeReturnTo("/register")).toBe("/chat");
    expect(sanitizeReturnTo("")).toBe("/chat");
    expect(sanitizeReturnTo(null)).toBe("/chat");

    expect(getSafeReturnTo({ returnTo: "/knowledge" }, "?return_to=/skills")).toBe(
      "/knowledge",
    );
    expect(getSafeReturnTo(null, "?return_to=%2Fskills%3Ftab%3Dmine")).toBe(
      "/skills?tab=mine",
    );
    expect(getSafeReturnTo(null, "?return_to=//evil.com")).toBe("/chat");

    expect(buildFeishuAuthorizeUrl("/projects?view=all")).toBe(
      "/api/v1/auth/feishu/authorize?return_to=%2Fprojects%3Fview%3Dall",
    );
    expect(buildFeishuAuthorizeUrl("//evil.com")).toBe(
      "/api/v1/auth/feishu/authorize?return_to=%2Fchat",
    );
  });

  it("映射一期新增认证错误码、拦截原型属性并在未知 URL 错误码时不回显原始参数", () => {
    expect(authErrorMessage(new ApiError(403, "user_disabled"))).toBe(
      "该账号已被停用或已离职，无法登录。",
    );
    expect(
      authErrorMessage(new ApiError(400, "password_change_not_supported")),
    ).toBe("企业单点登录账号由企业统一管理密码，不支持在此修改。");
    expect(authErrorCodeMessage("invalid_oauth_state")).toBe(
      "登录校验已失效，请重新点击飞书登录。",
    );
    expect(authErrorCodeMessage("mock_user_not_found")).toBe(
      "所选体验账号不存在或已不可用。",
    );
    expect(authErrorCodeMessage("unknown_injected_text")).toBe(
      "登录未能完成，请重试或联系企业 IT 管理员。",
    );
    // 严格防止 __proto__ / constructor / toString 等原型属性污染或导致 React 渲染崩溃。
    for (const protoKey of ["__proto__", "constructor", "toString"]) {
      expect(authErrorCodeMessage(protoKey)).toBe(
        "登录未能完成，请重试或联系企业 IT 管理员。",
      );
      expect(authErrorMessage(new ApiError(400, protoKey))).toBe(
        "请求未完成，请稍后再试。",
      );
    }
    expect(authErrorCodeMessage(null)).toBeNull();
  });
});

describe("企业登录页动态状态机（飞书 SSO + Mock 快捷切换 + 应急本地登录）", () => {
  it("访问已下线的 /register 会自动重定向至 /login", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me")) {
          return jsonResponse({ detail: "not_authenticated" }, 401);
        }
        if (url.endsWith("/auth/providers")) {
          return jsonResponse(LOCAL_ONLY_PROVIDERS);
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    const app = renderApp("/register");
    await waitForStableLocalLoginScreen(app);
    expect(app.getLocation().pathname).toBe("/login");
  });

  it("providers 加载期间显示骨架屏，失败时展示错误并支持重新加载恢复", async () => {
    const providersGate = deferred<Response>();
    let providerCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me")) {
          return jsonResponse({ detail: "not_authenticated" }, 401);
        }
        if (url.endsWith("/auth/providers")) {
          providerCalls += 1;
          if (providerCalls === 1) {
            return providersGate.promise;
          }
          return jsonResponse({
            feishu_enabled: true,
            local_login_enabled: false,
            mock_enabled: false,
          });
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );
    const user = userEvent.setup();

    renderApp("/login");

    // 骨架屏展示且未闪出任何登录按钮。
    expect(
      await screen.findByRole("status", { name: "正在加载登录配置" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "使用飞书账号登录" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "使用邮箱登录" }),
    ).not.toBeInTheDocument();

    providersGate.resolve(jsonResponse({ detail: "http_error" }, 500));
    expect(await screen.findByText("无法加载登录配置")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "重新加载" }));
    expect(
      await screen.findByRole("button", { name: "使用飞书账号登录" }),
    ).toBeEnabled();
    expect(screen.queryByLabelText("工作邮箱")).not.toBeInTheDocument();
    expect(
      screen.queryByText("非生产联调 · 快捷切换角色"),
    ).not.toBeInTheDocument();
  });

  it("仅开启飞书 + 本地时默认折叠应急邮箱表单，点击展开后可登录，点击飞书安全构造授权地址", async () => {
    const externalAssignSpy = vi
      .spyOn(authBrowserNavigation, "assign")
      .mockImplementation(() => undefined);

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me")) {
          return jsonResponse({ detail: "not_authenticated" }, 401);
        }
        if (url.endsWith("/auth/providers")) {
          return jsonResponse({
            feishu_enabled: true,
            local_login_enabled: true,
            mock_enabled: false,
          });
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );
    const user = userEvent.setup();

    renderApp("/login?return_to=%2Fprojects%3Fview%3Dall");

    const feishuButton = await screen.findByRole("button", {
      name: "使用飞书账号登录",
    });
    expect(feishuButton).toBeEnabled();
    // 本地应急表单默认折叠不可见。
    expect(screen.queryByLabelText("工作邮箱")).not.toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "管理员 / 应急邮箱密码登录" }),
    );
    expect(await screen.findByLabelText("工作邮箱")).toBeInTheDocument();

    await user.click(feishuButton);
    expect(externalAssignSpy).toHaveBeenCalledTimes(1);
    expect(externalAssignSpy).toHaveBeenCalledWith(
      "/api/v1/auth/feishu/authorize?return_to=%2Fprojects%3Fview%3Dall",
    );
    // 跳转中按钮进入 loading 禁用态防重复触发。
    expect(feishuButton).toBeDisabled();
    expect(feishuButton).toHaveAttribute("aria-busy", "true");
  });

  it("飞书 SSO 对恶意 ?return_to=//evil.com 强制回退至 /chat，并展示回调错误与过期横幅", async () => {
    const externalAssignSpy = vi
      .spyOn(authBrowserNavigation, "assign")
      .mockImplementation(() => undefined);

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me")) {
          return jsonResponse({ detail: "not_authenticated" }, 401);
        }
        if (url.endsWith("/auth/providers")) {
          return jsonResponse({
            feishu_enabled: true,
            local_login_enabled: false,
            mock_enabled: false,
          });
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );
    const user = userEvent.setup();

    renderApp("/login?reason=expired&error=user_disabled&return_to=//evil.com");

    expect(
      await screen.findByText("登录状态已过期或账号权限已变更，请重新登录。"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("该账号已被停用或已离职，无法登录。"),
    ).toBeInTheDocument();

    await user.click(
      await screen.findByRole("button", { name: "使用飞书账号登录" }),
    );
    expect(externalAssignSpy).toHaveBeenCalledWith(
      "/api/v1/auth/feishu/authorize?return_to=%2Fchat",
    );
  });

  it("全部登录开关关闭时渲染 EmptyState 兜底提示联系 IT 管理员", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me")) {
          return jsonResponse({ detail: "not_authenticated" }, 401);
        }
        if (url.endsWith("/auth/providers")) {
          return jsonResponse({
            feishu_enabled: false,
            local_login_enabled: false,
            mock_enabled: false,
          });
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    renderApp("/login");

    expect(
      await screen.findByRole("heading", { name: "暂不可登录" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/请联系企业 IT 管理员检查登录配置/),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("工作邮箱")).not.toBeInTheDocument();
  });

  it("Mock 快捷登录展示角色网格、防并发连点并在成功后回到原目标路径", async () => {
    const mockLoginGate = deferred<Response>();
    const mockLoginCalls: string[] = [];

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me")) {
          return jsonResponse({ detail: "not_authenticated" }, 401);
        }
        if (url.endsWith("/auth/providers")) {
          return jsonResponse({
            feishu_enabled: false,
            local_login_enabled: true,
            mock_enabled: true,
          });
        }
        if (url.endsWith("/auth/mock/users")) {
          return jsonResponse({ items: MOCK_USERS_FIXTURE });
        }
        if (url.endsWith("/auth/mock/login") && init?.method === "POST") {
          const payload = JSON.parse(String(init.body)) as { user_id: string };
          mockLoginCalls.push(payload.user_id);
          return mockLoginGate.promise;
        }
        if (url.includes("/api/v1/projects")) {
          return jsonResponse({ items: [], page: 1, page_size: 100, total: 0 });
        }
        if (url.includes("/api/v1/sessions")) {
          return jsonResponse({ items: [], page: 1, page_size: 8, total: 0 });
        }
        throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
      }),
    );
    const user = userEvent.setup();

    const app = renderApp("/projects?view=all");

    expect(
      await screen.findByText("非生产联调 · 快捷切换角色"),
    ).toBeInTheDocument();
    // 同步开启 Mock 与本地时，本地表单默认折叠。
    expect(screen.queryByLabelText("工作邮箱")).not.toBeInTheDocument();

    const finAdminButton = await screen.findByRole("button", {
      name: /财务主管/,
    });
    const superAdminButton = screen.getByRole("button", {
      name: /超级管理员/,
    });
    const rdMemberButton = screen.getByRole("button", { name: /MOCK003/ });

    // 验证卡片展示角色、主部门、等宽工号（且兼容无邮箱/无显示名账号）。
    expect(within(finAdminButton).getByText("部门管理员")).toBeInTheDocument();
    expect(within(finAdminButton).getByText("财务部")).toBeInTheDocument();
    expect(within(finAdminButton).getByText("MOCK002")).toBeInTheDocument();
    expect(within(rdMemberButton).getByText("员工")).toBeInTheDocument();
    expect(within(rdMemberButton).getByText("研发部")).toBeInTheDocument();

    await user.click(finAdminButton);
    // 请求进行中：目标卡片标记 aria-busy，全部卡片禁用防止并发竞态。
    expect(finAdminButton).toBeDisabled();
    expect(finAdminButton).toHaveAttribute("aria-busy", "true");
    expect(superAdminButton).toBeDisabled();
    expect(rdMemberButton).toBeDisabled();

    // 尝试点击另一张卡片不应发起第二次请求。
    fireEvent.click(superAdminButton);
    expect(mockLoginCalls).toEqual(["mock_fin_admin"]);

    mockLoginGate.resolve(
      jsonResponse({
        ...DEPT_ADMIN,
        auth_provider: "mock",
        email: null,
        id: "mock_fin_admin",
      }),
    );
    await waitForStableProjectsPage(app);

    // 验证无邮箱账号打开账户菜单不崩溃且显示工号与角色。
    await user.click(screen.getByRole("button", { name: "账户菜单" }));
    expect(screen.getByText("财务主管")).toBeInTheDocument();
    expect(screen.getByText("部门管理员")).toBeInTheDocument();
  });

  it.each(["__proto__", "constructor", "toString"])(
    "URL ?error=%s 原型属性参数不会导致登录页白屏崩溃并展示受控中文兜底文案",
    async (errorParam) => {
      vi.stubGlobal(
        "fetch",
        vi.fn(async (input: RequestInfo | URL) => {
          const url = getRequestUrl(input);
          if (url.endsWith("/auth/me")) {
            return jsonResponse({ detail: "not_authenticated" }, 401);
          }
          if (url.endsWith("/auth/providers")) {
            return jsonResponse(LOCAL_ONLY_PROVIDERS);
          }
          throw new Error(`Unexpected request: ${url}`);
        }),
      );

      renderApp(`/login?error=${encodeURIComponent(errorParam)}`);

      expect(
        await screen.findByRole("heading", { name: "登录工作台" }),
      ).toBeInTheDocument();
      expect(
        screen.getByText("登录未能完成，请重试或联系企业 IT 管理员。"),
      ).toBeInTheDocument();
    },
  );

  it("飞书外跳后经 bfcache pageshow 返回，或外跳同步抛错时均自动解锁登录按钮", async () => {
    let shouldThrow = true;
    const externalAssignSpy = vi
      .spyOn(authBrowserNavigation, "assign")
      .mockImplementation(() => {
        if (shouldThrow) {
          throw new Error("navigation blocked");
        }
      });

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me")) {
          return jsonResponse({ detail: "not_authenticated" }, 401);
        }
        if (url.endsWith("/auth/providers")) {
          return jsonResponse({
            feishu_enabled: true,
            local_login_enabled: true,
            mock_enabled: false,
          });
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );
    const user = userEvent.setup();

    renderApp("/login");
    const feishuButton = await screen.findByRole("button", {
      name: "使用飞书账号登录",
    });

    // 1. 同步抛错：必须立即释放锁定与 loading，展示错误提示并允许重试。
    await user.click(feishuButton);
    expect(
      await screen.findByText("无法跳转至飞书登录页，请检查浏览器设置后重试。"),
    ).toBeInTheDocument();
    expect(feishuButton).toBeEnabled();
    expect(feishuButton).not.toHaveAttribute("aria-busy", "true");

    // 2. 正常外跳进入 loading 后，模拟浏览器后退触发 bfcache pageshow(persisted=true) 恢复按钮。
    shouldThrow = false;
    await user.click(feishuButton);
    expect(feishuButton).toBeDisabled();
    expect(feishuButton).toHaveAttribute("aria-busy", "true");

    act(() => {
      const pageShowEvent = new Event("pageshow");
      Object.defineProperty(pageShowEvent, "persisted", { value: true });
      window.dispatchEvent(pageShowEvent);
    });

    expect(feishuButton).toBeEnabled();
    expect(feishuButton).not.toHaveAttribute("aria-busy", "true");

    // 解锁后可再次点击触发外跳。
    await user.click(feishuButton);
    expect(externalAssignSpy).toHaveBeenCalledTimes(3);
  });

  it("Mock 快捷登录失败时展示中文错误（含停用账号定制文案）并解除卡片禁用", async () => {
    let loginErrorDetail = "mock_user_not_found";
    let loginStatus = 404;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me")) {
          return jsonResponse({ detail: "not_authenticated" }, 401);
        }
        if (url.endsWith("/auth/providers")) {
          return jsonResponse({
            feishu_enabled: false,
            local_login_enabled: false,
            mock_enabled: true,
          });
        }
        if (url.endsWith("/auth/mock/users")) {
          return jsonResponse({ items: [MOCK_USERS_FIXTURE[0]] });
        }
        if (url.endsWith("/auth/mock/login")) {
          return jsonResponse({ detail: loginErrorDetail }, loginStatus);
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );
    const user = userEvent.setup();

    renderApp("/login");
    const card = await screen.findByRole("button", { name: /超级管理员/ });
    await user.click(card);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "所选体验账号不存在或已不可用。",
    );
    expect(card).toBeEnabled();

    // 后端契约 1.4 节：Mock 账号已停用返回 401 invalid_credentials。
    loginErrorDetail = "invalid_credentials";
    loginStatus = 401;
    await user.click(card);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "该体验账号已被停用，无法登录。",
    );
    expect(
      screen.queryByText("登录状态已过期或账号权限已变更，请重新登录。"),
    ).not.toBeInTheDocument();
    expect(card).toBeEnabled();
  });
});

describe("认证流程与本地应急登录", () => {
  it("支持失焦校验、输入清除错误、密码显隐切换及提交聚焦首个错误字段", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me")) {
          return jsonResponse({ detail: "not_authenticated" }, 401);
        }
        if (url.endsWith("/auth/providers")) {
          return jsonResponse(LOCAL_ONLY_PROVIDERS);
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );
    const user = userEvent.setup();
    const app = renderApp("/login");
    await waitForStableLocalLoginScreen(app);

    const emailInput = screen.getByLabelText("工作邮箱");
    const passwordInput = screen.getByLabelText("密码");

    // 失焦校验邮箱格式错误，但不应抢走转移到密码框的焦点。
    await user.type(emailInput, "not-an-email");
    await user.click(passwordInput);
    expect(screen.getByText("请输入有效的邮箱地址。")).toBeInTheDocument();
    expect(emailInput).toHaveAttribute("aria-invalid", "true");
    expect(passwordInput).toHaveFocus();

    // 输入清除邮箱错误。
    await user.clear(emailInput);
    await user.type(emailInput, "lin@example.com");
    expect(
      screen.queryByText("请输入有效的邮箱地址。"),
    ).not.toBeInTheDocument();

    // 切换密码显隐。
    const toggleButton = screen.getByRole("button", { name: "显示密码" });
    expect(passwordInput).toHaveAttribute("type", "password");
    await user.click(toggleButton);
    expect(passwordInput).toHaveAttribute("type", "text");
    expect(
      screen.getByRole("button", { name: "隐藏密码" }),
    ).toHaveAttribute("aria-pressed", "true");

    // 提交时密码为空：校验失败并将焦点移到密码输入框。
    await user.click(screen.getByRole("button", { name: "使用邮箱登录" }));
    expect(screen.getByText("请输入密码。")).toBeInTheDocument();
    expect(passwordInput).toHaveFocus();
  });

  it("登录成功后更新认证缓存并进入工作区", async () => {
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me")) {
          return jsonResponse({ detail: "not_authenticated" }, 401);
        }
        if (url.endsWith("/auth/providers")) {
          return jsonResponse(LOCAL_ONLY_PROVIDERS);
        }
        if (url.endsWith("/auth/login")) {
          return jsonResponse(USER);
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
        throw new Error(`Unexpected request: ${init?.method} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    const app = renderApp("/login");

    await waitForStableLocalLoginScreen(app);
    await user.type(screen.getByLabelText("工作邮箱"), "lin@example.com");
    await user.type(
      screen.getByLabelText("密码"),
      "correct-horse-battery",
    );
    await user.click(screen.getByLabelText("在此设备保持登录 30 天"));
    await user.click(screen.getByRole("button", { name: "使用邮箱登录" }));

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

  it("匿名登录输错密码返回 401 invalid_credentials 时仅展示表单错误，不误触发“登录已过期”横幅", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me")) {
          return jsonResponse({ detail: "not_authenticated" }, 401);
        }
        if (url.endsWith("/auth/providers")) {
          return jsonResponse(LOCAL_ONLY_PROVIDERS);
        }
        if (url.endsWith("/auth/login")) {
          return jsonResponse({ detail: "invalid_credentials" }, 401);
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );
    const user = userEvent.setup();

    const app = renderApp("/login");

    await waitForStableLocalLoginScreen(app);
    await user.type(screen.getByLabelText("工作邮箱"), "lin@example.com");
    await user.type(screen.getByLabelText("密码"), "wrong-password-123");
    await user.click(screen.getByRole("button", { name: "使用邮箱登录" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "邮箱或密码不正确。",
    );
    expect(
      screen.queryByText("登录状态已过期或账号权限已变更，请重新登录。"),
    ).not.toBeInTheDocument();
    expect(app.getLocation().search).toBe("");
    expect(
      screen.getByRole("button", { name: "使用邮箱登录" }),
    ).toBeEnabled();
  });

  it("登录可完整输入并提交 128 个非 BMP 密码码点", async () => {
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me")) {
          return jsonResponse({ detail: "not_authenticated" }, 401);
        }
        if (url.endsWith("/auth/providers")) {
          return jsonResponse(LOCAL_ONLY_PROVIDERS);
        }
        if (url.endsWith("/auth/login")) {
          return jsonResponse(USER);
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
        throw new Error(`Unexpected request: ${init?.method} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    const password = "😀".repeat(128);

    const app = renderApp("/login");

    await waitForStableLocalLoginScreen(app);
    await user.type(screen.getByLabelText("工作邮箱"), "lin@example.com");
    await user.type(screen.getByLabelText("密码"), password);
    expect(screen.getByLabelText("密码")).toHaveValue(password);
    await user.click(screen.getByRole("button", { name: "使用邮箱登录" }));

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
        if (url.endsWith("/auth/providers")) {
          return jsonResponse(LOCAL_ONLY_PROVIDERS);
        }
        if (url.endsWith("/auth/login")) {
          return jsonResponse(USER);
        }
        if (url.includes("/api/v1/projects")) {
          return jsonResponse({ items: [], page: 1, page_size: 100, total: 0 });
        }
        if (url.includes("/api/v1/sessions")) {
          return jsonResponse({ items: [], page: 1, page_size: 8, total: 0 });
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );
    const user = userEvent.setup();

    const app = renderApp("/projects?view=all");

    await waitForStableLocalLoginScreen(app);
    expect(app.getLocation().search).toBe("");
    await user.type(screen.getByLabelText("工作邮箱"), "lin@example.com");
    await user.type(screen.getByLabelText("密码"), "correct-horse-battery");
    await user.click(screen.getByRole("button", { name: "使用邮箱登录" }));

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
      screen.queryByRole("heading", { name: "登录工作台" }),
    ).not.toBeInTheDocument();
  });

  it("主动退出登录调用真实接口并回到登录页且不显示“登录已过期”横幅", async () => {
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me")) {
          return jsonResponse(USER);
        }
        if (url.endsWith("/auth/providers")) {
          return jsonResponse(LOCAL_ONLY_PROVIDERS);
        }
        if (
          url.endsWith("/auth/logout") &&
          init?.method === "POST"
        ) {
          return new Response(null, { status: 204 });
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
        throw new Error(`Unexpected request: ${init?.method} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    const app = renderApp("/chat");

    await waitForStableChatWorkspace(app);
    await clickLogout(user);

    await waitForStableLocalLoginScreen(app);
    expect(app.getLocation().search).toBe("");
    expect(
      screen.queryByText("登录状态已过期或账号权限已变更，请重新登录。"),
    ).not.toBeInTheDocument();
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
        if (url.includes("/api/v1/projects")) return jsonResponse({ items: [] });
        if (url.includes("/api/v1/sessions")) return jsonResponse({ items: [] });
        if (url.endsWith("/api/v1/skills")) return jsonResponse({ items: [] });
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
    expect(
      screen.queryByRole("menuitem", { name: "我的技能" }),
    ).not.toBeInTheDocument();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("menuitem", { name: "退出登录" })).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(profileItem).toHaveFocus();
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
    expect(screen.getByRole("link", { name: "技能库" })).toHaveFocus();
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
      if (path === "/api/v1/sessions") return jsonResponse({ items: [] });
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
        if (path === "/api/v1/sessions") {
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
        if (path === "/api/v1/sessions") {
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
        if (path === "/api/v1/sessions") {
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
    let authMeCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = new URL(getRequestUrl(input), "http://localhost")
          .pathname;
        if (path.endsWith("/auth/me") && init?.method === "PATCH") {
          return jsonResponse({ ...SUPER_ADMIN, display_name: "已写入" });
        }
        if (path.endsWith("/auth/me")) {
          authMeCalls += 1;
          return authMeCalls === 1
            ? jsonResponse(SUPER_ADMIN)
            : jsonResponse({ detail: "http_error" }, 500);
        }
        if (path === "/api/v1/projects") {
          return jsonResponse({ items: [] });
        }
        if (path === "/api/v1/sessions") {
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
    ).toEqual(SUPER_ADMIN);
    expect(
      screen.queryByText("暂时无法确认登录状态"),
    ).not.toBeInTheDocument();
  });

  it("显示名写入后刷新遇到401时清空身份并携带过期提示返回登录", async () => {
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
        if (path.endsWith("/auth/providers")) {
          return jsonResponse(LOCAL_ONLY_PROVIDERS);
        }
        if (path === "/api/v1/projects") {
          return jsonResponse({ items: [] });
        }
        if (path === "/api/v1/sessions") {
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

    await waitForStableLocalLoginScreen(app);
    expect(app.getLocation()).toEqual({
      pathname: "/login",
      search: "?reason=expired",
    });
    expect(
      screen.getByText("登录状态已过期或账号权限已变更，请重新登录。"),
    ).toBeInTheDocument();
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
});

describe("全站三级角色与分级路由守卫", () => {
  it("超级管理员访问 /admin 自动重定向至 /admin/models 并显示完整管理子导航", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me")) {
          return jsonResponse(SUPER_ADMIN);
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

    const app = renderApp("/admin");

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
      within(subnav).getByRole("link", { name: "公共技能" }),
    ).toHaveAttribute("href", "/admin/skills");
  });

  it("部门管理员访问 /admin 自动重定向至 /admin/skills 且仅展示技能管理导航", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me")) {
          return jsonResponse(DEPT_ADMIN);
        }
        if (url.includes("/api/v1/admin/skills")) {
          return jsonResponse({ items: [] });
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    const app = renderApp("/admin");

    await waitFor(() => {
      expect(app.getLocation().pathname).toBe("/admin/skills");
    });
    expect(screen.getByRole("link", { name: "管理" })).toBeInTheDocument();
    const subnav = screen.getByRole("navigation", { name: "管理子导航" });
    expect(
      within(subnav).getByRole("link", { name: "公共技能" }),
    ).toHaveAttribute("href", "/admin/skills");
    expect(
      within(subnav).queryByRole("link", { name: "模型与压缩策略" }),
    ).not.toBeInTheDocument();
  });

  it("部门管理员访问超管专属路由 /admin/models 会被 SuperAdminRoute 拦截", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = getRequestUrl(input);
      if (url.endsWith("/auth/me")) {
        return jsonResponse(DEPT_ADMIN);
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    renderApp("/admin/models");

    expect(
      await screen.findByRole("heading", { name: "当前账号没有管理员权限" }),
    ).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.every(([input]) =>
        getRequestUrl(input).endsWith("/auth/me"),
      ),
    ).toBe(true);
  });

  it("普通员工访问 /admin 与 /admin/skills 均被拦截且不发管理接口请求", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = getRequestUrl(input);
      if (url.endsWith("/auth/me")) {
        return jsonResponse(USER);
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const app = renderApp("/admin/skills");
    expect(
      await screen.findByRole("heading", { name: "当前账号没有管理员权限" }),
    ).toBeInTheDocument();

    await act(async () => {
      await app.navigateTo("/admin");
    });
    expect(
      await screen.findByRole("heading", { name: "当前账号没有管理员权限" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "管理" })).not.toBeInTheDocument();
    expect(
      fetchMock.mock.calls.every(([input]) =>
        getRequestUrl(input).endsWith("/auth/me"),
      ),
    ).toBe(true);
  });

  it("部门管理员在 /admin/skills 遇超管路径或资源级 403 admin_required 时不卸载页面、不锁死技能管理", async () => {
    let authMeCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me")) {
          authMeCalls += 1;
          return jsonResponse(DEPT_ADMIN);
        }
        if (url.endsWith("/api/v1/admin/skills")) {
          return jsonResponse({ items: [] });
        }
        if (
          url.endsWith("/api/v1/admin/models") ||
          url.endsWith("/api/v1/admin/skills/executions/exec-1") ||
          url.endsWith("/api/v1/admin/skills/company-skill-1")
        ) {
          return jsonResponse({ detail: "admin_required" }, 403);
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    const app = renderApp("/admin/skills");
    await screen.findByRole("navigation", { name: "管理子导航" });
    app.client.setQueryData(adminKeys.skills(), []);
    app.client.setQueryData(adminKeys.modelProfiles(), {
      default_profile_id: null,
      items: [],
    });

    // 1. 触发超管专属接口与技能执行日志接口 403：不清空技能缓存、不阻断 /admin/skills。
    await act(async () => {
      await apiRequest("/admin/models").catch(() => undefined);
      await apiRequest("/admin/skills/executions/exec-1").catch(() => undefined);
    });
    expect(
      screen.queryByRole("heading", { name: "当前账号没有管理员权限" }),
    ).not.toBeInTheDocument();
    expect(app.client.getQueryData(adminKeys.skills())).toEqual([]);
    expect(app.client.getQueryData(adminKeys.modelProfiles())).toBeUndefined();

    // 2. 触发单个资源级 /admin/skills/{id} 403：静默复核 /auth/me，仍保留 /admin/skills 页面不卸载。
    const callsBeforeResourceForbidden = authMeCalls;
    await act(async () => {
      await apiRequest("/admin/skills/company-skill-1", {
        method: "PATCH",
        body: { enabled: true },
      }).catch(() => undefined);
    });
    await waitFor(() => {
      expect(authMeCalls).toBeGreaterThan(callsBeforeResourceForbidden);
    });
    expect(
      screen.queryByRole("heading", { name: "当前账号没有管理员权限" }),
    ).not.toBeInTheDocument();
    expect(app.getLocation().pathname).toBe("/admin/skills");
  });

  it("部门管理员资源级 403 静默复核发现已降权为员工时，被 SkillAdminRoute 正确拦截", async () => {
    let currentIdentity: User = DEPT_ADMIN;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me")) {
          return jsonResponse(currentIdentity);
        }
        if (url.endsWith("/api/v1/admin/skills")) {
          return jsonResponse({ items: [] });
        }
        if (url.endsWith("/api/v1/admin/skills/skill-99")) {
          return jsonResponse({ detail: "admin_required" }, 403);
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    renderApp("/admin/skills");
    await screen.findByRole("navigation", { name: "管理子导航" });
    currentIdentity = USER;
    await act(async () => {
      await apiRequest("/admin/skills/skill-99").catch(() => undefined);
    });
    expect(
      await screen.findByRole("heading", { name: "当前账号没有管理员权限" }),
    ).toBeInTheDocument();
  });

  it("部门管理员集合级 GET /admin/skills 返回 403 时立即 fail-closed 阻断并清空管理缓存", async () => {
    let authCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me")) {
          authCalls += 1;
          return jsonResponse(authCalls === 1 ? DEPT_ADMIN : USER);
        }
        if (url.endsWith("/api/v1/admin/skills")) {
          return jsonResponse({ detail: "admin_required" }, 403);
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    const app = renderApp("/admin/skills");
    expect(
      await screen.findByRole("heading", {
        name: "当前账号没有管理员权限",
      }),
    ).toBeInTheDocument();
    expect(authCalls).toBeGreaterThanOrEqual(2);
    expect(
      app.client.getQueryCache().findAll({ queryKey: adminKeys.all }),
    ).toHaveLength(0);
  });

  it("部门管理员触发超管拒止后切号登录超管，同步复位拒止标记且无首帧无权限闪屏", async () => {
    let identity: User | null = DEPT_ADMIN;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = getRequestUrl(input);
        if (url.endsWith("/auth/me")) {
          return identity
            ? jsonResponse(identity)
            : jsonResponse({ detail: "not_authenticated" }, 401);
        }
        if (url.endsWith("/auth/logout")) {
          identity = null;
          return new Response(null, { status: 204 });
        }
        if (url.endsWith("/auth/providers")) {
          return jsonResponse({
            feishu_enabled: false,
            local_login_enabled: false,
            mock_enabled: true,
          });
        }
        if (url.endsWith("/auth/mock/users")) {
          return jsonResponse({ items: [MOCK_USERS_FIXTURE[0]] });
        }
        if (url.endsWith("/auth/mock/login")) {
          identity = SUPER_ADMIN;
          return jsonResponse(SUPER_ADMIN);
        }
        if (url.endsWith("/api/v1/admin/skills")) {
          return jsonResponse({ items: [] });
        }
        if (url.endsWith("/api/v1/admin/models")) {
          return jsonResponse({ detail: "admin_required" }, 403);
        }
        if (url.endsWith("/admin/llm/model-profiles")) {
          return jsonResponse({ default_profile_id: null, items: [] });
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
    const app = renderApp("/admin/skills");
    await screen.findByRole("navigation", { name: "管理子导航" });

    // 先触发 superAdminAccessDenied = true。
    await act(async () => {
      await apiRequest("/admin/models").catch(() => undefined);
    });

    // 登出后用 Mock 登录切换为超级管理员，直接进入 /admin/models 不出现无权限页。
    await clickLogout(user);
    await user.click(await screen.findByRole("button", { name: /超级管理员/ }));
    await waitForStableChatWorkspace(app);
    await act(async () => {
      await app.navigateTo("/admin/models");
    });
    await waitForStableAdminModelsPage(app);
    expect(
      screen.queryByRole("heading", { name: "当前账号没有管理员权限" }),
    ).not.toBeInTheDocument();
  });
});
