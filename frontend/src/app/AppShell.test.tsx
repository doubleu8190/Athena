import {
  act,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { User } from "../features/auth/model";
import type { Project } from "../features/projects/model";
import type { Session } from "../features/sessions/model";
import {
  PROJECT_A,
  PROJECT_B,
  getRequestUrl,
  listResponse,
  makeDeptAdmin,
  makeSession,
  makeSuperAdmin,
  makeUser,
} from "../test/fixtures";
import { jsonResponse, renderApp } from "../test/testUtils";
import { SIDEBAR_COLLAPSED_STORAGE_KEY } from "./shell/navigation";

const INITIAL_WINDOW_WIDTH = window.innerWidth;

interface ShellFetchOptions {
  deleteError?: { detail: string; status: number } | null;
  patchDelay?: Promise<void> | null;
  patchError?: { detail: string; status: number } | null;
  patchHandler?: (sessionId: string, body: { project_id?: string | null; title?: string | null }) => Promise<Response> | Response;
  projects?: Project[];
  sessions?: Session[];
  user?: User;
}

function installShellFetch(options: ShellFetchOptions = {}) {
  const user = options.user ?? makeUser();
  const projects = options.projects ?? [PROJECT_A, PROJECT_B];
  let sessions = [...(options.sessions ?? [])];
  let patchError = options.patchError ?? null;
  let deleteError = options.deleteError ?? null;
  let patchDelay = options.patchDelay ?? null;
  let patchHandler = options.patchHandler ?? null;

  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(getRequestUrl(input), "http://localhost");
      const method = init?.method ?? "GET";

      if (url.pathname.endsWith("/auth/me") && method === "GET") {
        return jsonResponse(user);
      }
      if (url.pathname.endsWith("/auth/logout") && method === "POST") {
        return new Response(null, { status: 204 });
      }
      if (url.pathname.endsWith("/auth/providers") && method === "GET") {
        return jsonResponse({
          feishu_enabled: false,
          local_login_enabled: true,
          mock_enabled: false,
        });
      }
      if (url.pathname === "/api/v1/projects" && method === "GET") {
        return jsonResponse(listResponse(projects, 1, 100, projects.length));
      }
      if (url.pathname === "/api/v1/skills" && method === "GET") {
        return jsonResponse({ items: [] });
      }
      if (url.pathname === "/api/v1/me/skills" && method === "GET") {
        return jsonResponse({ items: [] });
      }
      if (url.pathname === "/api/v1/knowledge-bases" && method === "GET") {
        return jsonResponse({ items: [], page: 1, page_size: 12 });
      }
      if (url.pathname === "/api/v1/admin/skills" && method === "GET") {
        return jsonResponse({ items: [] });
      }
      if (url.pathname === "/api/v1/admin/llm/model-profiles") {
        return jsonResponse({ default_profile_id: null, items: [] });
      }
      if (url.pathname === "/api/v1/admin/llm/compression-policy") {
        return jsonResponse({
          compression_model_profile_id: null,
          created_at: null,
          id: null,
          recent_ratio: null,
          recent_token_cap: null,
          summary_ratio: null,
          summary_token_cap: null,
          trigger_tokens: null,
        });
      }
      if (url.pathname === "/api/v1/sessions" && method === "GET") {
        const q = url.searchParams.get("q")?.trim().toLowerCase() ?? "";
        const pageSize = Number(url.searchParams.get("page_size") ?? 30);
        const matched = q
          ? sessions.filter((item) =>
              (item.title ?? "").toLowerCase().includes(q),
            )
          : sessions;
        return jsonResponse(
          listResponse(matched, 1, pageSize, matched.length),
        );
      }

      const sessionMatch = /^\/api\/v1\/sessions\/([^/]+)$/.exec(url.pathname);
      if (sessionMatch) {
        const sessionId = decodeURIComponent(sessionMatch[1]);
        const target = sessions.find((item) => item.id === sessionId);
        if (method === "GET") {
          return target
            ? jsonResponse(target)
            : jsonResponse({ detail: "session_not_found" }, 404);
        }
        if (method === "PATCH") {
          const body = JSON.parse(String(init?.body)) as {
            project_id?: string | null;
            title?: string | null;
          };
          if (patchHandler) {
            return patchHandler(sessionId, body);
          }
          if (patchDelay) {
            await patchDelay;
          }
          if (patchError) {
            return jsonResponse(
              { detail: patchError.detail },
              patchError.status,
            );
          }
          if (!target) {
            return jsonResponse({ detail: "session_not_found" }, 404);
          }
          const updated: Session = {
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
          if (deleteError) {
            return jsonResponse(
              { detail: deleteError.detail },
              deleteError.status,
            );
          }
          sessions = sessions.filter((item) => item.id !== sessionId);
          return new Response(null, { status: 204 });
        }
      }

      if (/^\/api\/v1\/sessions\/[^/]+\/(messages|runs|files)$/.test(url.pathname)) {
        if (url.pathname.endsWith("/files")) {
          return jsonResponse({ items: [], next_after_file_id: null });
        }
        return jsonResponse({ items: [], next_before_seq: null, page: 1, page_size: 20, total: 0 });
      }

      throw new Error(`Unexpected request: ${method} ${url}`);
    },
  );

  vi.stubGlobal("fetch", fetchMock);
  return {
    fetchMock,
    setDeleteError(next: { detail: string; status: number } | null) {
      deleteError = next;
    },
    setPatchDelay(next: Promise<void> | null) {
      patchDelay = next;
    },
    setPatchError(next: { detail: string; status: number } | null) {
      patchError = next;
    },
    setPatchHandler(
      next:
        | ((
            sessionId: string,
            body: { project_id?: string | null; title?: string | null },
          ) => Promise<Response> | Response)
        | null,
    ) {
      patchHandler = next;
    },
    setSessions(next: Session[]) {
      sessions = [...next];
    },
  };
}

beforeEach(() => {
  window.innerWidth = 1280;
  window.localStorage.removeItem(SIDEBAR_COLLAPSED_STORAGE_KEY);
});

afterEach(() => {
  window.innerWidth = INITIAL_WINDOW_WIDTH;
  window.localStorage.removeItem(SIDEBAR_COLLAPSED_STORAGE_KEY);
  vi.useRealTimers();
});

describe("单层可折叠主应用壳层（AppShell）", () => {
  it("支持桌面端展开/折叠至 56px、localStorage 持久化与 ⌘/Ctrl+B 快捷键切换", async () => {
    installShellFetch();
    const user = userEvent.setup();
    const firstRender = renderApp("/chat");

    const sidebar = await screen.findByRole("complementary", {
      name: "工作区侧栏",
    });
    expect(sidebar).toHaveAttribute("data-collapsed", "false");
    expect(screen.getAllByLabelText("Athena 对话首页")).toHaveLength(1);
    expect(
      screen.getByPlaceholderText("搜索历史对话…"),
    ).toBeInTheDocument();

    // 点击收起按钮切换为 56px 精简图标栏并写入 localStorage。
    await user.click(screen.getByRole("button", { name: "收起侧栏" }));
    expect(sidebar).toHaveAttribute("data-collapsed", "true");
    expect(sidebar.className).toContain("w-[56px]");
    expect(
      window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY),
    ).toBe("collapsed");
    expect(
      screen.queryByPlaceholderText("搜索历史对话…"),
    ).not.toBeInTheDocument();
    // 收起态仍保持唯一 markOnly Brand 与账户菜单触发器可用。
    expect(screen.getAllByLabelText("Athena 对话首页")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "账户菜单" }));
    expect(
      screen.getByRole("menuitem", { name: "工作偏好与记忆" }),
    ).toBeInTheDocument();
    await user.keyboard("{Escape}");

    // 重新挂载后从 localStorage 恢复折叠态。
    firstRender.unmount();
    installShellFetch();
    renderApp("/chat");
    const restoredSidebar = await screen.findByRole("complementary", {
      name: "工作区侧栏",
    });
    expect(restoredSidebar).toHaveAttribute("data-collapsed", "true");

    // ⌘B 展开，Ctrl+B 再次收起。
    fireEvent.keyDown(window, { key: "b", metaKey: true });
    expect(restoredSidebar).toHaveAttribute("data-collapsed", "false");
    expect(
      window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY),
    ).toBe("expanded");

    fireEvent.keyDown(window, { ctrlKey: true, key: "b" });
    expect(restoredSidebar).toHaveAttribute("data-collapsed", "true");
    expect(
      window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY),
    ).toBe("collapsed");
  });

  it("⌘/Ctrl+K 在收起态自动展开并聚焦搜索框，250ms 防抖搜索 q 参数与清空恢复", async () => {
    const now = new Date();
    const sessions = [
      makeSession({
        id: "s-report",
        title: "季度复盘纪要",
        updated_at: now.toISOString(),
      }),
      makeSession({
        id: "s-budget",
        title: "财务预算核对",
        updated_at: now.toISOString(),
      }),
    ];
    const { fetchMock } = installShellFetch({ sessions });
    window.localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, "collapsed");
    const user = userEvent.setup();

    renderApp("/chat");
    const sidebar = await screen.findByRole("complementary", {
      name: "工作区侧栏",
    });
    expect(sidebar).toHaveAttribute("data-collapsed", "true");

    // ⌘K 自动展开侧栏并聚焦搜索框。
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    await waitFor(() => {
      expect(sidebar).toHaveAttribute("data-collapsed", "false");
    });
    const searchInput = screen.getByPlaceholderText("搜索历史对话…");
    await waitFor(() => {
      expect(searchInput).toHaveFocus();
    });
    expect(screen.getByRole("search", { name: "搜索历史对话" })).toBeInTheDocument();

    // 输入关键词：等待 250ms 防抖后才发起带 q 参数的请求。
    await user.type(searchInput, "复盘");
    await waitFor(
      () => {
        expect(
          fetchMock.mock.calls.some(([input]) => {
            const url = new URL(getRequestUrl(input), "http://localhost");
            return (
              url.pathname === "/api/v1/sessions" &&
              url.searchParams.get("q") === "复盘"
            );
          }),
        ).toBe(true);
      },
      { timeout: 1500 },
    );

    expect(await screen.findByText("搜索结果（1）")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "季度复盘纪要" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "财务预算核对" }),
    ).not.toBeInTheDocument();

    // 搜索无匹配时展示空态。
    await user.clear(searchInput);
    await user.type(searchInput, "不存在关键词");
    expect(await screen.findByText("搜索结果（0）")).toBeInTheDocument();
    expect(screen.getByText("未找到匹配的对话")).toBeInTheDocument();

    // 点击清空搜索按钮立即恢复时间分组历史列表。
    await user.click(screen.getByRole("button", { name: "清空搜索" }));
    expect(searchInput).toHaveValue("");
    expect(await screen.findByText("今天")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "财务预算核对" }),
    ).toBeInTheDocument();
  });

  it("⌘/Ctrl+Shift+O 全局快捷键跳转 /chat 新建对话", async () => {
    installShellFetch();
    const app = renderApp("/skills");
    await screen.findByRole("heading", { level: 1, name: "技能库" });

    fireEvent.keyDown(window, { key: "O", metaKey: true, shiftKey: true });
    await waitFor(() => {
      expect(app.getLocation().pathname).toBe("/chat");
    });
  });

  it("按本地日期自然分组展示今天 / 昨天 / 近 7 天 / 更早历史对话", async () => {
    const now = new Date();
    const today = new Date(
      now.getFullYear(),
      now.getMonth(),
      now.getDate(),
      10,
      0,
      0,
    );
    const yesterday = new Date(
      now.getFullYear(),
      now.getMonth(),
      now.getDate() - 1,
      18,
      0,
      0,
    );
    const threeDaysAgo = new Date(
      now.getFullYear(),
      now.getMonth(),
      now.getDate() - 3,
      12,
      0,
      0,
    );
    const twentyDaysAgo = new Date(
      now.getFullYear(),
      now.getMonth(),
      now.getDate() - 20,
      9,
      0,
      0,
    );

    installShellFetch({
      sessions: [
        makeSession({
          id: "s-1",
          title: "今天的会话",
          updated_at: today.toISOString(),
        }),
        makeSession({
          id: "s-2",
          title: "昨天的会话",
          updated_at: yesterday.toISOString(),
        }),
        makeSession({
          id: "s-3",
          title: "三天前的会话",
          updated_at: threeDaysAgo.toISOString(),
        }),
        makeSession({
          id: "s-4",
          title: "更早的会话",
          updated_at: twentyDaysAgo.toISOString(),
        }),
      ],
    });

    renderApp("/chat");

    const todayRegion = await screen.findByRole("region", { name: "今天" });
    expect(
      within(todayRegion).getByRole("link", { name: "今天的会话" }),
    ).toBeInTheDocument();

    const yesterdayRegion = screen.getByRole("region", { name: "昨天" });
    expect(
      within(yesterdayRegion).getByRole("link", { name: "昨天的会话" }),
    ).toBeInTheDocument();

    const last7Region = screen.getByRole("region", { name: "近 7 天" });
    expect(
      within(last7Region).getByRole("link", { name: "三天前的会话" }),
    ).toBeInTheDocument();

    const earlierRegion = screen.getByRole("region", { name: "更早" });
    expect(
      within(earlierRegion).getByRole("link", { name: "更早的会话" }),
    ).toBeInTheDocument();
  });

  it("支持侧栏会话行内联重命名（Esc 取消、非法标题校验提示、Enter 保存）", async () => {
    const session = makeSession({
      id: "s-rename",
      title: "原始标题",
      updated_at: new Date().toISOString(),
    });
    const { fetchMock } = installShellFetch({ sessions: [session] });
    const user = userEvent.setup();

    renderApp("/chat");

    const row = (
      await screen.findByRole("link", { name: "原始标题" })
    ).closest("li") as HTMLElement;

    // 打开 ... 菜单进入内联重命名，按 Esc 取消不发 PATCH。
    await user.click(within(row).getByRole("button", { name: "更多会话操作" }));
    await user.click(screen.getByRole("menuitem", { name: "重命名" }));
    const editInput = await screen.findByLabelText("重命名会话标题");
    expect(editInput).toHaveValue("原始标题");
    await user.clear(editInput);
    await user.type(editInput, "放弃修改");
    await user.keyboard("{Escape}");

    expect(screen.queryByLabelText("重命名会话标题")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "原始标题" })).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH"),
    ).toBe(false);

    // 再次进入内联重命名：空标题触发校验提示并拦截提交。
    const rowAgain = screen
      .getByRole("link", { name: "原始标题" })
      .closest("li") as HTMLElement;
    await user.click(
      within(rowAgain).getByRole("button", { name: "更多会话操作" }),
    );
    await user.click(screen.getByRole("menuitem", { name: "重命名" }));
    const renameInput = await screen.findByLabelText("重命名会话标题");
    await user.clear(renameInput);
    await user.keyboard("{Enter}");
    expect(screen.getByRole("alert")).toHaveTextContent("请输入会话标题。");
    expect(
      fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH"),
    ).toBe(false);

    // 输入合法标题后按 Enter 调用 PATCH 并刷新展示。
    await user.type(renameInput, "已重命名对话");
    await user.keyboard("{Enter}");

    expect(
      await screen.findByRole("link", { name: "已重命名对话" }),
    ).toBeInTheDocument();
    const patchCall = fetchMock.mock.calls.find(
      ([input, init]) =>
        getRequestUrl(input).endsWith("/sessions/s-rename") &&
        init?.method === "PATCH",
    );
    expect(JSON.parse(String(patchCall?.[1]?.body))).toEqual({
      title: "已重命名对话",
    });
  });

  it("支持移动对话至项目 / 移出项目，遇 409 session_has_active_run 展示 InlineAlert 并在提交中禁关弹窗", async () => {
    const session = makeSession({
      id: "s-move",
      project_id: PROJECT_A.id,
      title: "跨项目对话",
      updated_at: new Date().toISOString(),
    });
    const controller = installShellFetch({
      patchError: { detail: "session_has_active_run", status: 409 },
      sessions: [session],
    });
    const user = userEvent.setup();

    renderApp("/chat");

    const row = (
      await screen.findByRole("link", { name: "跨项目对话" })
    ).closest("li") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "更多会话操作" }));
    await user.click(screen.getByRole("menuitem", { name: "移动 / 移出项目" }));

    const dialog = screen.getByRole("dialog", { name: "调整所属项目" });
    await user.click(within(dialog).getByRole("combobox", { name: "目标项目" }));
    await user.click(screen.getByRole("option", { name: "未归属项目" }));
    await user.click(within(dialog).getByRole("button", { name: "保存归属" }));

    // 409 session_has_active_run 保持弹窗打开并展示明确 InlineAlert。
    expect(
      await within(dialog).findByText(
        "当前对话有正在执行的任务，请等待完成或停止后再操作。",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("dialog", { name: "调整所属项目" }),
    ).toBeInTheDocument();

    // 清除错误并模拟提交延迟：提交期间 Escape 不关闭弹窗，完成后成功关闭并发送 project_id: null。
    let resolvePatch!: () => void;
    controller.setPatchError(null);
    controller.setPatchDelay(
      new Promise<void>((resolve) => {
        resolvePatch = resolve;
      }),
    );

    await user.click(within(dialog).getByRole("button", { name: "保存归属" }));
    await waitFor(() => {
      expect(
        within(dialog).getByRole("button", { name: "取消" }),
      ).toBeDisabled();
    });
    await user.keyboard("{Escape}");
    expect(
      screen.getByRole("dialog", { name: "调整所属项目" }),
    ).toBeInTheDocument();

    await act(async () => {
      resolvePatch();
    });
    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", { name: "调整所属项目" }),
      ).not.toBeInTheDocument();
    });

    const lastPatch = controller.fetchMock.mock.calls
      .filter(([, init]) => init?.method === "PATCH")
      .at(-1);
    expect(JSON.parse(String(lastPatch?.[1]?.body))).toEqual({
      project_id: null,
    });
  });

  it("支持删除当前路由会话并在确认后立即跳转 /chat，遇 409 保留弹窗", async () => {
    const session = makeSession({
      id: "s-del-active",
      title: "当前待删对话",
      updated_at: new Date().toISOString(),
    });
    const controller = installShellFetch({
      deleteError: { detail: "session_has_active_run", status: 409 },
      sessions: [session],
    });
    const user = userEvent.setup();

    const app = renderApp("/chat/s-del-active");

    const row = (
      await screen.findByRole("link", { name: "当前待删对话" })
    ).closest("li") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "更多会话操作" }));
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
    expect(app.getLocation().pathname).toBe("/chat/s-del-active");

    controller.setDeleteError(null);
    await user.click(
      within(alertDialog).getByRole("button", { name: "确认删除" }),
    );
    await waitFor(() => {
      expect(app.getLocation().pathname).toBe("/chat");
    });
    expect(
      screen.queryByRole("link", { name: "当前待删对话" }),
    ).not.toBeInTheDocument();
  });

  it("严格按三级角色动态呈现管理导航（member / dept_admin / super_admin），绝无双 Logo 与重复 AdminSubnav", async () => {
    // 1. 普通员工：完全不渲染管理分区。
    installShellFetch({ user: makeUser() });
    const memberView = renderApp("/chat");
    await screen.findByRole("complementary", { name: "工作区侧栏" });
    expect(screen.getAllByLabelText("Athena 对话首页")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "管理后台" })).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "部门管理" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("navigation", { name: "管理子导航" }),
    ).not.toBeInTheDocument();
    memberView.unmount();

    // 2. 部门管理员：仅展示「部门管理」下 2 项入口，收起态指向 /admin/skills。
    installShellFetch({ user: makeDeptAdmin() });
    const deptView = renderApp("/chat");
    const deptGroup = await screen.findByRole("group", { name: "部门管理" });
    expect(within(deptGroup).getAllByRole("link")).toHaveLength(2);
    expect(
      within(deptGroup).getByRole("link", { name: "部门技能" }),
    ).toHaveAttribute("href", "/admin/skills");
    expect(
      within(deptGroup).getByRole("link", { name: "部门知识库" }),
    ).toHaveAttribute("href", "/knowledge?scope=department");
    expect(screen.queryByRole("button", { name: "管理后台" })).not.toBeInTheDocument();
    fireEvent.keyDown(window, { key: "b", metaKey: true });
    expect(
      screen.getByRole("link", { name: "部门管理" }),
    ).toHaveAttribute("href", "/admin/skills");
    window.localStorage.removeItem(SIDEBAR_COLLAPSED_STORAGE_KEY);
    deptView.unmount();

    // 3. 超级管理员：非 /admin 路径默认折叠「管理后台」，进入 /admin/* 自动展开全部 6 项，绝无 AdminSubnav。
    installShellFetch({ user: makeSuperAdmin() });
    const user = userEvent.setup();
    const adminView = renderApp("/chat");
    const adminTrigger = await screen.findByRole("button", {
      name: "管理后台",
    });
    expect(adminTrigger).toHaveAttribute("aria-expanded", "false");
    expect(
      screen.queryByRole("link", { name: "模型与压缩策略" }),
    ).not.toBeInTheDocument();

    await user.click(adminTrigger);
    const adminGroup = screen.getByRole("group", { name: "管理后台" });
    expect(within(adminGroup).getAllByRole("link")).toHaveLength(6);
    expect(
      within(adminGroup).getByRole("link", { name: "模型与压缩策略" }),
    ).toHaveAttribute("href", "/admin/models");
    expect(
      within(adminGroup).getByRole("link", { name: "系统提示词" }),
    ).toHaveAttribute("href", "/admin/system-prompts");
    expect(
      within(adminGroup).getByRole("link", { name: "MCP 服务" }),
    ).toHaveAttribute("href", "/admin/mcp");
    expect(
      within(adminGroup).getByRole("link", { name: "工具审批策略" }),
    ).toHaveAttribute("href", "/admin/tools");
    expect(
      within(adminGroup).getByRole("link", { name: "企业与部门技能" }),
    ).toHaveAttribute("href", "/admin/skills");
    expect(
      within(adminGroup).getByRole("link", { name: "子智能体" }),
    ).toHaveAttribute("href", "/admin/subagents");

    // 点击进入 /admin/models 后仍无 AdminSubnav 且仅 1 个 Brand。
    await user.click(
      within(adminGroup).getByRole("link", { name: "模型与压缩策略" }),
    );
    expect(
      await screen.findByRole("heading", { level: 1, name: "模型与压缩策略" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("navigation", { name: "管理子导航" }),
    ).not.toBeInTheDocument();
    expect(screen.getAllByLabelText("Athena 对话首页")).toHaveLength(1);
    expect(adminTrigger).toHaveAttribute("aria-expanded", "true");

    // 收起态图标指向 /admin/models。
    fireEvent.keyDown(window, { key: "b", metaKey: true });
    expect(screen.getByRole("link", { name: "管理后台" })).toHaveAttribute(
      "href",
      "/admin/models",
    );
    adminView.unmount();
  });

  it("底部账户菜单展示三项核心入口并支持退出登录跳回 /login", async () => {
    installShellFetch({
      user: makeSuperAdmin({
        departments: [
          {
            id: "dept-hq",
            is_primary: true,
            name: "集团总部",
            path: "/dept-hq/",
          },
        ],
        display_name: "林超管",
        email: "super@example.com",
      }),
    });
    const user = userEvent.setup();
    const app = renderApp("/chat");

    const accountButton = await screen.findByRole("button", {
      name: "账户菜单",
    });
    expect(accountButton).toHaveTextContent("林超管");
    expect(accountButton).toHaveTextContent("集团总部");
    expect(accountButton).toHaveTextContent("超管");

    await user.click(accountButton);
    const menu = screen.getByRole("menu");
    expect(within(menu).getByText("super@example.com")).toBeInTheDocument();
    expect(within(menu).getByText("超级管理员")).toBeInTheDocument();
    expect(
      within(menu).getByRole("menuitem", { name: "工作偏好与记忆" }),
    ).toHaveAttribute("href", "/preferences");
    expect(
      within(menu).getByRole("menuitem", { name: "个人资料与安全" }),
    ).toHaveAttribute("href", "/profile");

    await user.click(within(menu).getByRole("menuitem", { name: "退出登录" }));
    await waitFor(() => {
      expect(app.getLocation().pathname).toBe("/login");
    });
  });

  it("<1024px 窄屏隐藏固定侧栏，通过顶部汉堡打开左侧 Sheet 抽屉并在导航后自动关闭，⌘K 同步打开抽屉并聚焦搜索框", async () => {
    window.innerWidth = 768;
    installShellFetch({
      sessions: [
        makeSession({
          id: "s-mobile",
          title: "移动端历史对话",
          updated_at: new Date().toISOString(),
        }),
      ],
    });
    const user = userEvent.setup();
    const app = renderApp("/chat");

    await screen.findByRole("heading", { name: /你好，/ });
    expect(
      screen.queryByRole("complementary", { name: "工作区侧栏" }),
    ).not.toBeInTheDocument();
    expect(screen.getAllByLabelText("Athena 对话首页")).toHaveLength(1);

    // 验证窄屏顶栏左侧汉堡按钮与唯一 Brand 属于同一左对齐容器，与右侧新建对话按钮分列顶栏两侧。
    const menuButton = screen.getByRole("button", {
      name: "打开导航菜单",
    });
    const header = menuButton.closest("header");
    expect(header).not.toBeNull();
    if (!header) {
      throw new Error("Expected mobile top header element");
    }
    const brandLink = within(header).getByRole("link", {
      name: "Athena 对话首页",
    });
    const newChatLink = within(header).getByRole("link", {
      name: "新建对话",
    });
    const leftGroup = menuButton.parentElement;
    expect(leftGroup).not.toBeNull();
    expect(leftGroup).toBe(brandLink.parentElement);
    expect(leftGroup).not.toBe(header);
    expect(Array.from(header.children)).toEqual([leftGroup, newChatLink]);

    // 点击汉堡打开 Sheet 抽屉，点击「技能库」导航后自动关闭抽屉。
    await user.click(screen.getByRole("button", { name: "打开导航菜单" }));
    const drawer = screen.getByRole("dialog", { name: "工作区导航" });
    expect(
      await within(drawer).findByRole("link", { name: "移动端历史对话" }),
    ).toBeInTheDocument();

    await user.click(within(drawer).getByRole("link", { name: "技能库" }));
    await waitFor(() => {
      expect(app.getLocation().pathname).toBe("/skills");
    });
    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", { name: "工作区导航" }),
      ).not.toBeInTheDocument();
    });

    // 窄屏下 ⌘K 自动打开移动抽屉并聚焦搜索框。
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    const reopenedDrawer = await screen.findByRole("dialog", {
      name: "工作区导航",
    });
    const mobileSearch = within(reopenedDrawer).getByPlaceholderText(
      "搜索历史对话…",
    );
    await waitFor(() => {
      expect(mobileSearch).toHaveFocus();
    });
  });

  it("收起态点击搜索图标自动展开并聚焦搜索框；侧栏项目仅展示最近 5 个，移动弹窗展示全部项目", async () => {
    const sevenProjects: Project[] = Array.from({ length: 7 }, (_, index) => ({
      ...PROJECT_A,
      id: `project-${index + 1}`,
      name: `业务项目 ${index + 1}`,
    }));
    const session = makeSession({
      id: "s-unbound",
      project_id: null,
      title: "待分配项目对话",
      updated_at: new Date().toISOString(),
    });
    installShellFetch({ projects: sevenProjects, sessions: [session] });
    window.localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, "collapsed");
    const user = userEvent.setup();

    renderApp("/chat");

    const sidebar = await screen.findByRole("complementary", {
      name: "工作区侧栏",
    });
    expect(sidebar).toHaveAttribute("data-collapsed", "true");

    // 点击收起态搜索图标自动展开侧栏并聚焦搜索框。
    await user.click(screen.getByRole("button", { name: "搜索历史对话" }));
    expect(sidebar).toHaveAttribute("data-collapsed", "false");
    const searchInput = screen.getByPlaceholderText("搜索历史对话…");
    await waitFor(() => {
      expect(searchInput).toHaveFocus();
    });

    // 侧栏项目折叠区仅显示最近 5 个项目 + 查看全部项目链接。
    const nav = screen.getByRole("navigation", { name: "主导航" });
    expect(
      await within(nav).findByRole("link", { name: "业务项目 5" }),
    ).toBeInTheDocument();
    expect(
      within(nav).queryByRole("link", { name: "业务项目 6" }),
    ).not.toBeInTheDocument();
    expect(
      within(nav).getByRole("link", { name: "查看全部项目 →" }),
    ).toHaveAttribute("href", "/projects");

    // 移动至项目弹窗列出全部 7 个项目（含第 7 个）与「未归属项目」。
    const row = (
      await screen.findByRole("link", { name: "待分配项目对话" })
    ).closest("li") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "更多会话操作" }));
    await user.click(screen.getByRole("menuitem", { name: "移动至项目" }));
    const dialog = screen.getByRole("dialog", { name: "调整所属项目" });
    await user.click(within(dialog).getByRole("combobox", { name: "目标项目" }));
    expect(
      screen.getByRole("option", { name: "业务项目 7" }),
    ).toBeInTheDocument();
  });

  it("localStorage 不可用时侧栏折叠仍能正常切换，内联重命名失焦也可保存", async () => {
    const getItemSpy = vi
      .spyOn(Storage.prototype, "getItem")
      .mockImplementation(() => {
        throw new Error("storage blocked");
      });
    const setItemSpy = vi
      .spyOn(Storage.prototype, "setItem")
      .mockImplementation(() => {
        throw new Error("storage blocked");
      });
    try {
      const session = makeSession({
        id: "s-blur",
        title: "失焦重命名测试",
        updated_at: new Date().toISOString(),
      });
      installShellFetch({ sessions: [session] });
      const user = userEvent.setup();

      renderApp("/chat");
      const sidebar = await screen.findByRole("complementary", {
        name: "工作区侧栏",
      });
      expect(sidebar).toHaveAttribute("data-collapsed", "false");

      await user.click(screen.getByRole("button", { name: "收起侧栏" }));
      expect(sidebar).toHaveAttribute("data-collapsed", "true");
      await user.click(screen.getByRole("button", { name: "展开侧栏" }));
      expect(sidebar).toHaveAttribute("data-collapsed", "false");

      const row = (
        await screen.findByRole("link", { name: "失焦重命名测试" })
      ).closest("li") as HTMLElement;
      await user.click(
        within(row).getByRole("button", { name: "更多会话操作" }),
      );
      await user.click(screen.getByRole("menuitem", { name: "重命名" }));
      const editInput = await screen.findByLabelText("重命名会话标题");
      await user.clear(editInput);
      await user.type(editInput, "失焦后已保存标题");
      fireEvent.blur(editInput);

      expect(
        await screen.findByRole("link", { name: "失焦后已保存标题" }),
      ).toBeInTheDocument();
    } finally {
      getItemSpy.mockRestore();
      setItemSpy.mockRestore();
    }
  });

  it("弹窗打开、内联编辑与可编辑输入框内均正确屏蔽/区分全局快捷键，且 ⌘⇧O 继承当前项目上下文", async () => {
    const boundSession = makeSession({
      id: "s-bound",
      project_id: PROJECT_A.id,
      title: "已绑项目会话",
      updated_at: new Date().toISOString(),
    });
    installShellFetch({ sessions: [boundSession] });
    const user = userEvent.setup();
    const app = renderApp("/chat");

    const sidebar = await screen.findByRole("complementary", {
      name: "工作区侧栏",
    });
    const draftTextarea = await screen.findByLabelText("对话草稿");
    draftTextarea.focus();

    // 在可编辑文本框内按 Ctrl+B、⌘⇧O、macOS 原生 Ctrl+K 不得误触发壳层快捷键。
    fireEvent.keyDown(draftTextarea, { ctrlKey: true, key: "b" });
    expect(sidebar).toHaveAttribute("data-collapsed", "false");
    fireEvent.keyDown(draftTextarea, { ctrlKey: true, key: "k" });
    expect(draftTextarea).toHaveFocus();
    fireEvent.keyDown(draftTextarea, { key: "O", metaKey: true, shiftKey: true });
    expect(app.getLocation().pathname).toBe("/chat");

    // 打开「新建项目」弹窗期间，全局快捷键全部屏蔽。
    await user.click(screen.getByRole("button", { name: "新建项目" }));
    const dialog = screen.getByRole("dialog", { name: "新建项目" });
    fireEvent.keyDown(window, { key: "b", metaKey: true });
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    fireEvent.keyDown(window, { key: "O", metaKey: true, shiftKey: true });
    expect(sidebar).toHaveAttribute("data-collapsed", "false");
    expect(dialog).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", { name: "新建项目" }),
      ).not.toBeInTheDocument();
    });

    // 进入绑定 PROJECT_A 的会话页后，按 ⌘⇧O 统一走 newChatTarget 继承项目上下文。
    await act(async () => {
      await app.navigateTo("/chat/s-bound");
    });
    await screen.findByRole("heading", { name: "已绑项目会话" });
    fireEvent.keyDown(window, { key: "O", metaKey: true, shiftKey: true });
    await waitFor(() => {
      expect(app.getLocation()).toEqual({
        pathname: "/chat/new",
        search: `?project=${PROJECT_A.id}`,
      });
    });
  });

  it("移动端 Sheet 抽屉内搜索框与内联重命名按 Escape 先清空/取消而不关闭抽屉，嵌套新建项目可正常输入并恢复焦点", async () => {
    window.innerWidth = 768;
    const session = makeSession({
      id: "s-mobile-esc",
      title: "抽屉内对话",
      updated_at: new Date().toISOString(),
    });
    installShellFetch({ sessions: [session] });
    const user = userEvent.setup();

    renderApp("/chat");
    await screen.findByRole("heading", { name: /你好，/ });

    // 1. 打开抽屉，输入搜索词后按 Escape：清空搜索并保持抽屉打开；再次按 Escape 才关闭抽屉。
    await user.click(screen.getByRole("button", { name: "打开导航菜单" }));
    const drawer = screen.getByRole("dialog", { name: "工作区导航" });
    const searchInput = within(drawer).getByPlaceholderText("搜索历史对话…");
    await user.type(searchInput, "抽屉");
    expect(searchInput).toHaveValue("抽屉");

    await user.keyboard("{Escape}");
    expect(searchInput).toHaveValue("");
    expect(
      screen.getByRole("dialog", { name: "工作区导航" }),
    ).toBeInTheDocument();

    // 2. 抽屉内进入内联重命名后按 Escape：仅取消重命名，抽屉保持打开。
    const row = (
      await within(drawer).findByRole("link", { name: "抽屉内对话" })
    ).closest("li") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "更多会话操作" }));
    await user.click(screen.getByRole("menuitem", { name: "重命名" }));
    const renameInput = await within(drawer).findByLabelText("重命名会话标题");
    await waitFor(() => expect(renameInput).toHaveFocus());

    await user.keyboard("{Escape}");
    expect(
      within(drawer).queryByLabelText("重命名会话标题"),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("dialog", { name: "工作区导航" }),
    ).toBeInTheDocument();

    // 3. 抽屉内打开 Radix NewProjectDialog 可正常输入，按 Escape 关闭并恢复触发按钮焦点。
    const newProjectButton = within(drawer).getByRole("button", {
      name: "新建项目",
    });
    await user.click(newProjectButton);
    const projectDialog = screen.getByRole("dialog", { name: "新建项目" });
    const projectNameInput = within(projectDialog).getByLabelText("项目名称");
    await waitFor(() => expect(projectNameInput).toHaveFocus());
    await user.type(projectNameInput, "移动端新建项目");
    expect(projectNameInput).toHaveValue("移动端新建项目");

    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", { name: "新建项目" }),
      ).not.toBeInTheDocument();
    });
    expect(
      screen.getByRole("dialog", { name: "工作区导航" }),
    ).toBeInTheDocument();
    await waitFor(() => expect(newProjectButton).toHaveFocus());

    // 4. 空搜索且非编辑态按 Escape 正常关闭抽屉。
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", { name: "工作区导航" }),
      ).not.toBeInTheDocument();
    });
  });

  it("内联重命名跨行不串号、失败后待禁用解除再恢复焦点、后台刷新不覆盖输入草稿", async () => {
    const now = new Date().toISOString();
    const sessionA = makeSession({
      id: "s-row-a",
      title: "会话 A",
      updated_at: now,
    });
    const sessionB = makeSession({
      id: "s-row-b",
      title: "会话 B",
      updated_at: now,
    });
    let resolvePatchA!: (res: Response) => void;
    const controller = installShellFetch({
      sessions: [sessionA, sessionB],
    });
    controller.setPatchHandler(async (sessionId, body) => {
      if (sessionId === "s-row-a" && body.title === "A 失败标题") {
        return jsonResponse({ detail: "http_error" }, 500);
      }
      if (sessionId === "s-row-a" && body.title === "A 慢保存") {
        return new Promise<Response>((resolve) => {
          resolvePatchA = resolve;
        });
      }
      return jsonResponse({ ...sessionB, title: body.title ?? sessionB.title });
    });
    const user = userEvent.setup();
    const app = renderApp("/chat");

    // 1. 失败后在 disabled 解除后恢复焦点，便于用户直接修改或按 Esc。
    const rowA = (await screen.findByRole("link", { name: "会话 A" })).closest(
      "li",
    ) as HTMLElement;
    await user.click(within(rowA).getByRole("button", { name: "更多会话操作" }));
    await user.click(screen.getByRole("menuitem", { name: "重命名" }));
    const inputA = await screen.findByLabelText("重命名会话标题");
    await user.clear(inputA);
    await user.type(inputA, "A 失败标题");
    await user.keyboard("{Enter}");

    expect(await screen.findByRole("alert")).toHaveTextContent("服务器拒绝");
    await waitFor(() => {
      expect(inputA).not.toBeDisabled();
      expect(inputA).toHaveFocus();
    });

    // 2. 后台刷新返回新标题时，不覆盖用户正在输入的草稿。
    await user.clear(inputA);
    await user.type(inputA, "用户输入中的草稿");
    controller.setSessions([
      { ...sessionA, title: "远端后台刷新标题" },
      sessionB,
    ]);
    await act(async () => {
      await app.client.invalidateQueries({ queryKey: ["sessions"] });
    });
    expect(inputA).toHaveValue("用户输入中的草稿");

    // 3. A 发起慢 PATCH 后立刻切去重命名 B；A 迟到成功不得关掉 B 的编辑框。
    await user.clear(inputA);
    await user.type(inputA, "A 慢保存");
    await user.keyboard("{Enter}");
    await waitFor(() => expect(resolvePatchA).toBeTypeOf("function"));

    const rowB = screen
      .getByRole("link", { name: "会话 B" })
      .closest("li") as HTMLElement;
    await user.click(within(rowB).getByRole("button", { name: "更多会话操作" }));
    await user.click(screen.getByRole("menuitem", { name: "重命名" }));
    const inputB = await screen.findByLabelText("重命名会话标题");
    expect(inputB).toHaveValue("会话 B");

    await act(async () => {
      resolvePatchA(jsonResponse({ ...sessionA, title: "A 慢保存" }));
    });
    expect(screen.getByLabelText("重命名会话标题")).toHaveValue("会话 B");
  });

  it("矮视口下工作台导航区具备独立滚动上限约束，项目 ID 与未归属哨兵绝对隔离", async () => {
    const sentinelProject: Project = {
      ...PROJECT_A,
      id: "__unbound__",
      name: "同名哨兵项目",
    };
    const session = makeSession({
      id: "s-sentinel",
      project_id: null,
      title: "哨兵隔离测试对话",
      updated_at: new Date().toISOString(),
    });
    const controller = installShellFetch({
      projects: [sentinelProject],
      sessions: [session],
      user: makeSuperAdmin(),
    });
    const user = userEvent.setup();

    renderApp("/admin/models");

    const mainNav = await screen.findByRole("navigation", { name: "主导航" });
    expect(mainNav.className).toContain("overflow-y-auto");
    expect(mainNav.className).toContain("max-h-[45%]");
    expect(mainNav.className).not.toContain("shrink-0");

    const row = (
      await screen.findByRole("link", { name: "哨兵隔离测试对话" })
    ).closest("li") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "更多会话操作" }));
    await user.click(screen.getByRole("menuitem", { name: "移动至项目" }));
    const dialog = screen.getByRole("dialog", { name: "调整所属项目" });
    await user.click(within(dialog).getByRole("combobox", { name: "目标项目" }));
    await user.click(screen.getByRole("option", { name: "同名哨兵项目" }));
    await user.click(within(dialog).getByRole("button", { name: "保存归属" }));

    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", { name: "调整所属项目" }),
      ).not.toBeInTheDocument();
    });
    const patchCall = controller.fetchMock.mock.calls.find(
      ([, init]) => init?.method === "PATCH",
    );
    expect(JSON.parse(String(patchCall?.[1]?.body))).toEqual({
      project_id: "__unbound__",
    });
  });
});
