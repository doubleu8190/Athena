import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  getRequestUrl,
  listResponse,
  makeSession,
  PROJECT_A,
  TEST_USER,
} from "../../test/fixtures";
import { jsonResponse, renderApp } from "../../test/testUtils";

interface SessionPageFetchOptions {
  initialSessions: ReturnType<typeof makeSession>[];
  deleteStatus?: number;
  deleteDetail?: string;
}

function installSessionPageFetch(options: SessionPageFetchOptions) {
  let renamed = false;
  let deleted = false;
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
        return jsonResponse(PROJECT_A);
      }
      if (url.pathname === "/api/v1/sessions") {
        const pageSize = Number(url.searchParams.get("page_size"));
        if (pageSize !== 10) {
          return jsonResponse(listResponse([]));
        }
        if (deleted) {
          return jsonResponse(listResponse([], 1, 10, 0));
        }
        const items = renamed
          ? options.initialSessions.map((session) => ({
              ...session,
              title: session.id === "session-1" ? "新的会话标题" : session.title,
            }))
          : options.initialSessions;
        return jsonResponse(listResponse(items, 1, 10, items.length));
      }
      if (url.pathname === "/api/v1/sessions/session-1") {
        if (init?.method === "PATCH") {
          renamed = true;
          const body = JSON.parse(String(init.body)) as { title: string };
          return jsonResponse({
            ...options.initialSessions[0],
            title: body.title,
          });
        }
        if (init?.method === "DELETE") {
          if (options.deleteStatus && options.deleteStatus >= 400) {
            return jsonResponse(
              { detail: options.deleteDetail ?? "http_error" },
              options.deleteStatus,
            );
          }
          deleted = true;
          return new Response(null, { status: 204 });
        }
        return jsonResponse(options.initialSessions[0]);
      }
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
    },
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("项目会话管理", () => {
  it("空标题显示未命名对话，重命名成功后刷新列表", async () => {
    const fetchMock = installSessionPageFetch({
      initialSessions: [
        makeSession({ id: "session-1", title: null }),
      ],
    });
    const user = userEvent.setup();

    renderApp(`/projects/${PROJECT_A.id}/sessions`);

    expect(await screen.findByText("未命名对话")).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", {
        name: "重命名会话：未命名对话",
      }),
    );
    const dialog = screen.getByRole("dialog", { name: "重命名会话" });
    const input = within(dialog).getByLabelText("会话标题");
    expect(input).toHaveValue("");
    await user.type(input, "新的会话标题");
    await user.click(
      within(dialog).getByRole("button", { name: "保存标题" }),
    );

    expect(await screen.findByText("新的会话标题")).toBeInTheDocument();
    const patchCall = fetchMock.mock.calls.find(
      ([request, init]) =>
        new URL(getRequestUrl(request), "http://localhost").pathname ===
          "/api/v1/sessions/session-1" && init?.method === "PATCH",
    );
    expect(JSON.parse(String(patchCall?.[1]?.body))).toEqual({
      title: "新的会话标题",
    });
    expect(
      screen.queryByRole("dialog", { name: "重命名会话" }),
    ).not.toBeInTheDocument();
  });

  it("删除 Session 成功并刷新为真实空态", async () => {
    installSessionPageFetch({
      initialSessions: [
        makeSession({ id: "session-1", title: "待删除会话" }),
      ],
    });
    const user = userEvent.setup();

    renderApp(`/projects/${PROJECT_A.id}/sessions`);

    await screen.findByText("待删除会话");
    await user.click(
      screen.getByRole("button", {
        name: "删除会话：待删除会话",
      }),
    );
    const dialog = screen.getByRole("dialog", { name: "删除会话" });
    await user.click(
      within(dialog).getByRole("button", { name: "确认删除" }),
    );

    expect(
      await screen.findByRole("heading", {
        name: "还没有项目会话",
      }),
    ).toBeInTheDocument();
  });

  it("活跃 Run 的 409 错误保留删除弹窗并显示原因", async () => {
    installSessionPageFetch({
      deleteDetail: "session_has_active_run",
      deleteStatus: 409,
      initialSessions: [
        makeSession({ id: "session-1", title: "运行中会话" }),
      ],
    });
    const user = userEvent.setup();

    renderApp(`/projects/${PROJECT_A.id}/sessions`);

    await screen.findByText("运行中会话");
    await user.click(
      screen.getByRole("button", {
        name: "删除会话：运行中会话",
      }),
    );
    const dialog = screen.getByRole("dialog", { name: "删除会话" });
    await user.click(
      within(dialog).getByRole("button", { name: "确认删除" }),
    );

    expect(
      await within(dialog).findByText(
        "当前对话有正在执行的任务，请等待完成或停止后再操作。",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("dialog", { name: "删除会话" }),
    ).toBeInTheDocument();
  });

  it("分页参数写入 URL 并可恢复对应页面", async () => {
    const fetchMock = installSessionPageFetch({
      initialSessions: [
        makeSession({ id: "session-page-2", title: "第二页会话" }),
      ],
    });
    fetchMock.mockImplementation(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/projects") {
          return jsonResponse(listResponse([PROJECT_A], 1, 100, 1));
        }
        if (url.pathname === `/api/v1/projects/${PROJECT_A.id}`) {
          return jsonResponse(PROJECT_A);
        }
        if (url.pathname === "/api/v1/sessions") {
          const pageSize = Number(url.searchParams.get("page_size"));
          if (pageSize !== 10) {
            return jsonResponse(listResponse([]));
          }
          const page = Number(url.searchParams.get("page") ?? 1);
          return jsonResponse(
            listResponse(
              page === 2
                ? [
                    makeSession({
                      id: "session-page-2",
                      title: "第二页会话",
                    }),
                  ]
                : [],
              page,
              10,
              11,
            ),
          );
        }
        throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
      },
    );

    renderApp(`/projects/${PROJECT_A.id}/sessions?page=2`);

    expect(await screen.findByText("第二页会话")).toBeInTheDocument();
    expect(screen.getByText("第 2 / 2 页")).toBeInTheDocument();
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([request]) => {
          const url = new URL(
            getRequestUrl(request),
            "http://localhost",
          );
          return (
            url.pathname === "/api/v1/sessions" &&
            url.searchParams.get("page") === "2"
          );
        }),
      ).toBe(true);
    });
  });
});

