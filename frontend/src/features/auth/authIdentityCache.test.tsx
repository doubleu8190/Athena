import {
  act,
  screen,
  waitFor,
} from "@testing-library/react";
import { onlineManager } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  getRequestUrl,
  listResponse,
  PROJECT_A,
  PROJECT_B,
  TEST_USER,
} from "../../test/fixtures";
import {
  createTestQueryClient,
  jsonResponse,
  renderApp,
} from "../../test/testUtils";
import { ADMIN_FORBIDDEN_EVENT } from "../../shared/api/client";
import { listProjects } from "../projects/api";
import type { Project } from "../projects/model";
import { projectKeys } from "../projects/queryKeys";
import {
  clearAuthenticatedIdentity,
  refreshAuthenticatedUser,
  setAuthenticatedIdentity,
} from "./cacheBoundary";
import { authKeys, type User } from "./model";

const USER_A: User = {
  ...TEST_USER,
  display_name: "用户 A",
  email: "a@example.com",
  id: "identity-user-a",
};
const USER_B: User = {
  ...TEST_USER,
  display_name: "用户 B",
  email: "b@example.com",
  id: "identity-user-b",
};

async function clickLogout(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "账户菜单" }));
  await user.click(screen.getByRole("menuitem", { name: "退出登录" }));
}
const PROJECT_FOR_A: Project = {
  ...PROJECT_A,
  id: "identity-project-a",
  name: "A 的私有项目",
};
const PROJECT_FOR_B: Project = {
  ...PROJECT_B,
  id: "identity-project-b",
  name: "B 的私有项目",
};

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

function deferredJsonResponse(body: unknown) {
  const encoder = new TextEncoder();
  let controller: ReadableStreamDefaultController<Uint8Array> | null = null;
  const stream = new ReadableStream<Uint8Array>({
    start(nextController) {
      controller = nextController;
    },
  });
  return {
    response: new Response(stream, {
      headers: { "Content-Type": "application/json" },
    }),
    complete() {
      if (!controller) {
        throw new Error("Response stream is not ready");
      }
      controller.enqueue(encoder.encode(JSON.stringify(body)));
      controller.close();
    },
  };
}

function emptySessions(url: URL): Response {
  return jsonResponse(
    listResponse(
      [],
      Number(url.searchParams.get("page") ?? 1),
      Number(url.searchParams.get("page_size") ?? 20),
      0,
    ),
  );
}

async function loginAsB(
  user: ReturnType<typeof userEvent.setup>,
) {
  await user.type(screen.getByLabelText("工作邮箱"), USER_B.email);
  await user.type(
    screen.getByLabelText("密码"),
    "correct-horse-battery",
  );
  await user.click(screen.getByRole("button", { name: "进入 Athena" }));
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

describe("认证身份缓存隔离", () => {
  it("显示名保存不取消权限复核，旧资料与PATCH响应都不能覆盖新的/me", async () => {
    const oldMeGate = deferred<Response>();
    let meRequests = 0;
    const updatedUser: User = {
      ...USER_A,
      display_name: "权限复核后的名字",
      role: "user",
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me") && init?.method === "PATCH") {
          // 模拟PATCH响应携带较早的角色，缓存必须以随后新的/me为准。
          return jsonResponse({ ...USER_A, display_name: "PATCH名字" });
        }
        if (url.pathname.endsWith("/auth/me")) {
          meRequests += 1;
          if (meRequests === 1) return jsonResponse(USER_A);
          if (meRequests === 2) return oldMeGate.promise;
          return jsonResponse(updatedUser);
        }
        if (url.pathname === "/api/v1/projects") {
          return jsonResponse(listResponse([], 1, 100, 0));
        }
        if (url.pathname === "/api/v1/sessions") {
          return jsonResponse(listResponse([], 1, 8, 0));
        }
        throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
      }),
    );
    const user = userEvent.setup();
    const app = renderApp("/profile");

    const displayName = await screen.findByLabelText(/显示名/);
    window.dispatchEvent(new CustomEvent(ADMIN_FORBIDDEN_EVENT));
    await waitFor(() => expect(meRequests).toBe(2));

    await user.clear(displayName);
    await user.type(displayName, "新显示名");
    await user.click(screen.getByRole("button", { name: "保存显示名" }));
    await waitFor(() => expect(meRequests).toBe(3));
    await screen.findByText("显示名已更新。");
    expect(
      app.client.getQueryData<User>(authKeys.currentUser),
    ).toMatchObject({ display_name: "权限复核后的名字", role: "user" });

    oldMeGate.resolve(jsonResponse(USER_A));
    await waitFor(() => {
      expect(app.client.getQueryData<User>(authKeys.currentUser)).toMatchObject({
        display_name: "权限复核后的名字",
        role: "user",
      });
    });
  });

  it("A 的显示名刷新失败迟到时不会污染已切换的 B", async () => {
    const refreshGate = deferred<Response>();
    let authMeCalls = 0;
    let refreshStarted = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me") && init?.method === "PATCH") {
          return jsonResponse({ ...USER_A, display_name: "A 已保存" });
        }
        if (url.pathname.endsWith("/auth/me")) {
          authMeCalls += 1;
          if (authMeCalls === 1) return jsonResponse(USER_A);
          refreshStarted = true;
          return refreshGate.promise;
        }
        if (url.pathname.endsWith("/auth/logout")) {
          return new Response(null, { status: 204 });
        }
        if (url.pathname.endsWith("/auth/login")) {
          return jsonResponse(USER_B);
        }
        if (
          url.pathname === "/api/v1/projects" &&
          (!init?.method || init.method === "GET")
        ) {
          return jsonResponse(listResponse([], 1, 100, 0));
        }
        if (
          url.pathname === "/api/v1/sessions" &&
          (!init?.method || init.method === "GET")
        ) {
          return emptySessions(url);
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      }),
    );
    const user = userEvent.setup();
    const app = renderApp("/profile");

    const displayName = await screen.findByLabelText(/显示名/);
    await user.clear(displayName);
    await user.type(displayName, "A 已保存");
    await user.click(screen.getByRole("button", { name: "保存显示名" }));
    await waitFor(() => expect(refreshStarted).toBe(true));

    await clickLogout(user);
    await waitForStableLoginScreen(app);
    await loginAsB(user);
    await act(async () => {
      app.navigateTo("/profile");
    });
    await screen.findByRole("heading", { name: "个人资料" });

    await act(async () => {
      refreshGate.resolve(jsonResponse({ detail: "http_error" }, 500));
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(
        app.client.getQueryData<User>(authKeys.currentUser),
      ).toEqual(USER_B);
    });
    expect(
      screen.getByDisplayValue(USER_B.display_name ?? ""),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(
        "显示名已保存，但登录资料刷新失败。请重试刷新。",
      ),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("暂时无法确认登录状态"),
    ).not.toBeInTheDocument();
  });

  it("显示名刷新失败不会解除管理员权限复核拒止", async () => {
    const adminUser: User = { ...USER_A, role: "admin" };
    let authMeCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me") && init?.method === "PATCH") {
          return jsonResponse({ ...adminUser, display_name: "已写入" });
        }
        if (url.pathname.endsWith("/auth/me")) {
          authMeCalls += 1;
          return authMeCalls === 1
            ? jsonResponse(adminUser)
            : jsonResponse({ detail: "http_error" }, 500);
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      }),
    );
    const user = userEvent.setup();
    const app = renderApp("/profile");

    const displayName = await screen.findByLabelText(/显示名/);
    window.dispatchEvent(new CustomEvent(ADMIN_FORBIDDEN_EVENT));
    await waitFor(() => expect(authMeCalls).toBe(2));

    await user.clear(displayName);
    await user.type(displayName, "已写入");
    await user.click(screen.getByRole("button", { name: "保存显示名" }));
    expect(
      await screen.findByText(
        "显示名已保存，但登录资料刷新失败。请重试刷新。",
      ),
    ).toBeInTheDocument();

    await act(async () => {
      app.navigateTo("/admin/models");
    });
    expect(
      await screen.findByRole("heading", {
        name: "当前账号没有管理员权限",
      }),
    ).toBeInTheDocument();
  });

  it("A 的显示名刷新成功后到达也不能覆盖已切换的 B", async () => {
    const refreshGate = deferred<Response>();
    let authMeCalls = 0;
    let refreshStarted = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me") && init?.method === "PATCH") {
          return jsonResponse({ ...USER_A, display_name: "A 已保存" });
        }
        if (url.pathname.endsWith("/auth/me")) {
          authMeCalls += 1;
          if (authMeCalls === 1) return jsonResponse(USER_A);
          refreshStarted = true;
          return refreshGate.promise;
        }
        if (url.pathname.endsWith("/auth/logout")) {
          return new Response(null, { status: 204 });
        }
        if (url.pathname.endsWith("/auth/login")) {
          return jsonResponse(USER_B);
        }
        if (url.pathname === "/api/v1/projects") {
          return jsonResponse(listResponse([], 1, 100, 0));
        }
        if (url.pathname === "/api/v1/sessions") {
          return emptySessions(url);
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      }),
    );
    const user = userEvent.setup();
    const app = renderApp("/profile");

    const displayName = await screen.findByLabelText(/显示名/);
    await user.clear(displayName);
    await user.type(displayName, "A 已保存");
    await user.click(screen.getByRole("button", { name: "保存显示名" }));
    await waitFor(() => expect(refreshStarted).toBe(true));

    await clickLogout(user);
    await waitForStableLoginScreen(app);
    await loginAsB(user);
    await act(async () => {
      app.navigateTo("/profile");
    });
    await screen.findByRole("heading", { name: "个人资料" });

    await act(async () => {
      refreshGate.resolve(jsonResponse(USER_A));
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(
        app.client.getQueryData<User>(authKeys.currentUser),
      ).toEqual(USER_B);
    });
    expect(
      screen.getByDisplayValue(USER_B.display_name ?? ""),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(
        "显示名已保存，但登录资料刷新失败。请重试刷新。",
      ),
    ).not.toBeInTheDocument();
  });

  it("离线暂停的 A 刷新在切换 B 后恢复联网也不会回写", async () => {
    let authMeCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me") && init?.method === "PATCH") {
          onlineManager.setOnline(false);
          return jsonResponse({ ...USER_A, display_name: "A 已保存" });
        }
        if (url.pathname.endsWith("/auth/me")) {
          authMeCalls += 1;
          return jsonResponse(USER_A);
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      }),
    );
    const user = userEvent.setup();
    const app = renderApp("/profile");

    try {
      const displayName = await screen.findByLabelText(/显示名/);
      await user.clear(displayName);
      await user.type(displayName, "A 已保存");
      await user.click(screen.getByRole("button", { name: "保存显示名" }));
      await screen.findByText(
        "显示名已保存，但登录资料刷新失败。请重试刷新。",
      );
      expect(
        app.client.getQueryState(authKeys.currentUser)?.fetchStatus,
      ).toBe("paused");

      act(() => {
        clearAuthenticatedIdentity(app.client);
        setAuthenticatedIdentity(app.client, USER_B);
      });
      const callsBeforeOnline = authMeCalls;

      await act(async () => {
        onlineManager.setOnline(true);
        await Promise.resolve();
      });

      expect(authMeCalls).toBe(callsBeforeOnline);
      expect(
        app.client.getQueryData<User>(authKeys.currentUser),
      ).toEqual(USER_B);
    } finally {
      await act(async () => {
        onlineManager.setOnline(true);
      });
    }
  });

  it("取消刷新返回 cancelled 且保留当前身份", async () => {
    const refreshGate = deferred<Response>();
    let authMeCalls = 0;
    let refreshStarted = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          authMeCalls += 1;
          if (authMeCalls === 1) return jsonResponse(USER_A);
          refreshStarted = true;
          return refreshGate.promise;
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );
    const app = renderApp("/profile");
    await screen.findByLabelText(/显示名/);

    const refresh = refreshAuthenticatedUser(app.client);
    await waitFor(() => expect(refreshStarted).toBe(true));
    await app.client.cancelQueries(
      { exact: true, queryKey: authKeys.currentUser },
      { revert: false },
    );

    await expect(refresh).resolves.toMatchObject({
      reason: "cancelled",
      status: "failed",
    });
    expect(
      app.client.getQueryData<User>(authKeys.currentUser),
    ).toEqual(USER_A);

    await act(async () => {
      refreshGate.resolve(jsonResponse(USER_A));
      await Promise.resolve();
    });
  });

  it("离线暂停的刷新返回 paused 并保留当前身份", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(USER_A);
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );
    const app = renderApp("/profile");
    await screen.findByLabelText(/显示名/);
    onlineManager.setOnline(false);

    try {
      await expect(
        refreshAuthenticatedUser(app.client),
      ).resolves.toMatchObject({
        reason: "paused",
        status: "failed",
      });
      expect(
        app.client.getQueryData<User>(authKeys.currentUser),
      ).toEqual(USER_A);
    } finally {
      await act(async () => {
        onlineManager.setOnline(true);
      });
    }
  });

  it("替换刷新时旧调用返回 superseded，新调用成功才返回 refreshed", async () => {
    const firstGate = deferred<Response>();
    const secondGate = deferred<Response>();
    const updatedUser: User = {
      ...USER_A,
      display_name: "替换后成功",
    };
    let authMeCalls = 0;
    let firstStarted = false;
    let secondStarted = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          authMeCalls += 1;
          if (authMeCalls === 1) return jsonResponse(USER_A);
          if (authMeCalls === 2) {
            firstStarted = true;
            return firstGate.promise;
          }
          secondStarted = true;
          return secondGate.promise;
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );
    const app = renderApp("/profile");
    await screen.findByLabelText(/显示名/);

    const firstRefresh = refreshAuthenticatedUser(app.client);
    await waitFor(() => expect(firstStarted).toBe(true));
    const secondRefresh = refreshAuthenticatedUser(app.client);
    await waitFor(() => expect(secondStarted).toBe(true));

    await act(async () => {
      secondGate.resolve(jsonResponse(updatedUser));
    });
    const [firstResult, secondResult] = await Promise.all([
      firstRefresh,
      secondRefresh,
    ]);

    expect(firstResult).toMatchObject({
      reason: "superseded",
      status: "failed",
    });
    expect(secondResult).toEqual({ status: "refreshed" });
    expect(
      app.client.getQueryData<User>(authKeys.currentUser),
    ).toEqual(updatedUser);

    await act(async () => {
      firstGate.resolve(jsonResponse(USER_A));
      await Promise.resolve();
    });
  });

  it("没有实际用户查询成功结果时刷新返回 unavailable", async () => {
    const result = await refreshAuthenticatedUser(
      createTestQueryClient(),
    );

    expect(result).toMatchObject({
      reason: "unavailable",
      status: "failed",
    });
  });

  it("A 退出后 B 的项目请求未完成时不会显示 A 的项目", async () => {
    let identity: User | null = USER_A;
    const bProjectsGate = deferred<void>();
    let bProjectsStarted = false;
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return identity
            ? jsonResponse(identity)
            : jsonResponse({ detail: "not_authenticated" }, 401);
        }
        if (url.pathname.endsWith("/auth/logout")) {
          identity = null;
          return new Response(null, { status: 204 });
        }
        if (url.pathname.endsWith("/auth/login")) {
          identity = USER_B;
          return jsonResponse(USER_B);
        }
        if (
          url.pathname === "/api/v1/projects" &&
          (!init?.method || init.method === "GET")
        ) {
          if (identity?.id === USER_A.id) {
            return jsonResponse(
              listResponse([PROJECT_FOR_A], 1, 100, 1),
            );
          }
          if (identity?.id === USER_B.id) {
            bProjectsStarted = true;
            await bProjectsGate.promise;
            return jsonResponse(
              listResponse([PROJECT_FOR_B], 1, 100, 1),
            );
          }
          return jsonResponse({ detail: "not_authenticated" }, 401);
        }
        if (
          url.pathname === "/api/v1/sessions" &&
          (!init?.method || init.method === "GET")
        ) {
          return emptySessions(url);
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    const app = renderApp("/projects");

    expect(
      await screen.findAllByText(PROJECT_FOR_A.name),
    ).not.toHaveLength(0);
    await clickLogout(user);
    await waitForStableLoginScreen(app);

    await loginAsB(user);
    await user.click(await screen.findByRole("button", { name: "账户菜单" }));
    expect(
      await screen.findAllByText(USER_B.display_name ?? USER_B.email),
    ).not.toHaveLength(0);
    await waitFor(() => {
      expect(bProjectsStarted).toBe(true);
    });

    expect(
      screen.queryByText(PROJECT_FOR_A.name),
    ).not.toBeInTheDocument();
    expect(
      app.client.getQueryData(projectKeys.directory()),
    ).toBeUndefined();
    expect(
      screen.getAllByText("正在加载项目…").length,
    ).toBeGreaterThan(0);

    bProjectsGate.resolve();
    expect(
      await screen.findAllByText(PROJECT_FOR_B.name),
    ).not.toHaveLength(0);
    expect(
      screen.queryByText(PROJECT_FOR_A.name),
    ).not.toBeInTheDocument();
    expect(
      app.client.getQueryData<Project[]>(projectKeys.directory()),
    ).toEqual([PROJECT_FOR_B]);
  });

  it("A 的慢查询在 B 登录后返回时不能覆盖 B 的缓存", async () => {
    let identity: User | null = USER_A;
    const aProjectsGate = deferred<Response>();
    let aProjectsStarted = false;
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(identity ?? { detail: "not_authenticated" });
        }
        if (url.pathname.endsWith("/auth/logout")) {
          identity = null;
          return new Response(null, { status: 204 });
        }
        if (url.pathname.endsWith("/auth/login")) {
          identity = USER_B;
          return jsonResponse(USER_B);
        }
        if (
          url.pathname === "/api/v1/projects" &&
          (!init?.method || init.method === "GET")
        ) {
          if (identity?.id === USER_A.id) {
            aProjectsStarted = true;
            return aProjectsGate.promise;
          }
          return jsonResponse(
            listResponse([PROJECT_FOR_B], 1, 100, 1),
          );
        }
        if (
          url.pathname === "/api/v1/sessions" &&
          (!init?.method || init.method === "GET")
        ) {
          return emptySessions(url);
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const app = renderApp("/chat");
    const user = userEvent.setup();

    await waitFor(() => {
      expect(aProjectsStarted).toBe(true);
    });
    await clickLogout(user);
    await waitForStableLoginScreen(app);
    await loginAsB(user);
    expect(
      await screen.findAllByText(PROJECT_FOR_B.name),
    ).not.toHaveLength(0);

    await act(async () => {
      aProjectsGate.resolve(
        jsonResponse(listResponse([PROJECT_FOR_A], 1, 100, 1)),
      );
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(
        app.client.getQueryData<Project[]>(
          projectKeys.directory(),
        ),
      ).toEqual([PROJECT_FOR_B]);
    });
    expect(
      screen.getAllByText(PROJECT_FOR_B.name),
    ).not.toHaveLength(0);
    expect(
      screen.queryByText(PROJECT_FOR_A.name),
    ).not.toBeInTheDocument();
    expect(
      app.client.getQueryData<User>(authKeys.currentUser),
    ).toEqual(USER_B);
  });

  it("旧身份的 auth/me 迟到不能把 B 的认证状态改成错误", async () => {
    let identity: User | null = USER_A;
    const staleAuthGate = deferred<Response>();
    let authMeCalls = 0;
    let staleAuthStarted = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = requestUrl(input);
          if (url.pathname.endsWith("/auth/me")) {
            authMeCalls += 1;
            if (authMeCalls === 1) {
              return jsonResponse(USER_A);
            }
            staleAuthStarted = true;
            return staleAuthGate.promise;
          }
          if (url.pathname.endsWith("/auth/logout")) {
            identity = null;
            return new Response(null, { status: 204 });
          }
          if (url.pathname.endsWith("/auth/login")) {
            identity = USER_B;
            return jsonResponse(USER_B);
          }
          if (
            url.pathname === "/api/v1/projects" &&
            (!init?.method || init.method === "GET")
          ) {
            return jsonResponse(
              listResponse(
                [identity?.id === USER_B.id ? PROJECT_FOR_B : PROJECT_FOR_A],
                1,
                100,
                1,
              ),
            );
          }
          if (
            url.pathname === "/api/v1/sessions" &&
            (!init?.method || init.method === "GET")
          ) {
            return emptySessions(url);
          }
          throw new Error(
            `Unexpected request: ${init?.method ?? "GET"} ${url}`,
          );
        },
      ),
    );
    const app = renderApp("/chat");
    const user = userEvent.setup();

    await screen.findByRole("heading", { name: "对话工作台" });
    await act(async () => {
      void app.client
        .refetchQueries({
          exact: true,
          queryKey: authKeys.currentUser,
        })
        .catch(() => undefined);
      await Promise.resolve();
    });
    await waitFor(() => {
      expect(staleAuthStarted).toBe(true);
    });

    await clickLogout(user);
    await waitForStableLoginScreen(app);
    await loginAsB(user);
    expect(
      await screen.findAllByText(PROJECT_FOR_B.name),
    ).not.toHaveLength(0);

    await act(async () => {
      staleAuthGate.resolve(jsonResponse(USER_A));
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(
        app.client.getQueryData<User>(authKeys.currentUser),
      ).toEqual(USER_B);
    });
    expect(
      screen.getByRole("heading", { name: "对话工作台" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("暂时无法确认登录状态"),
    ).not.toBeInTheDocument();
  });

  it("A 的响应体在 B 登录后才完成时不能写回项目缓存", async () => {
    let identity: User | null = USER_A;
    let serveDelayedA = false;
    const delayedA = deferredJsonResponse(
      listResponse([PROJECT_FOR_A], 1, 100, 1),
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = requestUrl(input);
          if (url.pathname.endsWith("/auth/me")) {
            return identity
              ? jsonResponse(identity)
              : jsonResponse({ detail: "not_authenticated" }, 401);
          }
          if (url.pathname.endsWith("/auth/logout")) {
            identity = null;
            return new Response(null, { status: 204 });
          }
          if (url.pathname.endsWith("/auth/login")) {
            identity = USER_B;
            return jsonResponse(USER_B);
          }
          if (
            url.pathname === "/api/v1/projects" &&
            (!init?.method || init.method === "GET")
          ) {
            if (identity?.id === USER_A.id && serveDelayedA) {
              return delayedA.response;
            }
            return jsonResponse(
              listResponse(
                [identity?.id === USER_B.id ? PROJECT_FOR_B : PROJECT_FOR_A],
                1,
                100,
                1,
              ),
            );
          }
          if (
            url.pathname === "/api/v1/sessions" &&
            (!init?.method || init.method === "GET")
          ) {
            return emptySessions(url);
          }
          throw new Error(
            `Unexpected request: ${init?.method ?? "GET"} ${url}`,
          );
        },
      ),
    );
    const app = renderApp("/projects");
    const user = userEvent.setup();

    expect(
      await screen.findAllByText(PROJECT_FOR_A.name),
    ).not.toHaveLength(0);
    serveDelayedA = true;
    act(() => {
      void app.client
        .refetchQueries({
          exact: true,
          queryKey: projectKeys.directory(),
        })
        .catch(() => undefined);
    });
    await waitFor(() => {
      expect(delayedA.response.bodyUsed).toBe(true);
    });

    await clickLogout(user);
    await waitForStableLoginScreen(app);
    await loginAsB(user);
    expect(
      await screen.findAllByText(PROJECT_FOR_B.name),
    ).not.toHaveLength(0);

    await act(async () => {
      delayedA.complete();
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(
        app.client.getQueryData<Project[]>(
          projectKeys.directory(),
        ),
      ).toEqual([PROJECT_FOR_B]);
    });
    expect(
      screen.queryByText(PROJECT_FOR_A.name),
    ).not.toBeInTheDocument();
  });

  it("旧身份迟到 401 不会退出已经登录的 B", async () => {
    let identity: User | null = USER_A;
    const aUnauthorizedGate = deferred<Response>();
    let aProjectsStarted = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = requestUrl(input);
          if (url.pathname.endsWith("/auth/me")) {
            return jsonResponse(identity ?? { detail: "not_authenticated" });
          }
          if (url.pathname.endsWith("/auth/logout")) {
            identity = null;
            return new Response(null, { status: 204 });
          }
          if (url.pathname.endsWith("/auth/login")) {
            identity = USER_B;
            return jsonResponse(USER_B);
          }
          if (
            url.pathname === "/api/v1/projects" &&
            (!init?.method || init.method === "GET")
          ) {
            if (identity?.id === USER_A.id) {
              aProjectsStarted = true;
              return aUnauthorizedGate.promise;
            }
            return jsonResponse(
              listResponse([PROJECT_FOR_B], 1, 100, 1),
            );
          }
          if (
            url.pathname === "/api/v1/sessions" &&
            (!init?.method || init.method === "GET")
          ) {
            return emptySessions(url);
          }
          throw new Error(
            `Unexpected request: ${init?.method ?? "GET"} ${url}`,
          );
        },
      ),
    );
    const app = renderApp("/chat");
    const user = userEvent.setup();

    await waitFor(() => {
      expect(aProjectsStarted).toBe(true);
    });
    await clickLogout(user);
    await waitForStableLoginScreen(app);
    await loginAsB(user);
    expect(
      await screen.findAllByText(PROJECT_FOR_B.name),
    ).not.toHaveLength(0);

    await act(async () => {
      aUnauthorizedGate.resolve(
        jsonResponse({ detail: "not_authenticated" }, 401),
      );
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(
        app.client.getQueryData<User>(authKeys.currentUser),
      ).toEqual(USER_B);
    });
    expect(
      screen.getByRole("heading", { name: "对话工作台" }),
    ).toBeInTheDocument();
    expect(
      screen.getAllByText(PROJECT_FOR_B.name),
    ).not.toHaveLength(0);
    expect(
      screen.queryByRole("heading", { name: "欢迎回来" }),
    ).not.toBeInTheDocument();
  });

  it("当前身份收到 401 时清空业务缓存并跳转登录", async () => {
    let unauthorized = false;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = requestUrl(input);
          if (url.pathname.endsWith("/auth/me")) {
            return jsonResponse(USER_A);
          }
          if (
            url.pathname === "/api/v1/projects" &&
            (!init?.method || init.method === "GET")
          ) {
            return unauthorized
              ? jsonResponse({ detail: "not_authenticated" }, 401)
              : jsonResponse(
                  listResponse([PROJECT_FOR_A], 1, 100, 1),
                );
          }
          throw new Error(
            `Unexpected request: ${init?.method ?? "GET"} ${url}`,
          );
        },
      ),
    );
    const app = renderApp("/projects");

    expect(
      await screen.findAllByText(PROJECT_FOR_A.name),
    ).not.toHaveLength(0);
    app.client.setQueryData(["knowledge", "directory"], [
      { id: "stale-knowledge", name: "旧知识库" },
    ]);
    app.client.setQueryData(["admin", "llm", "model-profiles"], {
      default_profile_id: null,
      items: [],
    });
    unauthorized = true;

    await act(async () => {
      await listProjects().catch(() => undefined);
    });

    await waitForStableLoginScreen(app);
    expect(
      app.client
        .getQueryCache()
        .findAll({
          predicate: (query) => query.queryKey[0] !== "auth",
        }),
    ).toHaveLength(0);
    expect(
      app.client.getQueryData(projectKeys.directory()),
    ).toBeUndefined();
    expect(
      app.client.getQueryData<User | null>(authKeys.currentUser),
    ).toBeNull();
  });
});
