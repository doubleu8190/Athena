import {
  act,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  getRequestUrl,
  listResponse,
  PROJECT_A,
  PROJECT_B,
  TEST_USER,
} from "../../test/fixtures";
import { jsonResponse, renderApp } from "../../test/testUtils";
import { listProjects } from "../projects/api";
import type { Project } from "../projects/model";
import { projectKeys } from "../projects/queryKeys";
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
    await user.click(
      screen.getAllByRole("button", { name: "退出登录" })[0],
    );
    await waitForStableLoginScreen(app);

    await loginAsB(user);
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
    await user.click(
      screen.getAllByRole("button", { name: "退出登录" })[0],
    );
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

    await user.click(
      screen.getAllByRole("button", { name: "退出登录" })[0],
    );
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

    await user.click(
      screen.getAllByRole("button", { name: "退出登录" })[0],
    );
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
    await user.click(
      screen.getAllByRole("button", { name: "退出登录" })[0],
    );
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
