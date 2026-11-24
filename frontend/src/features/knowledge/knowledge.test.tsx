import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  getRequestUrl,
  knowledgeListResponse,
  KNOWLEDGE_BASE_A,
  KNOWLEDGE_BASE_B,
  makeDeptAdmin,
  makeSuperAdmin,
  TEST_USER,
} from "../../test/fixtures";
import { jsonResponse, renderApp } from "../../test/testUtils";
import { canManageKnowledgeBase, type KnowledgeBase } from "./model";

function requestUrl(input: RequestInfo | URL): URL {
  return new URL(getRequestUrl(input), "http://localhost");
}

describe("知识库目录", () => {
  it("主内容、侧栏和分页只展示真实知识库字段", async () => {
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (
          url.pathname === "/api/v1/knowledge-bases" &&
          (!init?.method || init.method === "GET")
        ) {
          const pageSize = Number(
            url.searchParams.get("page_size") ?? 20,
          );
          return jsonResponse(
            knowledgeListResponse(
              [KNOWLEDGE_BASE_A, KNOWLEDGE_BASE_B],
              1,
              pageSize,
            ),
          );
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    renderApp("/knowledge");

    await screen.findByRole("heading", { level: 1, name: "知识库" });
    const main = screen.getByRole("main");
    expect(
      await within(main).findByRole("link", { name: /产品研究库/ }),
    ).toHaveAttribute("href", `/knowledge/${KNOWLEDGE_BASE_A.id}`);
    expect(
      within(main).getByText(KNOWLEDGE_BASE_A.description ?? ""),
    ).toBeInTheDocument();
    expect(within(main).queryByText(/共\s*\d+\s*个知识库/)).not.toBeInTheDocument();
    expect(within(main).queryByText(/份文档/)).not.toBeInTheDocument();

    expect(
      await within(main).findByRole("link", { name: /市场资料库/ }),
    ).toHaveAttribute("href", `/knowledge/${KNOWLEDGE_BASE_B.id}`);
    const sidebar = screen.getByRole("complementary", {
      name: "工作区侧栏",
    });
    expect(
      within(sidebar).getByRole("link", { name: "知识库" }),
    ).toHaveAttribute("aria-current", "page");
    expect(fetchMock).toHaveBeenCalled();
  });

  it("下一页只依据后端 items 和 page_size 判断", async () => {
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (
          url.pathname === "/api/v1/knowledge-bases" &&
          (!init?.method || init.method === "GET")
        ) {
          return jsonResponse(
            knowledgeListResponse(
              [KNOWLEDGE_BASE_A, KNOWLEDGE_BASE_B],
              1,
              2,
            ),
          );
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    renderApp("/knowledge");

    await screen.findByRole("heading", { level: 1, name: "知识库" });
    const main = screen.getByRole("main");
    await within(main).findByText("产品研究库");
    expect(
      within(main).getByRole("button", { name: "下一页" }),
    ).toBeEnabled();
    expect(
      within(main).queryByText(/共\s*\d+\s*个知识库/),
    ).not.toBeInTheDocument();
  });

  it("创建知识库提交真实名称和说明并进入详情", async () => {
    let created = false;
    const createdBase = {
      ...KNOWLEDGE_BASE_A,
      id: "knowledge-created",
      name: "新建研究库",
      description: "用于整理访谈",
    };
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (
          url.pathname === "/api/v1/knowledge-bases" &&
          init?.method === "POST"
        ) {
          created = true;
          return jsonResponse(createdBase, 201);
        }
        if (
          url.pathname === "/api/v1/knowledge-bases" &&
          (!init?.method || init.method === "GET")
        ) {
          const pageSize = Number(
            url.searchParams.get("page_size") ?? 20,
          );
          return jsonResponse(
            knowledgeListResponse(
              created ? [createdBase] : [],
              1,
              pageSize,
            ),
          );
        }
        if (url.pathname === `/api/v1/knowledge-bases/${createdBase.id}`) {
          return jsonResponse(createdBase);
        }
        if (
          url.pathname ===
          `/api/v1/knowledge-bases/${createdBase.id}/documents`
        ) {
          return jsonResponse(
            knowledgeListResponse([], 1, 10),
          );
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/knowledge");
    await screen.findByRole("heading", { level: 1, name: "知识库" });
    const main = screen.getByRole("main");
    const createButtons = await within(main).findAllByRole("button", {
      name: "新建知识库",
    });
    await user.click(createButtons[0]);
    const dialog = screen.getByRole("dialog", { name: "新建知识库" });
    await user.type(
      within(dialog).getByLabelText("知识库名称"),
      "新建研究库",
    );
    await user.type(
      within(dialog).getByLabelText("说明"),
      "用于整理访谈",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "创建知识库" }),
    );

    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "新建研究库",
      }),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { name: "还没有文档" }),
    ).toBeInTheDocument();
    const postCall = fetchMock.mock.calls.find(
      ([input, init]) =>
        requestUrl(input).pathname === "/api/v1/knowledge-bases" &&
        init?.method === "POST",
    );
    expect(JSON.parse(String(postCall?.[1]?.body))).toEqual({
      description: "用于整理访谈",
      name: "新建研究库",
    });
  });

  it("改名和删除成功后刷新真实目录", async () => {
    let bases = [KNOWLEDGE_BASE_A];
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (
          url.pathname === "/api/v1/knowledge-bases" &&
          (!init?.method || init.method === "GET")
        ) {
          const pageSize = Number(
            url.searchParams.get("page_size") ?? 20,
          );
          return jsonResponse(
            knowledgeListResponse(bases, 1, pageSize),
          );
        }
        if (
          url.pathname === `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}`
        ) {
          if (init?.method === "PATCH") {
            const body = JSON.parse(String(init.body)) as {
              name?: string;
            };
            const updated = {
              ...KNOWLEDGE_BASE_A,
              name: body.name ?? KNOWLEDGE_BASE_A.name,
            };
            bases = [updated];
            return jsonResponse(updated);
          }
          if (init?.method === "DELETE") {
            bases = [];
            return new Response(null, { status: 204 });
          }
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/knowledge");
    await screen.findByRole("heading", { level: 1, name: "知识库" });
    const main = screen.getByRole("main");
    await within(main).findByText("产品研究库");
    await user.click(
      within(main).getByRole("button", {
        name: "编辑知识库：产品研究库",
      }),
    );
    const editDialog = screen.getByRole("dialog", {
      name: "编辑知识库",
    });
    const nameInput = within(editDialog).getByLabelText("知识库名称");
    await user.clear(nameInput);
    await user.type(nameInput, "更新后的研究库");
    await user.click(
      within(editDialog).getByRole("button", {
        name: "保存知识库",
      }),
    );

    expect(
      await within(main).findByText("更新后的研究库"),
    ).toBeInTheDocument();
    await user.click(
      within(main).getByRole("button", {
        name: "删除知识库：更新后的研究库",
      }),
    );
    const deleteDialog = screen.getByRole("dialog", {
      name: "删除知识库",
    });
    await user.click(
      within(deleteDialog).getByLabelText(
        "我已确认要删除这个知识库",
      ),
    );
    await user.click(
      within(deleteDialog).getByRole("button", {
        name: "删除知识库",
      }),
    );

    expect(
      await within(main).findByRole("heading", {
        name: "还没有知识库",
      }),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(
          ([input, init]) =>
            requestUrl(input).pathname ===
              `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}` &&
            init?.method === "DELETE",
        ),
      ).toBe(true);
    });
  });

  it("三层可见性分段筛选同步 URL ?scope=、透传 visibility_scope，并按角色控制只读与写操作按钮", async () => {
    const companyBase: KnowledgeBase = {
      ...KNOWLEDGE_BASE_A,
      id: "kb-company",
      name: "集团制度库",
      owner_department_id: null,
      visibility_scope: "company",
    };
    const deptBase: KnowledgeBase = {
      ...KNOWLEDGE_BASE_B,
      id: "kb-dept",
      name: "研发技术规范库",
      owner_department_id: "dept-rd",
      visibility_scope: "department",
    };
    const personalBase: KnowledgeBase = {
      ...KNOWLEDGE_BASE_A,
      id: "kb-personal",
      name: "个人工作笔记",
      owner_department_id: null,
      visibility_scope: "personal",
    };

    // 纯函数权限矩阵校验（含空用户态 fail-closed）。
    expect(canManageKnowledgeBase(null, personalBase)).toBe(false);
    expect(canManageKnowledgeBase(TEST_USER, personalBase)).toBe(true);
    expect(canManageKnowledgeBase(TEST_USER, companyBase)).toBe(false);
    expect(canManageKnowledgeBase(TEST_USER, deptBase)).toBe(false);
    expect(canManageKnowledgeBase(makeSuperAdmin(), companyBase)).toBe(true);
    expect(
      canManageKnowledgeBase(
        makeDeptAdmin({ managed_department_ids: ["dept-rd"] }),
        deptBase,
      ),
    ).toBe(true);
    expect(
      canManageKnowledgeBase(
        makeDeptAdmin({ managed_department_ids: ["dept-fin"] }),
        deptBase,
      ),
    ).toBe(false);

    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (
          url.pathname === "/api/v1/knowledge-bases" &&
          (!init?.method || init.method === "GET")
        ) {
          const scope = url.searchParams.get("visibility_scope");
          const all = [companyBase, deptBase, personalBase];
          const items = scope
            ? all.filter((item) => item.visibility_scope === scope)
            : all;
          return jsonResponse(knowledgeListResponse(items, 1, 12));
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    const app = renderApp("/knowledge?page=2");
    await screen.findByRole("heading", { level: 1, name: "知识库" });
    const main = screen.getByRole("main");

    // 1. 普通员工对全公司库和部门库只读：隐藏编辑与删除按钮，展示只读与三层徽章。
    const companyCard = (
      await within(main).findByRole("link", { name: /集团制度库/ })
    ).closest("li") as HTMLElement;
    expect(within(companyCard).getByText("全公司")).toBeInTheDocument();
    expect(within(companyCard).getByText("只读")).toBeInTheDocument();
    expect(
      within(companyCard).queryByRole("button", { name: /编辑知识库/ }),
    ).not.toBeInTheDocument();
    expect(
      within(companyCard).queryByRole("button", { name: /删除知识库/ }),
    ).not.toBeInTheDocument();

    const deptCard = within(main)
      .getByRole("link", { name: /研发技术规范库/ })
      .closest("li") as HTMLElement;
    expect(within(deptCard).getByText("研发部")).toBeInTheDocument();
    expect(within(deptCard).getByText("只读")).toBeInTheDocument();

    const personalCard = within(main)
      .getByRole("link", { name: /个人工作笔记/ })
      .closest("li") as HTMLElement;
    expect(
      within(personalCard).getByRole("button", {
        name: "编辑知识库：个人工作笔记",
      }),
    ).toBeInTheDocument();

    // 2. 点击「部门共享」筛选：URL 同步 ?scope=department 且重置页码为 1，请求带 visibility_scope=department。
    const filterList = within(main).getByRole("tablist", {
      name: "按可见范围筛选知识库",
    });
    await user.click(within(filterList).getByRole("tab", { name: "部门共享" }));

    expect(app.getLocation()).toEqual({
      pathname: "/knowledge",
      search: "?scope=department",
    });
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([input]) => {
          const url = requestUrl(input);
          return (
            url.pathname === "/api/v1/knowledge-bases" &&
            url.searchParams.get("visibility_scope") === "department" &&
            url.searchParams.get("page") === "1"
          );
        }),
      ).toBe(true);
    });
    expect(within(main).getByText("研发技术规范库")).toBeInTheDocument();
    expect(within(main).queryByText("集团制度库")).not.toBeInTheDocument();
  });

  it("部门管理员创建部门知识库自动预选唯一管辖部门，超管支持全公司/部门选择且编辑未改范围不发多余字段", async () => {
    const deptAdmin = makeDeptAdmin();
    const superAdmin = makeSuperAdmin();
    const postBodies: Array<Record<string, unknown>> = [];
    const patchBodies: Array<Record<string, unknown>> = [];
    const deptBase: KnowledgeBase = {
      ...KNOWLEDGE_BASE_A,
      id: "kb-fin-dept",
      name: "财务制度库",
      owner_department_id: "dept-fin",
      visibility_scope: "department",
    };

    // 1. dept_admin：仅有个人 / 部门共享可选，单管辖部门自动预选。
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(deptAdmin);
        }
        if (
          url.pathname === "/api/v1/knowledge-bases" &&
          init?.method === "POST"
        ) {
          postBodies.push(JSON.parse(String(init.body)));
          return jsonResponse(deptBase, 201);
        }
        if (url.pathname === "/api/v1/knowledge-bases") {
          return jsonResponse(knowledgeListResponse([deptBase], 1, 12));
        }
        if (url.pathname === `/api/v1/knowledge-bases/${deptBase.id}`) {
          return jsonResponse(deptBase);
        }
        if (url.pathname === `/api/v1/knowledge-bases/${deptBase.id}/documents`) {
          return jsonResponse(knowledgeListResponse([], 1, 10));
        }
        throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
      }),
    );
    const user = userEvent.setup();
    const firstRender = renderApp("/knowledge");
    await screen.findByRole("heading", { level: 1, name: "知识库" });
    await user.click(screen.getByRole("button", { name: "新建知识库" }));
    const deptDialog = screen.getByRole("dialog", { name: "新建知识库" });
    const scopeSelect = within(deptDialog).getByLabelText("可见范围");
    expect(within(scopeSelect).queryByText("全公司")).not.toBeInTheDocument();
    await user.selectOptions(scopeSelect, "department");
    // 唯一管辖部门「财务部」应自动预选。
    expect(within(deptDialog).getByLabelText("归属部门")).toHaveValue("dept-fin");
    await user.type(within(deptDialog).getByLabelText("知识库名称"), "财务制度库");
    await user.click(within(deptDialog).getByRole("button", { name: "创建知识库" }));

    await waitFor(() => {
      expect(postBodies[0]).toEqual({
        name: "财务制度库",
        owner_department_id: "dept-fin",
        visibility_scope: "department",
      });
    });
    firstRender.unmount();

    // 2. super_admin：部门目录拉取失败降级手工输入；编辑仅改名不发可见性字段，切为 company 不发 owner_department_id。
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(superAdmin);
        }
        if (url.pathname === "/api/v1/admin/org/departments") {
          return jsonResponse({ detail: "http_error" }, 500);
        }
        if (
          url.pathname === `/api/v1/knowledge-bases/${deptBase.id}` &&
          init?.method === "PATCH"
        ) {
          patchBodies.push(JSON.parse(String(init.body)));
          return jsonResponse({ ...deptBase, name: "财务与报销制度库" });
        }
        if (url.pathname === "/api/v1/knowledge-bases") {
          return jsonResponse(knowledgeListResponse([deptBase], 1, 12));
        }
        throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
      }),
    );
    renderApp("/knowledge");
    await screen.findByText("财务制度库");
    await user.click(
      screen.getByRole("button", { name: "编辑知识库：财务制度库" }),
    );
    const editDialog = screen.getByRole("dialog", { name: "编辑知识库" });
    // 部门接口失败时降级为文本输入框。
    expect(
      await within(editDialog).findByText(/已降级为手动输入部门 ID/),
    ).toBeInTheDocument();

    // 仅改名、不改范围与归属部门：不发送 visibility_scope / owner_department_id。
    const editNameInput = within(editDialog).getByLabelText("知识库名称");
    await user.clear(editNameInput);
    await user.type(editNameInput, "财务与报销制度库");
    await user.click(within(editDialog).getByRole("button", { name: "保存知识库" }));
    await waitFor(() => {
      expect(patchBodies[0]).toEqual({ name: "财务与报销制度库" });
    });

    // 再次编辑并切换为「全公司」：只发 visibility_scope: "company"，绝不发非法 owner_department_id。
    await user.click(
      screen.getByRole("button", { name: "编辑知识库：财务制度库" }),
    );
    const editDialog2 = screen.getByRole("dialog", { name: "编辑知识库" });
    await user.selectOptions(
      within(editDialog2).getByLabelText("可见范围"),
      "company",
    );
    await user.click(
      within(editDialog2).getByRole("button", { name: "保存知识库" }),
    );
    await waitFor(() => {
      expect(patchBodies[1]).toEqual({ visibility_scope: "company" });
    });
  });
});
