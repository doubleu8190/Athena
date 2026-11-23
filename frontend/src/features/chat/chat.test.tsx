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
const SIDEBAR_COLLAPSED_STORAGE_KEY = "athena.shell.sidebar-collapsed";

beforeEach(() => {
  window.localStorage.removeItem(SIDEBAR_COLLAPSED_STORAGE_KEY);
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
        if (pageSize === 8 || pageSize === 30) {
          const q = url.searchParams.get("q")?.trim().toLowerCase() ?? "";
          const matched = q
            ? recent.filter((item) =>
                (item.title ?? "").toLowerCase().includes(q),
              )
            : recent;
          return jsonResponse(
            listResponse(matched, 1, pageSize, matched.length),
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

describe("单层侧栏会话历史与项目上下文", () => {
  it("会话历史展示接口返回的真实会话，空标题统一显示未命名对话", async () => {
    const now = new Date();
    const recent = Array.from({ length: 4 }, (_, index) =>
      makeSession({
        id: `recent-${index}`,
        project_id: index % 2 === 0 ? PROJECT_A.id : null,
        title: index === 0 ? null : `真实最近会话 ${index}`,
        updated_at: new Date(now.getTime() - index * 60_000).toISOString(),
      }),
    );
    installChatFetch({ recent });

    renderApp("/chat");

    const sidebar = await screen.findByRole("complementary", {
      name: "工作区侧栏",
    });
    expect(
      await within(sidebar).findByRole("link", { name: "未命名对话" }),
    ).toBeInTheDocument();
    expect(
      within(sidebar).getByRole("link", { name: "真实最近会话 1" }),
    ).toBeInTheDocument();
    expect(
      within(sidebar).queryByText("示例会话"),
    ).not.toBeInTheDocument();
  });

  it("活动 Session 在侧栏高亮且新建对话继承其所属项目", async () => {
    const activeSession = makeSession({
      id: "session-active",
      project_id: PROJECT_A.id,
      title: "当前会话",
      updated_at: new Date().toISOString(),
    });
    installChatFetch({
      activeSession,
      recent: [activeSession],
    });

    renderApp("/chat/session-active");

    const activeLink = await screen.findByRole("link", { name: "当前会话" });
    expect(activeLink).toHaveAttribute("aria-current", "page");
    await waitFor(() => {
      expect(
        screen.getByRole("link", { name: "新建对话" }),
      ).toHaveAttribute("href", `/chat/new?project=${PROJECT_A.id}`);
    });
  });

  it("项目分区可折叠收起并展示最近项目与查看全部入口", async () => {
    installChatFetch({});
    const user = userEvent.setup();

    renderApp("/chat");

    const nav = await screen.findByRole("navigation", { name: "主导航" });
    expect(
      await within(nav).findByRole("link", { name: PROJECT_A.name }),
    ).toHaveAttribute("href", `/projects/${PROJECT_A.id}/overview`);
    expect(
      within(nav).getByRole("link", { name: "查看全部项目 →" }),
    ).toHaveAttribute("href", "/projects");

    const toggle = within(nav).getByRole("button", { name: "项目" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(
      within(nav).queryByRole("link", { name: PROJECT_A.name }),
    ).not.toBeInTheDocument();
  });
});

describe("窄屏移动导航抽屉", () => {
  it.each([920, 390])(
    "在 %ipx 视口可打开左侧 Sheet 抽屉、访问历史会话并在关闭后恢复主内容编辑",
    async (width) => {
      window.innerWidth = width;
      const recentSession = makeSession({
        id: "session-recent",
        project_id: null,
        title: "窄屏最近会话",
        updated_at: new Date().toISOString(),
      });
      installChatFetch({
        recent: [recentSession],
      });
      const user = userEvent.setup();

      renderApp("/chat");

      await screen.findByRole("heading", { name: "对话工作台" });
      const trigger = screen.getByRole("button", {
        name: "打开导航菜单",
      });
      expect(trigger).toHaveAttribute("aria-expanded", "false");
      expect(trigger).toHaveAttribute(
        "aria-controls",
        "mobile-navigation-drawer",
      );

      await user.click(trigger);
      const drawer = screen.getByRole("dialog", {
        name: "工作区导航",
      });
      expect(trigger).toHaveAttribute("aria-expanded", "true");
      expect(
        await within(drawer).findByRole("link", {
          name: "窄屏最近会话",
        }),
      ).toBeInTheDocument();

      await user.click(
        within(drawer).getByRole("button", {
          name: "关闭",
        }),
      );
      await waitFor(() => {
        expect(
          screen.queryByRole("dialog", { name: "工作区导航" }),
        ).not.toBeInTheDocument();
      });
      await user.type(
        screen.getByLabelText("对话草稿"),
        `抽屉关闭后可编辑 ${width}`,
      );
      expect(screen.getByLabelText("对话草稿")).toHaveValue(
        `抽屉关闭后可编辑 ${width}`,
      );
    },
  );

  it("抽屉内打开新建项目后可正常输入，Escape 只关闭内层弹窗并恢复抽屉触发钮焦点", async () => {
    window.innerWidth = 390;
    installChatFetch({});
    const user = userEvent.setup();

    renderApp("/chat");

    await user.click(
      await screen.findByRole("button", { name: "打开导航菜单" }),
    );
    const drawer = screen.getByRole("dialog", {
      name: "工作区导航",
    });
    const newProjectTrigger = within(drawer).getByRole("button", {
      name: "新建项目",
    });
    await user.click(newProjectTrigger);
    const createDialog = screen.getByRole("dialog", { name: "新建项目" });
    const nameInput = within(createDialog).getByLabelText("项目名称");
    await waitFor(() => expect(nameInput).toHaveFocus());
    await user.type(nameInput, "抽屉内新建项目");
    expect(nameInput).toHaveValue("抽屉内新建项目");

    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", { name: "新建项目" }),
      ).not.toBeInTheDocument();
    });
    expect(
      screen.getByRole("dialog", { name: "工作区导航" }),
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

    await screen.findByRole("combobox", { name: "更改项目" });
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

    await screen.findByRole("combobox", { name: "更改项目" });
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

    await screen.findByRole("combobox", { name: "更改项目" });
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

    await screen.findByRole("combobox", { name: "更改项目" });
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

