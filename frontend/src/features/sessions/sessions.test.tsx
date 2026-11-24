import { screen, waitFor, within } from "@testing-library/react";
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

interface SessionPageFetchOptions {
  deleteDetail?: string;
  deleteStatus?: number;
  initialSessions: ReturnType<typeof makeSession>[];
}

function installSessionPageFetch(options: SessionPageFetchOptions) {
  let sessions = [...options.initialSessions];
  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(getRequestUrl(input), "http://localhost");
      const method = init?.method ?? "GET";

      if (url.pathname.endsWith("/auth/me")) {
        return jsonResponse(TEST_USER);
      }
      if (url.pathname === "/api/v1/projects") {
        return jsonResponse(listResponse([PROJECT_A, PROJECT_B], 1, 100, 2));
      }
      if (url.pathname === `/api/v1/projects/${PROJECT_A.id}`) {
        return jsonResponse(PROJECT_A);
      }
      if (url.pathname === `/api/v1/projects/${PROJECT_A.id}/knowledge-bases`) {
        return jsonResponse([]);
      }
      if (url.pathname === "/api/v1/skills") {
        return jsonResponse({ items: [] });
      }
      if (url.pathname === "/api/v1/sessions" && method === "GET") {
        const pageSize = Number(url.searchParams.get("page_size") ?? 10);
        const projectId = url.searchParams.get("project_id");
        if (projectId !== PROJECT_A.id) {
          return jsonResponse(listResponse([], 1, pageSize, 0));
        }
        const q = url.searchParams.get("q")?.trim().toLowerCase() ?? "";
        const matched = sessions
          .filter((item) => item.project_id === PROJECT_A.id)
          .filter((item) =>
            q ? (item.title ?? "").toLowerCase().includes(q) : true,
          );
        return jsonResponse(listResponse(matched, 1, pageSize, matched.length));
      }
      const sessionMatch = /^\/api\/v1\/sessions\/([^/]+)$/.exec(url.pathname);
      if (sessionMatch) {
        const sessionId = decodeURIComponent(sessionMatch[1]);
        const target = sessions.find((item) => item.id === sessionId);
        if (method === "PATCH" && target) {
          const body = JSON.parse(String(init?.body)) as {
            project_id?: string | null;
            title?: string | null;
          };
          const updated = {
            ...target,
            ...(Object.hasOwn(body, "title") ? { title: body.title ?? null } : {}),
            ...(Object.hasOwn(body, "project_id")
              ? { project_id: body.project_id ?? null }
              : {}),
          };
          sessions = sessions.map((item) =>
            item.id === sessionId ? updated : item,
          );
          return jsonResponse(updated);
        }
        if (method === "DELETE") {
          if (options.deleteStatus && options.deleteStatus >= 400) {
            return jsonResponse(
              { detail: options.deleteDetail ?? "http_error" },
              options.deleteStatus,
            );
          }
          sessions = sessions.filter((item) => item.id !== sessionId);
          return new Response(null, { status: 204 });
        }
        if (target) {
          return jsonResponse(target);
        }
      }
      throw new Error(`Unexpected request: ${method} ${url}`);
    },
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("项目会话管理（单页工作文件夹左栏）", () => {
  it("空标题显示未命名对话，支持操作菜单重命名与移出项目并刷新列表", async () => {
    const fetchMock = installSessionPageFetch({
      initialSessions: [
        makeSession({ id: "session-1", project_id: PROJECT_A.id, title: null }),
      ],
    });
    const user = userEvent.setup();

    renderApp(`/projects/${PROJECT_A.id}/sessions`);

    expect(await screen.findByText("未命名对话")).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "更多对话操作：未命名对话" }),
    );
    await user.click(screen.getByRole("menuitem", { name: "重命名" }));

    const dialog = screen.getByRole("dialog", { name: "重命名对话" });
    const input = within(dialog).getByLabelText("对话标题");
    expect(input).toHaveValue("");
    await user.type(input, "新的会话标题");
    await user.click(within(dialog).getByRole("button", { name: "保存标题" }));

    expect(await screen.findByText("新的会话标题")).toBeInTheDocument();
    const renamePatchCall = fetchMock.mock.calls.find(
      ([request, init]) =>
        new URL(getRequestUrl(request), "http://localhost").pathname ===
          "/api/v1/sessions/session-1" && init?.method === "PATCH",
    );
    expect(JSON.parse(String(renamePatchCall?.[1]?.body))).toEqual({
      title: "新的会话标题",
    });

    // 通过「移动 / 移出项目」将该会话移出当前项目。
    await user.click(
      screen.getByRole("button", { name: "更多对话操作：新的会话标题" }),
    );
    await user.click(screen.getByRole("menuitem", { name: "移动 / 移出项目" }));
    const moveDialog = screen.getByRole("dialog", { name: "调整所属项目" });
    await user.click(
      within(moveDialog).getByRole("combobox", { name: "目标项目" }),
    );
    await user.click(screen.getByRole("option", { name: "未归属项目" }));
    await user.click(
      within(moveDialog).getByRole("button", { name: "保存归属" }),
    );

    expect(
      await screen.findByRole("heading", { name: "还没有项目对话" }),
    ).toBeInTheDocument();
  });

  it("支持按标题防抖搜索项目内对话（q 参数）与清空恢复", async () => {
    const fetchMock = installSessionPageFetch({
      initialSessions: [
        makeSession({
          id: "session-1",
          project_id: PROJECT_A.id,
          title: "年度财务规划",
        }),
        makeSession({
          id: "session-2",
          project_id: PROJECT_A.id,
          title: "研发架构评审",
        }),
      ],
    });
    const user = userEvent.setup();

    renderApp(`/projects/${PROJECT_A.id}`);

    expect(await screen.findByText("年度财务规划")).toBeInTheDocument();
    expect(screen.getByText("研发架构评审")).toBeInTheDocument();

    const searchInput = screen.getByLabelText("搜索项目内对话");
    await user.type(searchInput, "财务");

    await waitFor(
      () => {
        expect(
          fetchMock.mock.calls.some(([input]) => {
            const url = new URL(getRequestUrl(input), "http://localhost");
            return (
              url.pathname === "/api/v1/sessions" &&
              url.searchParams.get("project_id") === PROJECT_A.id &&
              url.searchParams.get("q") === "财务"
            );
          }),
        ).toBe(true);
      },
      { timeout: 1500 },
    );

    expect(await screen.findByText("年度财务规划")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByText("研发架构评审")).not.toBeInTheDocument();
    });

    await user.click(screen.getByRole("button", { name: "清空搜索" }));
    expect(await screen.findByText("研发架构评审")).toBeInTheDocument();
  });

  it("删除 Session 成功并刷新为真实空态", async () => {
    installSessionPageFetch({
      initialSessions: [
        makeSession({
          id: "session-1",
          project_id: PROJECT_A.id,
          title: "待删除会话",
        }),
      ],
    });
    const user = userEvent.setup();

    renderApp(`/projects/${PROJECT_A.id}`);

    await screen.findByText("待删除会话");
    await user.click(
      screen.getByRole("button", { name: "更多对话操作：待删除会话" }),
    );
    await user.click(screen.getByRole("menuitem", { name: "删除对话" }));

    const alertDialog = screen.getByRole("alertdialog", { name: "删除对话" });
    await user.click(
      within(alertDialog).getByRole("button", { name: "确认删除" }),
    );

    expect(
      await screen.findByRole("heading", { name: "还没有项目对话" }),
    ).toBeInTheDocument();
  });

  it("活跃 Run 的 409 错误保留删除弹窗并显示原因", async () => {
    installSessionPageFetch({
      deleteDetail: "session_has_active_run",
      deleteStatus: 409,
      initialSessions: [
        makeSession({
          id: "session-1",
          project_id: PROJECT_A.id,
          title: "运行中会话",
        }),
      ],
    });
    const user = userEvent.setup();

    renderApp(`/projects/${PROJECT_A.id}`);

    await screen.findByText("运行中会话");
    await user.click(
      screen.getByRole("button", { name: "更多对话操作：运行中会话" }),
    );
    await user.click(screen.getByRole("menuitem", { name: "删除对话" }));

    const alertDialog = screen.getByRole("alertdialog", { name: "删除对话" });
    await user.click(
      within(alertDialog).getByRole("button", { name: "确认删除" }),
    );

    expect(
      await within(alertDialog).findByText(
        "当前对话有正在执行的任务，请等待完成或停止后再操作。",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("alertdialog", { name: "删除对话" }),
    ).toBeInTheDocument();
  });

  it("分页参数通过旧路由重定向后仍写入 URL 并可恢复对应页面", async () => {
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
        if (
          url.pathname === `/api/v1/projects/${PROJECT_A.id}/knowledge-bases`
        ) {
          return jsonResponse([]);
        }
        if (url.pathname === "/api/v1/skills") {
          return jsonResponse({ items: [] });
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
    vi.stubGlobal("fetch", fetchMock);

    renderApp(`/projects/${PROJECT_A.id}/sessions?page=2`);

    expect(await screen.findByText("第二页会话")).toBeInTheDocument();
    expect(screen.getByText("第 2 / 2 页")).toBeInTheDocument();
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([request]) => {
          const url = new URL(getRequestUrl(request), "http://localhost");
          return (
            url.pathname === "/api/v1/sessions" &&
            url.searchParams.get("page") === "2"
          );
        }),
      ).toBe(true);
    });
  });
});
