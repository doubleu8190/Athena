import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../../shared/api/client";
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
import { knowledgeErrorMessage } from "./errorMessages";
import {
  canManageKnowledgeBase,
  countExtraVisibilityGrants,
  type KnowledgeBase,
} from "./model";

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

  it("super_admin 新建部门共享知识库追加跨部门与角色授权，去重拉取部门目录并规范化提交", async () => {
    const superAdmin = makeSuperAdmin();
    let orgDeptCalls = 0;
    const postBodies: Array<Record<string, unknown>> = [];
    const createdBase: KnowledgeBase = {
      ...KNOWLEDGE_BASE_A,
      id: "kb-shared-fin",
      name: "跨部门财务制度库",
      owner_department_id: "dept-fin",
      visibility_grants: [
        {
          created_at: "2026-11-24T09:00:00Z",
          id: "grant-owner",
          include_subdepartments: true,
          target_id: "dept-fin",
          target_type: "department",
        },
        {
          created_at: "2026-11-24T09:00:00Z",
          id: "grant-rd",
          include_subdepartments: true,
          target_id: "dept-rd",
          target_type: "department",
        },
        {
          created_at: "2026-11-24T09:00:00Z",
          id: "grant-role",
          include_subdepartments: false,
          target_id: "dept_admin",
          target_type: "role",
        },
      ],
      visibility_scope: "department",
    };

    const orgDepartments = [
      {
        created_at: "2026-10-01T00:00:00Z",
        external_id: "ext-fin",
        id: "dept-fin",
        name: "财务部",
        order_index: 1,
        parent_id: "dept-root",
        path: "/dept-root/dept-fin/",
        status: "active",
        synced_at: "2026-11-24T00:00:00Z",
        updated_at: "2026-11-24T00:00:00Z",
      },
      {
        created_at: "2026-10-01T00:00:00Z",
        external_id: "ext-rd",
        id: "dept-rd",
        name: "研发部",
        order_index: 2,
        parent_id: "dept-root",
        path: "/dept-root/dept-rd/",
        status: "active",
        synced_at: "2026-11-24T00:00:00Z",
        updated_at: "2026-11-24T00:00:00Z",
      },
      {
        created_at: "2026-10-01T00:00:00Z",
        external_id: "ext-hr",
        id: "dept-hr",
        name: "人力资源部",
        order_index: 3,
        parent_id: "dept-root",
        path: "/dept-root/dept-hr/",
        status: "active",
        synced_at: "2026-11-24T00:00:00Z",
        updated_at: "2026-11-24T00:00:00Z",
      },
    ];

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(superAdmin);
        }
        if (url.pathname === "/api/v1/admin/org/departments") {
          orgDeptCalls += 1;
          return jsonResponse({ items: orgDepartments });
        }
        if (
          url.pathname === "/api/v1/knowledge-bases" &&
          init?.method === "POST"
        ) {
          postBodies.push(JSON.parse(String(init.body)));
          return jsonResponse(createdBase, 201);
        }
        if (url.pathname === "/api/v1/knowledge-bases") {
          return jsonResponse(knowledgeListResponse([], 1, 12));
        }
        if (url.pathname === `/api/v1/knowledge-bases/${createdBase.id}`) {
          return jsonResponse(createdBase);
        }
        if (
          url.pathname ===
          `/api/v1/knowledge-bases/${createdBase.id}/documents`
        ) {
          return jsonResponse(knowledgeListResponse([], 1, 10));
        }
        throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
      }),
    );
    const user = userEvent.setup();

    renderApp("/knowledge");
    await screen.findByRole("heading", { level: 1, name: "知识库" });
    await user.click(
      screen.getAllByRole("button", { name: "新建知识库" })[0],
    );
    const dialog = screen.getByRole("dialog", { name: "新建知识库" });

    await user.type(
      within(dialog).getByLabelText("知识库名称"),
      "跨部门财务制度库",
    );
    await user.selectOptions(
      within(dialog).getByLabelText("可见范围"),
      "department",
    );
    const ownerSelect = await within(dialog).findByLabelText("归属部门");
    await waitFor(() =>
      expect(within(ownerSelect).getByText("财务部")).toBeInTheDocument(),
    );
    await user.selectOptions(ownerSelect, "dept-fin");

    // 确认 VisibilityGrantsEditor 渲染并复用同一份企业活跃部门目录（仅 1 次查询）。
    const grantsEditor = within(dialog).getByTestId("visibility-grants-editor");
    expect(orgDeptCalls).toBe(1);

    // 追加跨部门「研发部」（默认含子部门 true）。
    await user.selectOptions(
      within(grantsEditor).getByLabelText("选择附加共享部门"),
      "dept-rd",
    );
    await user.click(
      within(grantsEditor).getByRole("button", { name: "添加共享" }),
    );

    // 切至角色授权，追加「部门管理员」（role 项 include_subdepartments 恒为 false）。
    await user.selectOptions(
      within(grantsEditor).getByLabelText("附加授权类型"),
      "role",
    );
    await user.selectOptions(
      within(grantsEditor).getByLabelText("选择附加共享角色"),
      "dept_admin",
    );
    await user.click(
      within(grantsEditor).getByRole("button", { name: "添加共享" }),
    );

    await user.click(
      within(dialog).getByRole("button", { name: "创建知识库" }),
    );

    await waitFor(() => {
      expect(postBodies).toHaveLength(1);
    });
    expect(postBodies[0]).toEqual({
      name: "跨部门财务制度库",
      owner_department_id: "dept-fin",
      visibility_grants: [
        {
          include_subdepartments: true,
          target_id: "dept-fin",
          target_type: "department",
        },
        {
          include_subdepartments: true,
          target_id: "dept-rd",
          target_type: "department",
        },
        {
          include_subdepartments: false,
          target_id: "dept_admin",
          target_type: "role",
        },
      ],
      visibility_scope: "department",
    });
    expect(orgDeptCalls).toBe(1);

    // 跳转详情后顶部应展示「额外共享 2 项」徽章。
    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "跨部门财务制度库",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText("额外共享 2 项")).toBeInTheDocument();
  });

  it("dept_admin 未改动附加授权时省略 visibility_grants；追加第二管辖部门时隐藏角色入口且默认不含子部门", async () => {
    const multiDeptAdmin = makeDeptAdmin({
      departments: [
        {
          id: "dept-fin",
          is_primary: true,
          name: "财务部",
          path: "/dept-root/dept-fin/",
        },
        {
          id: "dept-audit",
          is_primary: false,
          name: "内审部",
          path: "/dept-root/dept-audit/",
        },
      ],
      managed_department_ids: ["dept-fin", "dept-audit"],
    });
    const postBodies: Array<Record<string, unknown>> = [];
    const patchBodies: Array<Record<string, unknown>> = [];
    let currentBases: KnowledgeBase[] = [
      {
        ...KNOWLEDGE_BASE_A,
        id: "kb-fin-1",
        name: "财务制度基线库",
        owner_department_id: "dept-fin",
        visibility_grants: [
          {
            created_at: "2026-11-24T09:00:00Z",
            id: "g-owner",
            include_subdepartments: false,
            target_id: "dept-fin",
            target_type: "department",
          },
        ],
        visibility_scope: "department",
      },
    ];

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(multiDeptAdmin);
        }
        if (
          url.pathname === "/api/v1/knowledge-bases" &&
          init?.method === "POST"
        ) {
          const body = JSON.parse(String(init.body)) as Record<string, unknown>;
          postBodies.push(body);
          const created: KnowledgeBase = {
            ...KNOWLEDGE_BASE_A,
            id: `kb-created-${postBodies.length}`,
            name: String(body.name),
            owner_department_id: String(body.owner_department_id ?? ""),
            visibility_scope: "department",
          };
          currentBases = [created, ...currentBases];
          return jsonResponse(created, 201);
        }
        if (
          url.pathname === "/api/v1/knowledge-bases/kb-fin-1" &&
          init?.method === "PATCH"
        ) {
          const body = JSON.parse(String(init.body)) as Record<string, unknown>;
          patchBodies.push(body);
          return jsonResponse(currentBases[0]);
        }
        if (url.pathname === "/api/v1/knowledge-bases") {
          return jsonResponse(knowledgeListResponse(currentBases, 1, 12));
        }
        if (url.pathname.startsWith("/api/v1/knowledge-bases/kb-created-")) {
          const id = url.pathname.split("/")[4];
          if (url.pathname.endsWith("/documents")) {
            return jsonResponse(knowledgeListResponse([], 1, 10));
          }
          const matched = currentBases.find((item) => item.id === id);
          return jsonResponse(matched ?? currentBases[0]);
        }
        throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
      }),
    );
    const user = userEvent.setup();

    const app = renderApp("/knowledge");
    await screen.findByText("财务制度基线库");

    // 1. 新建：仅选归属部门、不触碰附加授权区 -> 严格省略 visibility_grants。
    await user.click(screen.getByRole("button", { name: "新建知识库" }));
    const createDialog1 = screen.getByRole("dialog", { name: "新建知识库" });
    await user.type(
      within(createDialog1).getByLabelText("知识库名称"),
      "默认归属财务库",
    );
    await user.selectOptions(
      within(createDialog1).getByLabelText("可见范围"),
      "department",
    );
    await user.selectOptions(
      within(createDialog1).getByLabelText("归属部门"),
      "dept-fin",
    );
    const grantsEditor1 = within(createDialog1).getByTestId(
      "visibility-grants-editor",
    );
    // 部门管理员不展示角色授权入口，且归属部门与新增项「含子部门」均默认 false。
    expect(
      within(grantsEditor1).queryByLabelText("附加授权类型"),
    ).not.toBeInTheDocument();
    expect(
      within(grantsEditor1).getByLabelText("包含下级子部门"),
    ).not.toBeChecked();
    expect(within(grantsEditor1).getByLabelText("含子部门")).not.toBeChecked();

    await user.click(
      within(createDialog1).getByRole("button", { name: "创建知识库" }),
    );
    await waitFor(() => expect(postBodies).toHaveLength(1));
    expect(postBodies[0]).toEqual({
      name: "默认归属财务库",
      owner_department_id: "dept-fin",
      visibility_scope: "department",
    });
    expect(postBodies[0]).not.toHaveProperty("visibility_grants");

    // 2. 返回目录页再新建：显式追加自己管辖的第二个部门「内审部」。
    app.navigateTo("/knowledge");
    await screen.findByText("财务制度基线库");
    await user.click(screen.getByRole("button", { name: "新建知识库" }));
    const createDialog2 = screen.getByRole("dialog", { name: "新建知识库" });
    await user.type(
      within(createDialog2).getByLabelText("知识库名称"),
      "财务内审联合库",
    );
    await user.selectOptions(
      within(createDialog2).getByLabelText("可见范围"),
      "department",
    );
    await user.selectOptions(
      within(createDialog2).getByLabelText("归属部门"),
      "dept-fin",
    );
    const grantsEditor2 = within(createDialog2).getByTestId(
      "visibility-grants-editor",
    );
    await user.selectOptions(
      within(grantsEditor2).getByLabelText("选择附加共享部门"),
      "dept-audit",
    );
    await user.click(
      within(grantsEditor2).getByRole("button", { name: "添加共享" }),
    );
    await user.click(
      within(createDialog2).getByRole("button", { name: "创建知识库" }),
    );
    await waitFor(() => expect(postBodies).toHaveLength(2));
    expect(postBodies[1]).toEqual({
      name: "财务内审联合库",
      owner_department_id: "dept-fin",
      visibility_grants: [
        {
          include_subdepartments: false,
          target_id: "dept-fin",
          target_type: "department",
        },
        {
          include_subdepartments: false,
          target_id: "dept-audit",
          target_type: "department",
        },
      ],
      visibility_scope: "department",
    });

    // 3. 返回目录页编辑已有知识库：更换归属部门为「内审部」且未改附加授权 -> PATCH 省略 visibility_grants。
    app.navigateTo("/knowledge");
    await screen.findByText("财务制度基线库");
    await user.click(
      screen.getByRole("button", { name: "编辑知识库：财务制度基线库" }),
    );
    const editDialog = screen.getByRole("dialog", { name: "编辑知识库" });
    await user.selectOptions(
      within(editDialog).getByLabelText("归属部门"),
      "dept-audit",
    );
    await user.click(
      within(editDialog).getByRole("button", { name: "保存知识库" }),
    );
    await waitFor(() => expect(patchBodies).toHaveLength(1));
    expect(patchBodies[0]).toEqual({
      owner_department_id: "dept-audit",
      visibility_scope: "department",
    });
    expect(patchBodies[0]).not.toHaveProperty("visibility_grants");

    // 4. 再次编辑并在原归属部门下追加第二管辖部门「内审部」-> PATCH 携带规范化 visibility_grants。
    await user.click(
      screen.getByRole("button", { name: "编辑知识库：财务制度基线库" }),
    );
    const editDialog2 = screen.getByRole("dialog", { name: "编辑知识库" });
    const editGrantsEditor = within(editDialog2).getByTestId(
      "visibility-grants-editor",
    );
    await user.selectOptions(
      within(editGrantsEditor).getByLabelText("选择附加共享部门"),
      "dept-audit",
    );
    await user.click(
      within(editGrantsEditor).getByRole("button", { name: "添加共享" }),
    );
    await user.click(
      within(editDialog2).getByRole("button", { name: "保存知识库" }),
    );
    await waitFor(() => expect(patchBodies).toHaveLength(2));
    expect(patchBodies[1]).toEqual({
      visibility_grants: [
        {
          include_subdepartments: false,
          target_id: "dept-fin",
          target_type: "department",
        },
        {
          include_subdepartments: false,
          target_id: "dept-audit",
          target_type: "department",
        },
      ],
    });
  });

  it("super_admin 仅修改附加授权触发 PATCH，切为 personal/company 绝不携带归属与授权，403 展示权限提示且列表展示额外共享徽章", async () => {
    const superAdmin = makeSuperAdmin();
    const patchBodies: Array<Record<string, unknown>> = [];
    let shouldForbidNextPatch = false;
    const deptBaseWithExtras: KnowledgeBase = {
      ...KNOWLEDGE_BASE_A,
      id: "kb-shared-ops",
      name: "运营协作知识库",
      owner_department_id: "dept-fin",
      visibility_grants: [
        {
          created_at: "2026-11-24T09:00:00Z",
          id: "g-1",
          include_subdepartments: true,
          target_id: "dept-fin",
          target_type: "department",
        },
        {
          created_at: "2026-11-24T09:00:00Z",
          id: "g-2",
          include_subdepartments: true,
          target_id: "dept-rd",
          target_type: "department",
        },
      ],
      visibility_scope: "department",
    };
    const deptBaseNoExtras: KnowledgeBase = {
      ...KNOWLEDGE_BASE_B,
      id: "kb-only-owner",
      name: "纯本部门知识库",
      owner_department_id: "dept-fin",
      visibility_grants: [
        {
          created_at: "2026-11-24T09:00:00Z",
          id: "g-only",
          include_subdepartments: true,
          target_id: "dept-fin",
          target_type: "department",
        },
      ],
      visibility_scope: "department",
    };

    // 纯函数与错误码映射断言。
    expect(countExtraVisibilityGrants(deptBaseWithExtras)).toBe(1);
    expect(countExtraVisibilityGrants(deptBaseNoExtras)).toBe(0);
    expect(
      countExtraVisibilityGrants({
        ...deptBaseWithExtras,
        visibility_scope: "company",
      }),
    ).toBe(0);
    expect(
      knowledgeErrorMessage(new ApiError(403, "visibility_grant_forbidden")),
    ).toMatch(/包含下级子部门/);
    expect(
      knowledgeErrorMessage(new ApiError(422, "visibility_grant_invalid")),
    ).toMatch(/格式无效/);
    expect(
      knowledgeErrorMessage(
        new ApiError(422, "visibility_grant_target_invalid"),
      ),
    ).toMatch(/目标标识无效/);
    expect(
      knowledgeErrorMessage(new ApiError(422, "visibility_grant_duplicate")),
    ).toMatch(/重复/);
    expect(
      knowledgeErrorMessage(new ApiError(422, "visibility_grant_role_invalid")),
    ).toMatch(/授权角色无效/);
    expect(
      knowledgeErrorMessage(
        new ApiError(422, "visibility_grant_target_type_invalid"),
      ),
    ).toMatch(/目标类型/);
    expect(
      knowledgeErrorMessage(
        new ApiError(422, "visibility_grant_department_not_found"),
      ),
    ).toMatch(/不存在或已归档/);

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(superAdmin);
        }
        if (url.pathname === "/api/v1/admin/org/departments") {
          return jsonResponse({
            items: [
              {
                created_at: "2026-10-01T00:00:00Z",
                external_id: "ext-fin",
                id: "dept-fin",
                name: "财务部",
                order_index: 1,
                parent_id: "dept-root",
                path: "/dept-root/dept-fin/",
                status: "active",
                synced_at: "2026-11-24T00:00:00Z",
                updated_at: "2026-11-24T00:00:00Z",
              },
              {
                created_at: "2026-10-01T00:00:00Z",
                external_id: "ext-rd",
                id: "dept-rd",
                name: "研发部",
                order_index: 2,
                parent_id: "dept-root",
                path: "/dept-root/dept-rd/",
                status: "active",
                synced_at: "2026-11-24T00:00:00Z",
                updated_at: "2026-11-24T00:00:00Z",
              },
            ],
          });
        }
        if (
          url.pathname === `/api/v1/knowledge-bases/${deptBaseWithExtras.id}` &&
          init?.method === "PATCH"
        ) {
          const body = JSON.parse(String(init.body)) as Record<string, unknown>;
          if (shouldForbidNextPatch) {
            return jsonResponse({ detail: "visibility_grant_forbidden" }, 403);
          }
          patchBodies.push(body);
          return jsonResponse(deptBaseWithExtras);
        }
        if (url.pathname === "/api/v1/knowledge-bases") {
          return jsonResponse(
            knowledgeListResponse([deptBaseWithExtras, deptBaseNoExtras], 1, 12),
          );
        }
        throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
      }),
    );
    const user = userEvent.setup();

    renderApp("/knowledge");
    await screen.findByRole("heading", { level: 1, name: "知识库" });
    const main = screen.getByRole("main");

    // 列表页徽章核验：存在额外授权展示「额外共享 1 项」，无额外授权不展示。
    const sharedCard = (
      await within(main).findByRole("link", { name: /运营协作知识库/ })
    ).closest("li") as HTMLElement;
    expect(within(sharedCard).getByText("额外共享 1 项")).toBeInTheDocument();

    const ownerOnlyCard = within(main)
      .getByRole("link", { name: /纯本部门知识库/ })
      .closest("li") as HTMLElement;
    expect(
      within(ownerOnlyCard).queryByText(/额外共享/),
    ).not.toBeInTheDocument();

    // 1. 仅新增角色授权（名称与说明不变）-> 正常发送 PATCH 且只携带 visibility_grants。
    await user.click(
      within(sharedCard).getByRole("button", {
        name: "编辑知识库：运营协作知识库",
      }),
    );
    const editDialog1 = screen.getByRole("dialog", { name: "编辑知识库" });
    const grantsEditor1 = within(editDialog1).getByTestId(
      "visibility-grants-editor",
    );
    await user.selectOptions(
      within(grantsEditor1).getByLabelText("附加授权类型"),
      "role",
    );
    await user.selectOptions(
      within(grantsEditor1).getByLabelText("选择附加共享角色"),
      "member",
    );
    await user.click(
      within(grantsEditor1).getByRole("button", { name: "添加共享" }),
    );
    await user.click(
      within(editDialog1).getByRole("button", { name: "保存知识库" }),
    );
    await waitFor(() => expect(patchBodies).toHaveLength(1));
    expect(patchBodies[0]).toEqual({
      visibility_grants: [
        {
          include_subdepartments: true,
          target_id: "dept-fin",
          target_type: "department",
        },
        {
          include_subdepartments: true,
          target_id: "dept-rd",
          target_type: "department",
        },
        {
          include_subdepartments: false,
          target_id: "member",
          target_type: "role",
        },
      ],
    });

    // 2. 后端返回 403 visibility_grant_forbidden 时展示明确中文权限提示，且保留弹窗。
    shouldForbidNextPatch = true;
    await user.click(
      within(sharedCard).getByRole("button", {
        name: "编辑知识库：运营协作知识库",
      }),
    );
    const editDialog2 = screen.getByRole("dialog", { name: "编辑知识库" });
    await user.click(
      within(editDialog2).getByRole("button", { name: "移除附加授权：研发部" }),
    );
    await user.click(
      within(editDialog2).getByRole("button", { name: "保存知识库" }),
    );
    expect(
      await within(editDialog2).findByText(
        /没有权限配置所选部门或角色共享.*包含下级子部门/,
      ),
    ).toBeInTheDocument();

    // 3. 将范围切为 personal 并保存 -> 严禁携带 owner_department_id 与 visibility_grants。
    shouldForbidNextPatch = false;
    await user.selectOptions(
      within(editDialog2).getByLabelText("可见范围"),
      "personal",
    );
    await user.click(
      within(editDialog2).getByRole("button", { name: "保存知识库" }),
    );
    await waitFor(() => expect(patchBodies).toHaveLength(2));
    expect(patchBodies[1]).toEqual({ visibility_scope: "personal" });
    expect(patchBodies[1]).not.toHaveProperty("owner_department_id");
    expect(patchBodies[1]).not.toHaveProperty("visibility_grants");
  });

  it("编辑部门知识库时临时将归属部门切至既有附加部门再切回原归属部门，保留原附加授权且保存不触发误 PATCH", async () => {
    const superAdmin = makeSuperAdmin();
    let patchCalls = 0;
    const deptBase: KnowledgeBase = {
      ...KNOWLEDGE_BASE_A,
      id: "kb-owner-roundtrip",
      name: "财务研发联合制度库",
      owner_department_id: "dept-fin",
      visibility_grants: [
        {
          created_at: "2026-11-24T09:00:00Z",
          id: "g-fin",
          include_subdepartments: true,
          target_id: "dept-fin",
          target_type: "department",
        },
        {
          created_at: "2026-11-24T09:00:00Z",
          id: "g-rd",
          include_subdepartments: true,
          target_id: "dept-rd",
          target_type: "department",
        },
      ],
      visibility_scope: "department",
    };

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(superAdmin);
        }
        if (url.pathname === "/api/v1/admin/org/departments") {
          return jsonResponse({
            items: [
              {
                created_at: "2026-10-01T00:00:00Z",
                external_id: "ext-fin",
                id: "dept-fin",
                name: "财务部",
                order_index: 1,
                parent_id: "dept-root",
                path: "/dept-root/dept-fin/",
                status: "active",
                synced_at: "2026-11-24T00:00:00Z",
                updated_at: "2026-11-24T00:00:00Z",
              },
              {
                created_at: "2026-10-01T00:00:00Z",
                external_id: "ext-rd",
                id: "dept-rd",
                name: "研发部",
                order_index: 2,
                parent_id: "dept-root",
                path: "/dept-root/dept-rd/",
                status: "active",
                synced_at: "2026-11-24T00:00:00Z",
                updated_at: "2026-11-24T00:00:00Z",
              },
            ],
          });
        }
        if (
          url.pathname === `/api/v1/knowledge-bases/${deptBase.id}` &&
          init?.method === "PATCH"
        ) {
          patchCalls += 1;
          return jsonResponse(deptBase);
        }
        if (url.pathname === "/api/v1/knowledge-bases") {
          return jsonResponse(knowledgeListResponse([deptBase], 1, 12));
        }
        throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${url}`);
      }),
    );
    const user = userEvent.setup();

    renderApp("/knowledge");
    await screen.findByRole("heading", { level: 1, name: "知识库" });
    await screen.findByText("财务研发联合制度库");

    await user.click(
      screen.getByRole("button", { name: "编辑知识库：财务研发联合制度库" }),
    );
    const editDialog = screen.getByRole("dialog", { name: "编辑知识库" });
    const ownerSelect = await within(editDialog).findByLabelText("归属部门");
    await waitFor(() =>
      expect(within(ownerSelect).getByText("研发部")).toBeInTheDocument(),
    );

    // 初始包含附加部门「研发部」。
    expect(
      within(editDialog).getByRole("button", { name: "移除附加授权：研发部" }),
    ).toBeInTheDocument();

    // 先将归属部门临时切到「研发部」，再切回原归属部门「财务部」。
    await user.selectOptions(ownerSelect, "dept-rd");
    await user.selectOptions(ownerSelect, "dept-fin");

    // 回归断言：原附加授权「研发部」必须完整保留，且直接点击保存只关闭弹窗而不发 PATCH。
    const grantsEditor = within(editDialog).getByTestId(
      "visibility-grants-editor",
    );
    expect(
      within(grantsEditor).getByRole("button", {
        name: "移除附加授权：研发部",
      }),
    ).toBeInTheDocument();
    expect(within(grantsEditor).getByText("共 2 条授权")).toBeInTheDocument();

    await user.click(
      within(editDialog).getByRole("button", { name: "保存知识库" }),
    );
    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", { name: "编辑知识库" }),
      ).not.toBeInTheDocument();
    });
    expect(patchCalls).toBe(0);
  });
});
