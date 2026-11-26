import {
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { makeSkill } from "../../test/adminFixtures";
import {
  getRequestUrl,
  listResponse,
  makeSession,
  PROJECT_A,
  PROJECT_B,
  TEST_USER,
} from "../../test/fixtures";
import { jsonResponse, renderApp } from "../../test/testUtils";
import type { Project } from "./model";
import { projectKeys } from "./queryKeys";

const PROJECT_SESSION = makeSession({
  id: "project-session",
  title: "项目真实会话",
});

const SKILL_FIN = makeSkill({
  description: "按财务模板输出周报",
  enabled: true,
  id: "skill-fin",
  name: "财务分析助手",
  scope: "company",
  slug: "fin-helper",
});

const SKILL_LEGAL = makeSkill({
  description: "合同条款风险核查",
  enabled: true,
  id: "skill-legal",
  name: "法务审阅",
  scope: "department",
  slug: "legal-review",
});

interface WorkspaceMockOptions {
  deleteResponse?: (url: URL) => Response;
  project?: Project;
  projects?: Project[];
  sessions?: ReturnType<typeof makeSession>[];
  sessionsTotal?: number;
  skills?: ReturnType<typeof makeSkill>[];
}

function installProjectWorkspaceFetch(options: WorkspaceMockOptions = {}) {
  let currentProject: Project = options.project ?? {
    ...PROJECT_A,
    default_skill_ids: [],
    description: "季度增长专项资料整理",
    instructions: "先输出核心指标变化，再分析原因。",
  };
  let projectList: Project[] = options.projects ?? [currentProject, PROJECT_B];
  const sessions = options.sessions ?? [PROJECT_SESSION];
  const skills = options.skills ?? [SKILL_FIN, SKILL_LEGAL];
  const patchPayloads: Array<Record<string, unknown>> = [];
  const deleteUrls: string[] = [];

  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(getRequestUrl(input), "http://localhost");
      const method = init?.method ?? "GET";

      if (url.pathname.endsWith("/auth/me") && method === "GET") {
        return jsonResponse(TEST_USER);
      }
      if (url.pathname === "/api/v1/projects" && method === "GET") {
        return jsonResponse(
          listResponse(projectList, 1, 100, projectList.length),
        );
      }
      if (url.pathname === `/api/v1/projects/${currentProject.id}`) {
        if (method === "GET") {
          return jsonResponse(currentProject);
        }
        if (method === "PATCH") {
          const body = JSON.parse(String(init?.body)) as Record<
            string,
            unknown
          >;
          patchPayloads.push(body);
          currentProject = {
            ...currentProject,
            ...(Object.hasOwn(body, "name")
              ? { name: body.name as string }
              : {}),
            ...(Object.hasOwn(body, "description")
              ? { description: body.description as string | null }
              : {}),
            ...(Object.hasOwn(body, "instructions")
              ? { instructions: body.instructions as string | null }
              : {}),
            ...(Object.hasOwn(body, "default_skill_ids")
              ? { default_skill_ids: body.default_skill_ids as string[] }
              : {}),
            updated_at: "2026-11-24T06:00:00Z",
          };
          projectList = projectList.map((item) =>
            item.id === currentProject.id ? currentProject : item,
          );
          return jsonResponse(currentProject);
        }
        if (method === "DELETE") {
          deleteUrls.push(`${url.pathname}${url.search}`);
          if (options.deleteResponse) {
            return options.deleteResponse(url);
          }
          projectList = projectList.filter(
            (item) => item.id !== currentProject.id,
          );
          return new Response(null, { status: 204 });
        }
      }
      if (url.pathname === "/api/v1/sessions" && method === "GET") {
        const pageSize = Number(url.searchParams.get("page_size") ?? 10);
        const page = Number(url.searchParams.get("page") ?? 1);
        const q = url.searchParams.get("q")?.trim().toLowerCase() ?? "";
        const projectId = url.searchParams.get("project_id");
        if (projectId === currentProject.id) {
          const matched = q
            ? sessions.filter((item) =>
                (item.title ?? "").toLowerCase().includes(q),
              )
            : sessions;
          const total = options.sessionsTotal ?? matched.length;
          return jsonResponse(listResponse(matched, page, pageSize, total));
        }
        return jsonResponse(listResponse([], 1, pageSize, 0));
      }
      if (
        url.pathname ===
          `/api/v1/projects/${currentProject.id}/knowledge-bases` &&
        method === "GET"
      ) {
        return jsonResponse([]);
      }
      if (url.pathname === "/api/v1/skills" && method === "GET") {
        return jsonResponse({ items: skills });
      }
      throw new Error(`Unexpected request: ${method} ${url}`);
    },
  );

  vi.stubGlobal("fetch", fetchMock);
  return { deleteUrls, fetchMock, patchPayloads };
}

describe("项目中心与单页双栏工作文件夹", () => {
  it("项目目录页展示 3 列文件夹卡片、简介、指令/默认技能徽章，链接指向 /projects/:id", async () => {
    installProjectWorkspaceFetch({
      project: {
        ...PROJECT_A,
        default_skill_ids: ["skill-fin", "skill-legal"],
        description: "年度增长战略资料与分析",
        instructions: "输出采用结论先行",
      },
    });

    renderApp("/projects");

    await screen.findByRole("heading", { level: 1, name: "项目" });
    const main = screen.getByRole("main");
    const projectLink = await within(main).findByRole("link", {
      name: new RegExp(PROJECT_A.name),
    });
    expect(projectLink).toHaveAttribute("href", `/projects/${PROJECT_A.id}`);
    expect(within(main).getByText("年度增长战略资料与分析")).toBeInTheDocument();
    expect(within(main).getByText("已配置专属指令")).toBeInTheDocument();
    expect(within(main).getByText("默认技能 2")).toBeInTheDocument();
  });

  it("项目工作文件夹 /projects/:id?page=2 按查询参数分页加载项目对话", async () => {
    const { fetchMock } = installProjectWorkspaceFetch({
      sessions: [makeSession({ id: "s-p2", title: "第二页项目对话" })],
      sessionsTotal: 15,
    });

    const app = renderApp(`/projects/${PROJECT_A.id}?page=2`);

    expect(
      await screen.findByRole("heading", { level: 1, name: PROJECT_A.name }),
    ).toBeInTheDocument();
    expect(app.getLocation()).toEqual({
      pathname: `/projects/${PROJECT_A.id}`,
      search: "?page=2",
    });
    expect(await screen.findByText("第二页项目对话")).toBeInTheDocument();
    expect(screen.getByText("第 2 / 2 页")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([input]) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        return (
          url.pathname === "/api/v1/sessions" &&
          url.searchParams.get("project_id") === PROJECT_A.id &&
          url.searchParams.get("page") === "2"
        );
      }),
    ).toBe(true);
  });

  it("项目 404 显示明确不可访问状态并返回项目中心", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/projects") {
          return jsonResponse(listResponse([PROJECT_A], 1, 100, 1));
        }
        if (url.pathname === "/api/v1/projects/missing") {
          return jsonResponse({ detail: "project_not_found" }, 404);
        }
        if (url.pathname === "/api/v1/sessions") {
          return jsonResponse(listResponse([]));
        }
        throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
      }),
    );

    renderApp("/projects/missing");

    expect(
      await screen.findByRole("heading", { name: "项目不存在或不可访问" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "返回项目中心" }),
    ).toHaveAttribute("href", "/projects");
  });

  it("单页工作文件夹支持编辑名称/清空简介、编辑/清空专属指令，以及配置与移除不可用默认技能", async () => {
    const { patchPayloads } = installProjectWorkspaceFetch({
      project: {
        ...PROJECT_A,
        default_skill_ids: ["skill-stale-removed"],
        description: "旧项目简介",
        instructions: "旧专属指令",
      },
    });
    const user = userEvent.setup();

    renderApp(`/projects/${PROJECT_A.id}`);

    // 1. 顶部编辑项目信息：改名并清空简介（显式传 description: null）。
    await screen.findByRole("heading", { level: 1, name: PROJECT_A.name });
    expect(screen.getByText("旧项目简介")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "编辑信息" }));
    const editDialog = screen.getByRole("dialog", { name: "编辑项目信息" });
    const nameInput = within(editDialog).getByLabelText("项目名称");
    const descInput = within(editDialog).getByLabelText("项目简介");
    await user.clear(nameInput);
    await user.type(nameInput, "新增长文件夹");
    await user.clear(descInput);
    await user.click(within(editDialog).getByRole("button", { name: "保存信息" }));

    expect(
      await screen.findByRole("heading", { level: 1, name: "新增长文件夹" }),
    ).toBeInTheDocument();
    expect(patchPayloads[0]).toEqual({
      description: null,
      name: "新增长文件夹",
    });

    // 2. 专属背景指令：编辑保存后再清空（显式传 instructions: null）。
    const instructionsTextarea = screen.getByRole("textbox", {
      name: "专属背景指令",
    });
    expect(instructionsTextarea).toHaveValue("旧专属指令");
    const saveInstructionsBtn = screen.getByRole("button", { name: "保存指令" });
    expect(saveInstructionsBtn).toBeDisabled();

    await user.clear(instructionsTextarea);
    await user.type(instructionsTextarea, "所有表格按季度汇总");
    expect(saveInstructionsBtn).toBeEnabled();
    await user.click(saveInstructionsBtn);

    await waitFor(() => {
      expect(patchPayloads[1]).toEqual({
        instructions: "所有表格按季度汇总",
      });
    });

    await user.clear(instructionsTextarea);
    await user.click(saveInstructionsBtn);
    await waitFor(() => {
      expect(patchPayloads[2]).toEqual({
        instructions: null,
      });
    });

    // 3. 默认挂载技能：不可用旧技能标灰展示且可移除，Popover 搜索并勾选新技能后保存。
    expect(
      await screen.findByText("skill-stale-removed（当前不可用）"),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", {
        name: "移除默认技能：skill-stale-removed",
      }),
    );

    await user.click(screen.getByRole("button", { name: "选择默认技能" }));
    const searchSkillInput = await screen.findByLabelText("搜索可用技能");
    await user.type(searchSkillInput, "财务");
    expect(screen.getByText("财务分析助手")).toBeInTheDocument();
    expect(screen.queryByText("法务审阅")).not.toBeInTheDocument();

    await user.click(screen.getByText("财务分析助手"));
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "保存默认技能" }));

    await waitFor(() => {
      expect(patchPayloads[3]).toEqual({
        default_skill_ids: ["skill-fin"],
      });
    });
  });
});

describe("新建项目（支持可选简介）", () => {
  function installCreateFetch(postResponse: () => Response) {
    const createdProjects: Project[] = [];
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/projects" && init?.method === "POST") {
          const body = JSON.parse(String(init.body)) as {
            description?: string;
            name: string;
          };
          const response = postResponse();
          if (response.status === 201) {
            createdProjects.push({
              ...PROJECT_A,
              description: body.description ?? null,
              id: "created-project",
              name: body.name,
            });
          }
          return response;
        }
        if (url.pathname === "/api/v1/projects") {
          return jsonResponse(
            listResponse(
              createdProjects,
              1,
              100,
              createdProjects.length,
            ),
          );
        }
        if (url.pathname === "/api/v1/projects/created-project") {
          return jsonResponse(
            createdProjects[0] ?? {
              ...PROJECT_A,
              id: "created-project",
              name: "创建项目",
            },
          );
        }
        if (url.pathname === "/api/v1/projects/created-project/knowledge-bases") {
          return jsonResponse([]);
        }
        if (url.pathname === "/api/v1/skills") {
          return jsonResponse({ items: [] });
        }
        if (url.pathname === "/api/v1/sessions") {
          return jsonResponse(listResponse([]));
        }
        throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  it("空名称和 121 个 Unicode 码点被拦截，120 个码点与可选简介成功创建并跳至 /projects/:id", async () => {
    const fetchMock = installCreateFetch(() =>
      jsonResponse(
        {
          ...PROJECT_A,
          description: "季度复盘材料",
          id: "created-project",
          name: "😀".repeat(120),
        },
        201,
      ),
    );
    const user = userEvent.setup();

    const app = renderApp("/projects");
    await screen.findByRole("heading", { level: 1, name: "项目" });
    const main = screen.getByRole("main");
    const openButtons = await within(main).findAllByRole("button", {
      name: "新建项目",
    });
    await user.click(openButtons[0]);

    const dialog = screen.getByRole("dialog", { name: "新建项目" });
    await user.click(
      within(dialog).getByRole("button", { name: "创建项目" }),
    );
    expect(within(dialog).getByText("请输入项目名称。")).toBeInTheDocument();

    const nameInput = within(dialog).getByLabelText("项目名称");
    fireEvent.change(nameInput, { target: { value: "😀".repeat(121) } });
    await user.click(
      within(dialog).getByRole("button", { name: "创建项目" }),
    );
    expect(
      within(dialog).getByText(
        "项目名称需为 1–120 个字符，且不能包含控制字符。",
      ),
    ).toBeInTheDocument();

    fireEvent.change(nameInput, { target: { value: "😀".repeat(120) } });
    await user.type(within(dialog).getByLabelText("项目简介"), "季度复盘材料");
    await user.click(
      within(dialog).getByRole("button", { name: "创建项目" }),
    );

    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "😀".repeat(120),
      }),
    ).toBeInTheDocument();
    expect(app.getLocation().pathname).toBe("/projects/created-project");
    const postCall = fetchMock.mock.calls.find(
      ([request, init]) =>
        new URL(getRequestUrl(request), "http://localhost").pathname ===
          "/api/v1/projects" && init?.method === "POST",
    );
    expect(JSON.parse(String(postCall?.[1]?.body))).toEqual({
      description: "季度复盘材料",
      name: "😀".repeat(120),
    });
  });

  it("服务端失败保留弹窗并在弹窗内显示 InlineAlert", async () => {
    installCreateFetch(() =>
      jsonResponse({ detail: "project_name_invalid" }, 422),
    );
    const user = userEvent.setup();

    renderApp("/projects");
    await screen.findByRole("heading", { level: 1, name: "项目" });
    const main = screen.getByRole("main");
    const openButtons = await within(main).findAllByRole("button", {
      name: "新建项目",
    });
    await user.click(openButtons[0]);
    const dialog = screen.getByRole("dialog", { name: "新建项目" });
    await user.type(within(dialog).getByLabelText("项目名称"), "有效名称");
    await user.click(
      within(dialog).getByRole("button", { name: "创建项目" }),
    );

    expect(
      await within(dialog).findByText(
        "项目名称需为 1–120 个字符，且不能包含控制字符。",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("dialog", { name: "新建项目" }),
    ).toBeInTheDocument();
  });
});

describe("温和删除项目与 409 冲突反馈", () => {
  it("默认勾选保留历史对话提交 ?detach_sessions=true，遇 409 session_has_active_run 保留弹窗并可成功删除返回 /projects", async () => {
    let deleteAttempts = 0;
    const { deleteUrls } = installProjectWorkspaceFetch({
      deleteResponse: (url) => {
        deleteAttempts += 1;
        if (deleteAttempts === 1) {
          return jsonResponse({ detail: "session_has_active_run" }, 409);
        }
        if (url.searchParams.get("detach_sessions") !== "true") {
          return jsonResponse({ detail: "project_has_sessions" }, 409);
        }
        return new Response(null, { status: 204 });
      },
      projects: [PROJECT_A],
      sessionsTotal: 2,
    });
    const user = userEvent.setup();
    const app = renderApp(`/projects/${PROJECT_A.id}`);

    await screen.findByRole("heading", { level: 1, name: PROJECT_A.name });
    await user.click(screen.getByRole("button", { name: "删除项目" }));

    const alertDialog = screen.getByRole("alertdialog", { name: "删除项目" });
    const detachCheckbox = within(alertDialog).getByRole("checkbox", {
      name: /保留历史对话/,
    });
    expect(detachCheckbox).toBeChecked();

    // 取消勾选时对存在历史对话的项目给出警告提示。
    await user.click(detachCheckbox);
    expect(detachCheckbox).not.toBeChecked();
    expect(
      await within(alertDialog).findByText(/仍有 2 个历史对话/),
    ).toBeInTheDocument();

    // 重新勾选后提交：首次遇 409 session_has_active_run 保留弹窗并展示清晰反馈。
    await user.click(detachCheckbox);
    expect(detachCheckbox).toBeChecked();
    await user.click(
      within(alertDialog).getByRole("button", { name: "确认删除" }),
    );

    expect(
      await within(alertDialog).findByText(
        "项目内存在正在执行的对话任务，请等待任务完成或停止后再删除项目。",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("alertdialog", { name: "删除项目" }),
    ).toBeInTheDocument();

    // 再次提交成功：携带 ?detach_sessions=true 并跳转回 /projects。
    await user.click(
      within(alertDialog).getByRole("button", { name: "确认删除" }),
    );

    await waitFor(() => {
      expect(app.getLocation().pathname).toBe("/projects");
    });
    expect(deleteUrls).toEqual([
      `/api/v1/projects/${PROJECT_A.id}?detach_sessions=true`,
      `/api/v1/projects/${PROJECT_A.id}?detach_sessions=true`,
    ]);
  });

  it("跨项目切换会隔离工作区实例，清空旧项目会话搜索关键词且不向新项目携带旧 q 参数", async () => {
    const projectBSession = makeSession({
      id: "project-b-session",
      project_id: PROJECT_B.id,
      title: "季度财务审阅",
    });
    const sessionRequests: Array<{ projectId: string | null; q: string | null }> =
      [];

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/projects" && method === "GET") {
          return jsonResponse(listResponse([PROJECT_A, PROJECT_B], 1, 100, 2));
        }
        if (url.pathname === `/api/v1/projects/${PROJECT_A.id}`) {
          return jsonResponse(PROJECT_A);
        }
        if (url.pathname === `/api/v1/projects/${PROJECT_B.id}`) {
          return jsonResponse(PROJECT_B);
        }
        if (
          url.pathname === `/api/v1/projects/${PROJECT_A.id}/knowledge-bases` ||
          url.pathname === `/api/v1/projects/${PROJECT_B.id}/knowledge-bases`
        ) {
          return jsonResponse([]);
        }
        if (url.pathname === "/api/v1/skills" && method === "GET") {
          return jsonResponse({ items: [] });
        }
        if (url.pathname === "/api/v1/sessions" && method === "GET") {
          const projectId = url.searchParams.get("project_id");
          const q = url.searchParams.get("q");
          if (projectId) {
            sessionRequests.push({ projectId, q });
          }
          if (projectId === PROJECT_A.id) {
            return jsonResponse(listResponse([PROJECT_SESSION], 1, 10, 1));
          }
          if (projectId === PROJECT_B.id) {
            return jsonResponse(listResponse([projectBSession], 1, 10, 1));
          }
          return jsonResponse(listResponse([], 1, 8, 0));
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );

    const user = userEvent.setup();
    const app = renderApp(`/projects/${PROJECT_A.id}`, {
      // 预置两个已缓存项目，确保跨项目切换同步切入 ProjectWorkspaceContent 从而验证 key 隔离。
      seedQueryClient: (client) => {
        client.setQueryData(projectKeys.detail(PROJECT_A.id), PROJECT_A);
        client.setQueryData(projectKeys.detail(PROJECT_B.id), PROJECT_B);
      },
    });

    expect(
      await screen.findByRole("heading", { level: 1, name: PROJECT_A.name }),
    ).toBeInTheDocument();
    const searchInputA = screen.getByLabelText("搜索项目内对话");
    await user.type(searchInputA, "年度规划");

    await waitFor(() => {
      expect(
        sessionRequests.some(
          (req) => req.projectId === PROJECT_A.id && req.q === "年度规划",
        ),
      ).toBe(true);
    });

    // 切换至已缓存的项目 B：工作区实例应重建，搜索框清空且不携带项目 A 的旧 q 参数。
    await app.navigateTo(`/projects/${PROJECT_B.id}`);

    expect(
      await screen.findByRole("heading", { level: 1, name: PROJECT_B.name }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("搜索项目内对话")).toHaveValue("");
    expect(await screen.findByText("季度财务审阅")).toBeInTheDocument();

    const projectBRequests = sessionRequests.filter(
      (req) => req.projectId === PROJECT_B.id,
    );
    expect(projectBRequests.length).toBeGreaterThan(0);
    expect(projectBRequests.every((req) => req.q === null)).toBe(true);
  });
});
