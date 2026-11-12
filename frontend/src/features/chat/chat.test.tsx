import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  getRequestUrl,
  listResponse,
  makeSession,
  PROJECT_A,
  PROJECT_B,
  TEST_USER,
} from "../../test/fixtures";
import { projectKeys } from "../projects/queryKeys";
import { jsonResponse, renderApp } from "../../test/testUtils";

const INITIAL_WINDOW_WIDTH = window.innerWidth;
const RECENT_SECTION_STORAGE_KEY = "athena.chat.recent-collapsed";

beforeEach(() => {
  window.localStorage.removeItem(RECENT_SECTION_STORAGE_KEY);
});

afterEach(() => {
  window.innerWidth = INITIAL_WINDOW_WIDTH;
});

async function chooseProject(
  user: ReturnType<typeof userEvent.setup>,
  projectId: string,
) {
  const trigger = screen.getByRole("combobox", { name: "更改项目" });
  await user.click(trigger);
  const name =
    projectId === ""
      ? "未归属项目"
      : projectId === PROJECT_A.id
        ? PROJECT_A.name
        : PROJECT_B.name;
  await user.click(screen.getByRole("option", { name: new RegExp(name) }));
}

function expectSelectedProject(projectId: string) {
  const name =
    projectId === ""
      ? "未归属项目"
      : projectId === PROJECT_A.id
        ? PROJECT_A.name
        : PROJECT_B.name;
  expect(screen.getByRole("combobox", { name: "更改项目" })).toHaveTextContent(name);
}

interface ChatFetchOptions {
  projects?: typeof PROJECT_A[];
  recent?: ReturnType<typeof makeSession>[];
  projectSessions?: Record<string, ReturnType<typeof makeSession>[]>;
  unboundTotal?: number;
  unboundSessions?: ReturnType<typeof makeSession>[];
  activeSession?: ReturnType<typeof makeSession> | null;
}

function installChatFetch(options: ChatFetchOptions = {}) {
  const projects = options.projects ?? [PROJECT_A, PROJECT_B];
  const recent = options.recent ?? [];
  const projectSessions = options.projectSessions ?? {};
  const unboundTotal = options.unboundTotal ?? 0;
  const unboundSessions = options.unboundSessions ?? [];
  const activeSession = options.activeSession ?? null;

  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(getRequestUrl(input), "http://localhost");
      if (url.pathname.endsWith("/auth/me")) {
        return jsonResponse(TEST_USER);
      }
      if (
        url.pathname === "/api/v1/skills" &&
        (!init?.method || init.method === "GET")
      ) {
        return jsonResponse({ items: [] });
      }
      if (
        url.pathname === "/api/v1/projects" &&
        (!init?.method || init.method === "GET")
      ) {
        return jsonResponse(
          listResponse(projects, 1, 100, projects.length),
        );
      }
      const projectDetailMatch = /^\/api\/v1\/projects\/([^/]+)$/.exec(
        url.pathname,
      );
      if (
        projectDetailMatch &&
        (!init?.method || init.method === "GET")
      ) {
        const project = projects.find(
          (item) =>
            item.id === decodeURIComponent(projectDetailMatch[1]),
        );
        return project
          ? jsonResponse(project)
          : jsonResponse({ detail: "project_not_found" }, 404);
      }
      if (url.pathname === "/api/v1/sessions") {
        const pageSize = Number(url.searchParams.get("page_size"));
        const projectId = url.searchParams.get("project_id");
        const isUnbound = url.searchParams.get("unbound") === "true";
        if (isUnbound && pageSize === 1) {
          return jsonResponse(
            listResponse(
              unboundTotal > 0 ? unboundSessions.slice(0, 1) : [],
              1,
              1,
              unboundTotal,
            ),
          );
        }
        if (isUnbound && pageSize === 5) {
          return jsonResponse(
            listResponse(
              unboundSessions,
              1,
              5,
              Math.max(unboundTotal, unboundSessions.length),
            ),
          );
        }
        if (projectId && pageSize === 5) {
          const items = projectSessions[projectId] ?? [];
          return jsonResponse(
            listResponse(items, 1, 5, items.length),
          );
        }
        if (pageSize === 8) {
          return jsonResponse(
            listResponse(recent, 1, 8, recent.length),
          );
        }
      }
      if (
        activeSession &&
        url.pathname === `/api/v1/sessions/${activeSession.id}`
      ) {
        return jsonResponse(activeSession);
      }
      if (
        activeSession &&
        url.pathname ===
          `/api/v1/sessions/${activeSession.id}/files`
      ) {
        return jsonResponse({
          items: [],
          next_after_file_id: null,
        });
      }
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
    },
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("对话侧栏项目树", () => {
  it("最近列表默认展开，点击与键盘折叠状态跨路由和重新挂载保留", async () => {
    const recent = [
      makeSession({ id: "recent-one", title: "最近对话一" }),
      makeSession({ id: "recent-two", title: "最近对话二" }),
    ];
    installChatFetch({ recent });
    const user = userEvent.setup();
    const view = renderApp("/chat");

    const section = await screen.findByRole("region", { name: "最近" });
    const toggle = within(section).getByRole("button", { name: "最近" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(await within(section).findByText("最近对话一")).toBeVisible();

    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(section.querySelector("#recent-sessions-content")).not.toBeVisible();

    toggle.focus();
    await user.keyboard("{Enter}");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle).toHaveFocus();
    await user.keyboard(" ");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    view.navigateTo("/chat/recent-one");
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    view.unmount();
    installChatFetch({ recent });
    renderApp("/chat");
    const reloadedSection = await screen.findByRole("region", { name: "最近" });
    expect(
      within(reloadedSection).getByRole("button", { name: "最近" }),
    ).toHaveAttribute("aria-expanded", "false");
    expect(reloadedSection.querySelector("#recent-sessions-content")).not.toBeVisible();
  });

  it("本地存储读取或写入失败时仍可正常展开收起", async () => {
    installChatFetch({ recent: [makeSession({ id: "recent-one", title: "最近对话一" })] });
    const getItemSpy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("storage unavailable");
    });
    const setItemSpy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("storage unavailable");
    });
    const user = userEvent.setup();

    renderApp("/chat");
    const section = await screen.findByRole("region", { name: "最近" });
    const toggle = within(section).getByRole("button", { name: "最近" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(section.querySelector("#recent-sessions-content")).not.toBeVisible();

    getItemSpy.mockRestore();
    setItemSpy.mockRestore();
  });

  it("点击项目只展开会话，多个项目可同时展开且不会导航", async () => {
    installChatFetch({
      projectSessions: {
        [PROJECT_A.id]: [
          makeSession({ id: "session-a1", title: "增长会话一" }),
          makeSession({ id: "session-a2", title: "增长会话二" }),
        ],
        [PROJECT_B.id]: [
          makeSession({
            id: "session-b1",
            project_id: PROJECT_B.id,
            title: "产品会话一",
          }),
        ],
      },
    });
    const user = userEvent.setup();

    renderApp("/chat");

    await screen.findByRole("heading", { name: "对话工作台" });
    const projectAButton = await screen.findByRole("button", {
      name: `项目 ${PROJECT_A.name}`,
    });
    const projectBButton = screen.getByRole("button", {
      name: `项目 ${PROJECT_B.name}`,
    });

    await user.click(projectAButton);
    expect(await screen.findByText("增长会话一")).toBeInTheDocument();
    expect(projectAButton).toHaveAttribute("aria-expanded", "true");

    await user.click(projectBButton);
    expect(await screen.findByText("产品会话一")).toBeInTheDocument();
    expect(screen.getByText("增长会话一")).toBeInTheDocument();
    expect(projectBButton).toHaveAttribute("aria-expanded", "true");
    expect(
      screen.queryByRole("navigation", { name: "项目页面" }),
    ).not.toBeInTheDocument();

    await user.click(projectAButton);
    expect(screen.queryByText("增长会话一")).not.toBeInTheDocument();
    expect(screen.getByText("产品会话一")).toBeInTheDocument();
  });

  it("未归属项目总数为 0 时隐藏入口", async () => {
    installChatFetch({ unboundTotal: 0 });

    renderApp("/chat");

    await screen.findByRole("button", {
      name: `项目 ${PROJECT_A.name}`,
    });
    expect(
      screen.queryByRole("button", { name: "未归属项目" }),
    ).not.toBeInTheDocument();
  });

  it("未归属项目有总数时才显示，并在展开后加载真实会话", async () => {
    const unboundSession = makeSession({
      id: "session-unbound",
      project_id: null,
      title: "未归属真实会话",
    });
    installChatFetch({
      unboundTotal: 1,
      unboundSessions: [unboundSession],
    });
    const user = userEvent.setup();

    renderApp("/chat");

    const unboundButton = await screen.findByRole("button", {
      name: "未归属项目",
    });
    expect(screen.queryByText("未归属真实会话")).not.toBeInTheDocument();
    await user.click(unboundButton);
    expect(await screen.findByText("未归属真实会话")).toBeInTheDocument();
    expect(unboundButton).toHaveAttribute("aria-expanded", "true");
  });

  it("最近列表只展示接口返回的 8 条真实会话，空标题统一命名", async () => {
    const recent = Array.from({ length: 9 }, (_, index) =>
      makeSession({
        id: `recent-${index}`,
        project_id:
          index % 2 === 0 ? PROJECT_A.id : null,
        title: index === 0 ? null : `真实最近会话 ${index}`,
      }),
    ).slice(0, 8);
    installChatFetch({ recent, unboundTotal: 0 });

    renderApp("/chat");

    const recentSection = await screen.findByRole("region", {
      name: "最近",
    });
    await waitFor(() => {
      expect(within(recentSection).getAllByRole("link")).toHaveLength(8);
    });
    expect(
      within(recentSection).getByText("未命名对话"),
    ).toBeInTheDocument();
    expect(
      within(recentSection).getAllByText(/未归属项目/).length,
    ).toBeGreaterThan(0);
    expect(
      within(recentSection).queryByText("示例会话"),
    ).not.toBeInTheDocument();
  });

  it("活动 Session 所属项目默认展开", async () => {
    installChatFetch({
      activeSession: makeSession({
        id: "session-active",
        title: "当前会话",
      }),
      projectSessions: {
        [PROJECT_A.id]: [
          makeSession({ id: "session-active", title: "当前会话" }),
        ],
      },
    });

    renderApp("/chat/session-active");

    const projectButton = await screen.findByRole("button", {
      name: `项目 ${PROJECT_A.name}`,
    });
    await waitFor(() => {
      expect(projectButton).toHaveAttribute("aria-expanded", "true");
    });
    expect(
      await screen.findByRole("link", { name: "新建对话" }),
    ).toHaveAttribute(
      "href",
      `/chat/new?project=${PROJECT_A.id}`,
    );
    expect(
      await screen.findByRole("link", { name: "当前会话" }),
    ).toBeInTheDocument();
    const mainNavigation = screen.getByRole("navigation", {
      name: "主导航",
    });
    expect(
      within(mainNavigation).getByRole("link", { name: "对话" }),
    ).toHaveAttribute("aria-current", "page");
  });
});

describe("窄屏工作区目录", () => {
  it.each([920, 390])(
    "在 %ipx 视口可打开目录、访问未归属与最近记录，并在关闭后恢复主内容",
    async (width) => {
      window.innerWidth = width;
      const unboundSession = makeSession({
        id: "session-unbound",
        project_id: null,
        title: "窄屏未归属会话",
      });
      const recentSession = makeSession({
        id: "session-recent",
        project_id: null,
        title: "窄屏最近会话",
      });
      installChatFetch({
        recent: [recentSession],
        unboundSessions: [unboundSession],
        unboundTotal: 1,
      });
      const user = userEvent.setup();

      renderApp("/chat");

      await screen.findByRole("heading", { name: "对话工作台" });
      const trigger = screen.getByRole("button", {
        name: "工作区目录",
      });
      expect(trigger).toHaveAttribute("aria-expanded", "false");
      expect(trigger).toHaveAttribute(
        "aria-controls",
        "mobile-workspace-directory",
      );

      await user.click(trigger);
      const dialog = screen.getByRole("dialog", {
        name: "工作区目录",
      });
      expect(trigger).toHaveAttribute("aria-expanded", "true");
      expect(
        within(dialog).getByRole("link", {
          name: /窄屏最近会话/,
        }),
      ).toBeInTheDocument();

      await user.click(
        within(dialog).getByRole("button", {
          name: "未归属项目",
        }),
      );
      expect(
        await within(dialog).findByRole("link", {
          name: "窄屏未归属会话",
        }),
      ).toBeInTheDocument();

      await user.click(
        within(dialog).getByRole("button", {
          name: "关闭弹窗",
        }),
      );
      expect(
        screen.queryByRole("dialog", { name: "工作区目录" }),
      ).not.toBeInTheDocument();
      await user.type(
        screen.getByLabelText("对话草稿"),
        `目录关闭后可编辑 ${width}`,
      );
      expect(screen.getByLabelText("对话草稿")).toHaveValue(
        `目录关闭后可编辑 ${width}`,
      );
    },
  );

  it("目录内打开新建项目后，Escape 只关闭内层弹窗并保留目录", async () => {
    window.innerWidth = 390;
    installChatFetch({});
    const user = userEvent.setup();

    renderApp("/chat");

    await user.click(
      await screen.findByRole("button", { name: "工作区目录" }),
    );
    const directory = screen.getByRole("dialog", {
      name: "工作区目录",
    });
    const newProjectTrigger = within(directory).getByRole("button", {
      name: "新建项目",
    });
    await user.click(newProjectTrigger);
    expect(
      screen.getByRole("dialog", { name: "新建项目" }),
    ).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(
      screen.queryByRole("dialog", { name: "新建项目" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("dialog", { name: "工作区目录" }),
    ).toBeInTheDocument();
    await waitFor(() => expect(newProjectTrigger).toHaveFocus());
  });
});

describe("新对话草稿", () => {
  it("目录异步重排或缩短时仍按项目 ID 选择有效活动项", async () => {
    let directoryProjects = [PROJECT_A, PROJECT_B];
    const fetchMock = installChatFetch();
    const defaultFetch = fetchMock.getMockImplementation();
    fetchMock.mockImplementation(async (input, init) => {
      const url = new URL(getRequestUrl(input), "http://localhost");
      if (url.pathname === "/api/v1/projects" && (!init?.method || init.method === "GET")) {
        return jsonResponse(
          listResponse(directoryProjects, 1, 100, directoryProjects.length),
        );
      }
      return defaultFetch!(input, init);
    });
    const user = userEvent.setup();
    const view = renderApp(`/chat/new?project=${PROJECT_A.id}`);

    const trigger = await screen.findByRole("combobox", { name: "更改项目" });
    await waitFor(() => expectSelectedProject(PROJECT_A.id));
    await user.click(trigger);
    await user.keyboard("{ArrowDown}");
    await user.keyboard("{ArrowDown}");
    expect(
      screen.getByRole("option", { name: new RegExp(PROJECT_B.name) }),
    ).toHaveAttribute("data-active", "true");

    directoryProjects = [PROJECT_B, PROJECT_A];
    await view.client.invalidateQueries({ queryKey: projectKeys.directory() });
    await waitFor(() => {
      expect(
        screen.getByRole("option", { name: new RegExp(PROJECT_B.name) }),
      ).toHaveAttribute("data-active", "true");
    });
    await user.keyboard("{Enter}");
    expectSelectedProject(PROJECT_B.id);

    await chooseProject(user, PROJECT_A.id);
    await user.click(trigger);
    await user.keyboard("{ArrowDown}");
    await user.keyboard("{ArrowDown}");
    directoryProjects = [PROJECT_A];
    await view.client.invalidateQueries({ queryKey: projectKeys.directory() });
    await waitFor(() => {
      expect(screen.queryByRole("option", { name: new RegExp(PROJECT_B.name) })).not.toBeInTheDocument();
      expect(
        screen.getByRole("option", { name: new RegExp(PROJECT_A.name) }),
      ).toHaveAttribute("data-active", "true");
    });
    await user.keyboard("{Enter}");
    expectSelectedProject(PROJECT_A.id);
  });

  it("项目菜单支持键盘选择、Escape 关闭和点击外部关闭", async () => {
    installChatFetch({});
    const user = userEvent.setup();
    renderApp("/chat");

    const trigger = await screen.findByRole("combobox", { name: "更改项目" });
    await waitFor(() => expect(trigger).not.toBeDisabled());
    trigger.focus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("listbox", { name: "项目" })).toBeInTheDocument();
    await user.keyboard("{ArrowDown}");
    expect(
      screen.getByRole("option", { name: new RegExp(PROJECT_A.name) }),
    ).toHaveAttribute("data-active", "true");
    await user.keyboard("{End}");
    await user.keyboard("{Enter}");
    expectSelectedProject(PROJECT_B.id);
    expect(trigger).toHaveFocus();

    await user.keyboard("{ArrowDown}");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("listbox", { name: "项目" })).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();

    await user.click(trigger);
    await user.click(screen.getByLabelText("对话草稿"));
    expect(screen.queryByRole("listbox", { name: "项目" })).not.toBeInTheDocument();
  });

  it("无效项目安全回退为未归属，且不创建 Session", async () => {
    const fetchMock = installChatFetch({});
    fetchMock.mockImplementation(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/projects") {
          return jsonResponse(
            listResponse([PROJECT_A], 1, 100, 1),
          );
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

    renderApp("/chat/new?project=missing");

    expect(
      await screen.findByText("该项目不可访问，已切换为未归属项目。"),
    ).toBeInTheDocument();
    expectSelectedProject("");
    expect(
      fetchMock.mock.calls.some(
        ([input, init]) =>
          new URL(getRequestUrl(input), "http://localhost").pathname ===
            "/api/v1/sessions" && init?.method === "POST",
      ),
    ).toBe(false);
  });

  it("项目选择只留在草稿，输入后可发送且说明真实创建边界", async () => {
    installChatFetch({
      projectSessions: {
        [PROJECT_A.id]: [],
      },
    });
    const user = userEvent.setup();

    renderApp("/chat");

    await screen.findByRole("heading", { name: "对话工作台" });
    await chooseProject(user, PROJECT_A.id);
    await user.type(screen.getByLabelText("对话草稿"), "这是一段本地草稿");

    expectSelectedProject(PROJECT_A.id);
    expect(screen.getByText("这是一段本地草稿")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "新建对话" }),
    ).toHaveAttribute(
      "href",
      `/chat/new?project=${PROJECT_A.id}`,
    );
    const sendButton = screen.getByRole("button", { name: "发送消息" });
    expect(sendButton).toBeEnabled();
    expect(
      screen.getByText("发送时才创建会话；Run 提交使用幂等请求标识。"),
    ).toBeInTheDocument();
  });

  it("项目 A 改选 B 后新建继承 B，返回时恢复来源草稿与手动项目", async () => {
    installChatFetch({});
    const user = userEvent.setup();

    const view = renderApp(`/chat/new?project=${PROJECT_A.id}`);

    const select = await screen.findByRole("combobox", { name: "更改项目" });
    await waitFor(() => {
      expectSelectedProject(PROJECT_A.id);
    });
    await chooseProject(user, PROJECT_B.id);
    const newChatLink = screen.getByRole("link", {
      name: "新建对话",
    });
    await waitFor(() => {
      expect(newChatLink).toHaveAttribute(
        "href",
        `/chat/new?project=${PROJECT_B.id}`,
      );
    });
    await user.type(screen.getByLabelText("对话草稿"), "旧项目草稿");
    await user.click(newChatLink);

    await waitFor(() => {
      expect(view.getLocation()).toEqual({
        pathname: "/chat/new",
        search: `?project=${PROJECT_B.id}`,
      });
    });
    expectSelectedProject(PROJECT_B.id);
    expect(screen.getByLabelText("对话草稿")).toHaveValue("");
    expect(
      screen.getByRole("heading", { name: "对话工作台" }),
    ).toBeInTheDocument();

    view.goBack();
    await waitFor(() => {
      expect(view.getLocation()).toEqual({
        pathname: "/chat/new",
        search: `?project=${PROJECT_A.id}`,
      });
    });
    await waitFor(() => {
      expectSelectedProject(PROJECT_B.id);
      expect(screen.getByLabelText("对话草稿")).toHaveValue(
        "旧项目草稿",
      );
    });
  });

  it("跨页面从项目 B 开始对话时不会带入项目 A 的旧草稿", async () => {
    installChatFetch({});
    const user = userEvent.setup();

    const view = renderApp(`/chat/new?project=${PROJECT_A.id}`);

    const select = await screen.findByRole("combobox", { name: "更改项目" });
    await waitFor(() => {
      expectSelectedProject(PROJECT_A.id);
    });
    await user.type(screen.getByLabelText("对话草稿"), "项目 A 的旧草稿");

    view.navigateTo(`/projects/${PROJECT_B.id}/overview`);
    await screen.findByRole("heading", { name: PROJECT_B.name });
    const startLinks = await screen.findAllByRole("link", {
      name: "开始对话",
    });
    await user.click(startLinks[0]);

    await waitFor(() => {
      expect(view.getLocation()).toEqual({
        pathname: "/chat/new",
        search: `?project=${PROJECT_B.id}`,
      });
    });
    expectSelectedProject(PROJECT_B.id);
    expect(screen.getByLabelText("对话草稿")).toHaveValue("");
  });

  it("浏览器返回原草稿历史项时恢复对应草稿，而不是当前项目草稿", async () => {
    installChatFetch({});
    const user = userEvent.setup();

    const view = renderApp(`/chat/new?project=${PROJECT_A.id}`);

    const initialSelect = await screen.findByRole("combobox", { name: "更改项目" });
    await waitFor(() => {
      expectSelectedProject(PROJECT_A.id);
    });
    await user.type(screen.getByLabelText("对话草稿"), "项目 A 草稿");

    view.navigateTo(`/projects/${PROJECT_B.id}/overview`);
    await screen.findByRole("heading", { name: PROJECT_B.name });
    const startLinks = await screen.findAllByRole("link", {
      name: "开始对话",
    });
    await user.click(startLinks[0]);
    await waitFor(() => {
      expect(view.getLocation()).toEqual({
        pathname: "/chat/new",
        search: `?project=${PROJECT_B.id}`,
      });
    });
    await user.type(screen.getByLabelText("对话草稿"), "项目 B 草稿");

    view.goBack();
    await waitFor(() => {
      expect(view.getLocation()).toEqual({
        pathname: `/projects/${PROJECT_B.id}/overview`,
        search: "",
      });
    });
    view.goBack();
    await waitFor(() => {
      expect(view.getLocation()).toEqual({
        pathname: "/chat/new",
        search: `?project=${PROJECT_A.id}`,
      });
    });
    await waitFor(() => {
      expectSelectedProject(PROJECT_A.id);
      expect(screen.getByLabelText("对话草稿")).toHaveValue(
        "项目 A 草稿",
      );
    });
  });

  it("改为未归属后新建进入未归属草稿", async () => {
    installChatFetch({});
    const user = userEvent.setup();

    const view = renderApp(`/chat/new?project=${PROJECT_A.id}`);

    const select = await screen.findByRole("combobox", { name: "更改项目" });
    await waitFor(() => {
      expectSelectedProject(PROJECT_A.id);
    });
    await chooseProject(user, "");
    expect(
      screen.getByRole("link", { name: "新建对话" }),
    ).toHaveAttribute("href", "/chat");
    await user.click(
      screen.getByRole("link", { name: "新建对话" }),
    );

    await waitFor(() => {
      expect(view.getLocation()).toEqual({
        pathname: "/chat",
        search: "",
      });
    });
    expectSelectedProject("");
    expect(
      screen.getByText("这次对话不会归入任何项目。"),
    ).toBeInTheDocument();
  });

  it("从 /chat 选择项目后新建会导航到该项目，并支持同 URL 再次重置", async () => {
    installChatFetch({});
    const user = userEvent.setup();

    const view = renderApp("/chat");

    await screen.findByRole("heading", { name: "对话工作台" });
    await waitFor(() => {
      expect(screen.getByRole("combobox", { name: "更改项目" })).not.toBeDisabled();
    });
    await chooseProject(user, PROJECT_A.id);
    await user.type(screen.getByLabelText("对话草稿"), "第一次草稿");
    await user.click(
      screen.getByRole("link", { name: "新建对话" }),
    );

    await waitFor(() => {
      expect(view.getLocation()).toEqual({
        pathname: "/chat/new",
        search: `?project=${PROJECT_A.id}`,
      });
    });
    expect(screen.getByLabelText("对话草稿")).toHaveValue("");
    expectSelectedProject(PROJECT_A.id);

    await user.type(screen.getByLabelText("对话草稿"), "第二次草稿");
    await user.click(
      screen.getByRole("link", { name: "新建对话" }),
    );
    expect(screen.getByLabelText("对话草稿")).toHaveValue("");
    expect(view.getLocation()).toEqual({
      pathname: "/chat/new",
      search: `?project=${PROJECT_A.id}`,
    });
  });

  it("从会话页新建后返回仍恢复来源草稿历史项", async () => {
    const activeSession = makeSession({
      id: "session-active",
      title: "来源会话",
    });
    installChatFetch({
      activeSession,
      projectSessions: {
        [PROJECT_A.id]: [activeSession],
      },
    });
    const user = userEvent.setup();

    const view = renderApp(`/chat/new?project=${PROJECT_A.id}`);

    await waitFor(() => {
      expectSelectedProject(PROJECT_A.id);
    });
    await user.type(screen.getByLabelText("对话草稿"), "会话页返回草稿");

    view.navigateTo(`/chat/${activeSession.id}`);
    await screen.findByRole("heading", { name: "来源会话" });
    await user.click(
      screen.getByRole("link", { name: "新建对话" }),
    );
    await waitFor(() => {
      expect(view.getLocation()).toEqual({
        pathname: "/chat/new",
        search: `?project=${PROJECT_A.id}`,
      });
    });
    expect(screen.getByLabelText("对话草稿")).toHaveValue("");

    view.goBack();
    await waitFor(() => {
      expect(view.getLocation()).toEqual({
        pathname: `/chat/${activeSession.id}`,
        search: "",
      });
    });
    view.goBack();
    await waitFor(() => {
      expect(view.getLocation()).toEqual({
        pathname: "/chat/new",
        search: `?project=${PROJECT_A.id}`,
      });
      expect(screen.getByLabelText("对话草稿")).toHaveValue(
        "会话页返回草稿",
      );
    });
  });

  it("同 URL /chat 新建会创建独立历史项，返回后恢复旧草稿", async () => {
    installChatFetch({});
    const user = userEvent.setup();

    const view = renderApp("/chat");

    await screen.findByRole("heading", { name: "对话工作台" });
    await user.type(screen.getByLabelText("对话草稿"), "同 URL 旧草稿");
    await user.click(
      screen.getByRole("link", { name: "新建对话" }),
    );
    expect(view.getLocation()).toEqual({
      pathname: "/chat",
      search: "",
    });
    expect(screen.getByLabelText("对话草稿")).toHaveValue("");

    view.goBack();
    await waitFor(() => {
      expect(screen.getByLabelText("对话草稿")).toHaveValue(
        "同 URL 旧草稿",
      );
    });
  });
});

