import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../shared/api/client";
import {
  makeDependencyCheck,
  makeSkillDetails,
  makeSkillVersion,
  TEST_ADMIN,
} from "../../test/adminFixtures";
import { getRequestUrl, makeDeptAdmin } from "../../test/fixtures";
import { jsonResponse, renderApp } from "../../test/testUtils";
import type { OrgDepartment } from "../admin/model";
import { skillErrorMessage } from "./errorMessages";
import {
  skillFormat,
  validateOptionalAdminSlug,
  validateStarterPrompt,
  validateVersionLabel,
  type SkillAdminDetails,
  type SkillVersion,
} from "./model";

function requestUrl(input: RequestInfo | URL): URL {
  return new URL(getRequestUrl(input), "http://localhost");
}

function versionListResponse(
  details: SkillAdminDetails,
  items: SkillVersion[] = details.versions,
) {
  return {
    items,
    page: 1,
    page_size: 20,
    total: items.length,
  };
}

const ORG_DEPARTMENTS: OrgDepartment[] = [
  {
    created_at: "2026-10-01T00:00:00Z",
    external_id: null,
    id: "dept-fin",
    name: "财务部",
    order_index: 1,
    parent_id: null,
    path: "/dept-fin",
    status: "active",
    synced_at: null,
    updated_at: "2026-10-01T00:00:00Z",
  },
  {
    created_at: "2026-10-01T00:00:00Z",
    external_id: null,
    id: "dept-ops",
    name: "运营部",
    order_index: 2,
    parent_id: null,
    path: "/dept-ops",
    status: "active",
    synced_at: null,
    updated_at: "2026-10-01T00:00:00Z",
  },
  {
    created_at: "2026-10-01T00:00:00Z",
    external_id: null,
    id: "dept-archived",
    name: "已归档旧部",
    order_index: 3,
    parent_id: null,
    path: "/dept-archived",
    status: "archived",
    synced_at: null,
    updated_at: "2026-10-01T00:00:00Z",
  },
];

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("管理端公共/部门技能双形态与在线 Markdown 发布", () => {
  it("super_admin 支持在线新建 Markdown 技能：.md 解析回填、跨部门与角色附加授权、首页推荐与引导语", async () => {
    let createdPayload: Record<string, unknown> | null = null;
    let createIdempotencyKey: string | null = null;
    let createdSkill: SkillAdminDetails | null = null;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (url.pathname === "/api/v1/admin/skills" && method === "GET") {
          return jsonResponse({ items: createdSkill ? [createdSkill] : [] });
        }
        if (url.pathname === "/api/v1/skills" && method === "GET") {
          return jsonResponse({ items: [] });
        }
        if (
          url.pathname === "/api/v1/admin/org/departments" &&
          method === "GET"
        ) {
          return jsonResponse({ items: ORG_DEPARTMENTS });
        }
        if (
          url.pathname === "/api/v1/admin/skills/parse-markdown" &&
          method === "POST"
        ) {
          const body = init?.body;
          if (!(body instanceof FormData) || !(body.get("file") instanceof File)) {
            throw new Error("parse-markdown 必须使用 FormData file 字段");
          }
          return jsonResponse({
            description: "依据差旅制度逐项核查报销单合规性",
            guide: "# 报销核查\n\n1. 校验发票抬头。\n2. 核对标准限额。",
            name: "财务报销合规审核助手",
          });
        }
        if (
          url.pathname === "/api/v1/admin/skills/markdown" &&
          method === "POST"
        ) {
          createdPayload = JSON.parse(String(init?.body)) as Record<
            string,
            unknown
          >;
          createIdempotencyKey = new Headers(init?.headers).get(
            "Idempotency-Key",
          );
          createdSkill = makeSkillDetails(
            {
              current_version_id: "ver-md-1",
              current_version_label: "v1",
              dependency_check: makeDependencyCheck({
                python_implementation: "markdown-only",
                python_version: "not-checked",
              }),
              description: String(createdPayload.description),
              enabled: Boolean(createdPayload.enabled),
              featured: Boolean(createdPayload.featured),
              format: "markdown",
              has_scripts: false,
              id: "skill-md-created",
              name: String(createdPayload.name),
              owner_department_id: "dept-fin",
              revision: 1,
              scope: "department",
              slug: String(createdPayload.slug ?? "fin-expense-audit"),
              starter_prompt:
                (createdPayload.starter_prompt as string | null) ?? null,
              visibility_grants: [
                {
                  created_at: "2026-11-24T06:00:00Z",
                  id: "grant-1",
                  include_subdepartments: true,
                  target_id: "dept-fin",
                  target_type: "department",
                },
                {
                  created_at: "2026-11-24T06:00:00Z",
                  id: "grant-2",
                  include_subdepartments: true,
                  target_id: "dept-ops",
                  target_type: "department",
                },
                {
                  created_at: "2026-11-24T06:00:00Z",
                  id: "grant-3",
                  include_subdepartments: false,
                  target_id: "member",
                  target_type: "role",
                },
              ],
            },
            {
              description: String(createdPayload.description),
              guide: String(createdPayload.guide),
              id: "ver-md-1",
              name: String(createdPayload.name),
              scripts: [],
              slug: String(createdPayload.slug ?? "fin-expense-audit"),
            },
          );
          return jsonResponse(createdSkill, 201);
        }
        if (
          createdSkill &&
          url.pathname === `/api/v1/admin/skills/${createdSkill.id}` &&
          method === "GET"
        ) {
          return jsonResponse(createdSkill);
        }
        if (
          createdSkill &&
          url.pathname ===
            `/api/v1/admin/skills/${createdSkill.id}/versions` &&
          method === "GET"
        ) {
          return jsonResponse(versionListResponse(createdSkill));
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );

    const user = userEvent.setup();
    renderApp("/admin/skills");

    const openButtons = await screen.findAllByRole("button", {
      name: "新建在线模板技能",
    });
    const headerTrigger = openButtons[0];
    await user.click(headerTrigger);

    const dialog = await screen.findByRole("dialog", {
      name: /新建在线模板技能/,
    });
    await waitFor(() =>
      expect(within(dialog).getByLabelText("技能名称")).toHaveFocus(),
    );

    // 取消后焦点正确归还至触发按钮（受控 Radix Dialog 焦点闭环）
    await user.keyboard("{Escape}");
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: /新建在线模板技能/ }),
      ).not.toBeInTheDocument(),
    );
    expect(headerTrigger).toHaveFocus();

    // 再次打开并走完整创建流
    await user.click(headerTrigger);
    const createDialog = await screen.findByRole("dialog", {
      name: /新建在线模板技能/,
    });

    // 通过 MarkdownSplitEditor 工具栏导入 .md 自动回填
    const mdFile = new File(["# 报销核查"], "expense-audit.md", {
      type: "text/markdown",
    });
    fireEvent.change(within(createDialog).getByLabelText("导入 .md"), {
      target: { files: [mdFile] },
    });

    await waitFor(() =>
      expect(within(createDialog).getByLabelText("技能名称")).toHaveValue(
        "财务报销合规审核助手",
      ),
    );
    expect(within(createDialog).getByLabelText("技能简介")).toHaveValue(
      "依据差旅制度逐项核查报销单合规性",
    );
    expect(
      within(createDialog).getByLabelText("技能指南正文（Markdown）"),
    ).toHaveValue("# 报销核查\n\n1. 校验发票抬头。\n2. 核对标准限额。");

    await user.type(
      within(createDialog).getByLabelText("自定义标识（可选）"),
      "fin-expense-audit",
    );

    // 切换发布范围为部门共享并选择财务部
    await user.selectOptions(
      within(createDialog).getByLabelText("发布范围"),
      "department",
    );
    const ownerSelect = await within(createDialog).findByLabelText("归属部门");
    expect(within(ownerSelect).queryByText("已归档旧部")).not.toBeInTheDocument();
    await user.selectOptions(ownerSelect, "dept-fin");

    // VisibilityGrantsEditor：追加运营部（含子部门）与员工角色（include_subdepartments 恒为 false）
    const grantsEditor = within(createDialog).getByTestId(
      "visibility-grants-editor",
    );
    await user.selectOptions(
      within(grantsEditor).getByLabelText("选择附加共享部门"),
      "dept-ops",
    );
    await user.click(
      within(grantsEditor).getByRole("button", { name: "添加共享" }),
    );
    expect(within(grantsEditor).getByText("运营部")).toBeInTheDocument();

    await user.selectOptions(
      within(grantsEditor).getByLabelText("附加授权类型"),
      "role",
    );
    await user.selectOptions(
      within(grantsEditor).getByLabelText("选择附加共享角色"),
      "member",
    );
    await user.click(
      within(grantsEditor).getByRole("button", { name: "添加共享" }),
    );
    expect(within(grantsEditor).getByText("按系统角色生效")).toBeInTheDocument();

    // 开启首页推荐并填写首页默认引导语
    await user.click(
      within(createDialog).getByRole("switch", {
        name: "推荐至工作台首页快捷应用",
      }),
    );
    await user.type(
      within(createDialog).getByLabelText("首页默认引导语（可选）"),
      "请上传差旅或报销单截图/PDF，我将逐项核查合规性。",
    );

    await user.click(
      within(createDialog).getByRole("button", { name: "创建并启用模板技能" }),
    );

    await waitFor(() => expect(createdPayload).not.toBeNull());
    expect(createIdempotencyKey).toMatch(/^[A-Za-z0-9._~-]+$/);
    expect(createdPayload).toEqual({
      description: "依据差旅制度逐项核查报销单合规性",
      enabled: true,
      featured: true,
      guide: "# 报销核查\n\n1. 校验发票抬头。\n2. 核对标准限额。",
      name: "财务报销合规审核助手",
      owner_department_id: "dept-fin",
      scope: "department",
      slug: "fin-expense-audit",
      starter_prompt: "请上传差旅或报销单截图/PDF，我将逐项核查合规性。",
      visibility_grants: [
        {
          include_subdepartments: true,
          target_id: "dept-fin",
          target_type: "department",
        },
        {
          include_subdepartments: true,
          target_id: "dept-ops",
          target_type: "department",
        },
        {
          include_subdepartments: false,
          target_id: "member",
          target_type: "role",
        },
      ],
    });

    // 创建成功后自动展示详情弹窗，且具备在线模板双向格式锁（无 ZIP 上传区）
    const detailDialog = await screen.findByRole("dialog", {
      name: "技能：财务报销合规审核助手",
    });
    expect(
      within(detailDialog).getByRole("button", {
        name: "在线编辑并发布新版本",
      }),
    ).toBeInTheDocument();
    expect(
      within(detailDialog).queryByLabelText("选择新版本 ZIP"),
    ).not.toBeInTheDocument();
  });

  it("dept_admin 新建在线模板：未改授权省略 visibility_grants、追加管辖部门时归属默认不含子部门且不展示角色授权；零管辖部门阻断提交", async () => {
    const multiDeptAdmin = makeDeptAdmin({
      departments: [
        {
          id: "dept-fin",
          is_primary: true,
          name: "财务部",
          path: "/dept-fin",
        },
        {
          id: "dept-audit",
          is_primary: false,
          name: "内审部",
          path: "/dept-audit",
        },
      ],
      managed_department_ids: ["dept-fin", "dept-audit"],
    });
    const createBodies: Array<Record<string, unknown>> = [];
    const createdById = new Map<string, SkillAdminDetails>();
    let orgDeptCalls = 0;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(multiDeptAdmin);
        }
        if (url.pathname === "/api/v1/admin/org/departments") {
          orgDeptCalls += 1;
          return jsonResponse({ detail: "admin_required" }, 403);
        }
        if (url.pathname === "/api/v1/admin/skills" && method === "GET") {
          return jsonResponse({ items: [] });
        }
        if (url.pathname === "/api/v1/skills" && method === "GET") {
          return jsonResponse({ items: [] });
        }
        if (
          url.pathname === "/api/v1/admin/skills/markdown" &&
          method === "POST"
        ) {
          const parsedBody = JSON.parse(String(init?.body)) as Record<
            string,
            unknown
          >;
          createBodies.push(parsedBody);
          const created = makeSkillDetails({
            format: "markdown",
            id: `skill-dept-md-${createBodies.length}`,
            name: String(parsedBody.name),
            owner_department_id: "dept-fin",
            scope: "department",
          });
          createdById.set(created.id, created);
          return jsonResponse(created, 201);
        }
        const detailMatch = /^\/api\/v1\/admin\/skills\/(skill-dept-md-\d+)$/.exec(
          url.pathname,
        );
        if (detailMatch && method === "GET") {
          const matched = createdById.get(detailMatch[1]);
          if (matched) {
            return jsonResponse(matched);
          }
        }
        const versionsMatch =
          /^\/api\/v1\/admin\/skills\/(skill-dept-md-\d+)\/versions$/.exec(
            url.pathname,
          );
        if (versionsMatch && method === "GET") {
          const matched = createdById.get(versionsMatch[1]);
          if (matched) {
            return jsonResponse(versionListResponse(matched));
          }
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );

    const user = userEvent.setup();
    const firstRender = renderApp("/admin/skills");

    // 场景 A：dept_admin 未改动授权配置 -> 省略 visibility_grants 交由后端按实时子树自动派生
    await user.click(
      (await screen.findAllByRole("button", { name: "新建在线模板技能" }))[0],
    );
    const defaultDialog = await screen.findByRole("dialog", {
      name: /新建在线模板技能/,
    });
    expect(
      within(defaultDialog).queryByLabelText("发布范围"),
    ).not.toBeInTheDocument();
    await user.selectOptions(
      within(defaultDialog).getByLabelText("归属部门"),
      "dept-fin",
    );
    const defaultGrantsEditor = within(defaultDialog).getByTestId(
      "visibility-grants-editor",
    );
    expect(
      within(defaultGrantsEditor).getByRole("checkbox", {
        name: "包含下级子部门",
      }),
    ).not.toBeChecked();
    await user.type(
      within(defaultDialog).getByLabelText("技能名称"),
      "默认授权模板",
    );
    await user.type(
      within(defaultDialog).getByLabelText("技能简介"),
      "未改授权走后端默认派生",
    );
    await user.type(
      within(defaultDialog).getByLabelText("技能指南正文（Markdown）"),
      "## 默认指南",
    );
    await user.click(
      within(defaultDialog).getByRole("button", { name: "创建并启用模板技能" }),
    );
    await waitFor(() => expect(createBodies).toHaveLength(1));
    expect(createBodies[0]).not.toHaveProperty("visibility_grants");
    expect(createBodies[0]).toMatchObject({
      owner_department_id: "dept-fin",
      scope: "department",
    });

    // 关闭详情弹窗后进入场景 B：追加另一管辖部门，归属部门与新增部门默认 include_subdepartments: false
    const detailAfterFirst = await screen.findByRole("dialog", {
      name: /技能：/,
    });
    await user.click(
      within(detailAfterFirst).getByRole("button", { name: "关闭" }),
    );

    await user.click(
      (await screen.findAllByRole("button", { name: "新建在线模板技能" }))[0],
    );
    const dialog = await screen.findByRole("dialog", {
      name: /新建在线模板技能/,
    });
    await user.selectOptions(within(dialog).getByLabelText("归属部门"), "dept-fin");

    const grantsEditor = within(dialog).getByTestId("visibility-grants-editor");
    // 绝不暴露角色授权类型选择器，防止误触 403 visibility_grant_forbidden
    expect(
      within(grantsEditor).queryByLabelText("附加授权类型"),
    ).not.toBeInTheDocument();
    expect(
      within(grantsEditor).queryByLabelText("选择附加共享角色"),
    ).not.toBeInTheDocument();
    // 仅能追加本人另一管辖部门「内审部」
    const extraDeptSelect =
      within(grantsEditor).getByLabelText("选择附加共享部门");
    expect(within(extraDeptSelect).getByText("内审部")).toBeInTheDocument();
    await user.selectOptions(extraDeptSelect, "dept-audit");
    await user.click(
      within(grantsEditor).getByRole("button", { name: "添加共享" }),
    );

    await user.type(within(dialog).getByLabelText("技能名称"), "部门报销模板");
    await user.type(within(dialog).getByLabelText("技能简介"), "部门报销核对");
    await user.type(
      within(dialog).getByLabelText("技能指南正文（Markdown）"),
      "## 步骤\n- 核查金额",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "创建并启用模板技能" }),
    );

    await waitFor(() => expect(createBodies).toHaveLength(2));
    expect(orgDeptCalls).toBe(0);
    expect(createBodies[1]).toMatchObject({
      owner_department_id: "dept-fin",
      scope: "department",
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
    firstRender.unmount();

    // 零管辖部门的 dept_admin 必须阻断提交并给出清晰提示
    const emptyDeptAdmin = makeDeptAdmin({
      departments: [],
      managed_department_ids: [],
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(emptyDeptAdmin);
        }
        if (url.pathname === "/api/v1/admin/skills") {
          return jsonResponse({ items: [] });
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    renderApp("/admin/skills");
    await user.click(
      (await screen.findAllByRole("button", { name: "新建在线模板技能" }))[0],
    );
    const blockedDialog = await screen.findByRole("dialog", {
      name: /新建在线模板技能/,
    });
    expect(within(blockedDialog).getByText("暂无可管辖部门")).toBeInTheDocument();
    expect(
      within(blockedDialog).getByRole("button", { name: "创建并启用模板技能" }),
    ).toBeDisabled();
  });

  it("双向格式锁 UI 与在线发版：markdown 隐藏 ZIP 上传并无叠窗切换发版（含 activate=false 与 expected_revision）；package 隐藏在线编辑入口", async () => {
    const mdSkill = makeSkillDetails(
      {
        current_version_id: "ver-md-1",
        current_version_label: "v1",
        description: "合同条款快速审查",
        enabled: true,
        featured: true,
        format: "markdown",
        has_scripts: false,
        id: "skill-md-1",
        name: "合同审查助手",
        revision: 2,
        scope: "company",
        slug: "contract-review",
        starter_prompt: "请粘贴或上传合同正文，我将标出核心风险条款。",
      },
      {
        description: "合同条款快速审查",
        guide: "# 合同审查指南\n\n检查违约责任与付款周期。",
        id: "ver-md-1",
        name: "合同审查助手",
        ordinal: 1,
        scripts: [],
        skill_id: "skill-md-1",
        slug: "contract-review",
      },
    );

    const pkgSkill = makeSkillDetails(
      {
        current_version_id: "ver-pkg-1",
        current_version_label: "v1",
        description: "带 Python 脚本的报表包",
        enabled: true,
        featured: false,
        format: "package",
        has_scripts: true,
        id: "skill-pkg-1",
        name: "自动化销售脚本包",
        revision: 1,
        scope: "company",
        slug: "sales-script-pkg",
      },
      {
        id: "ver-pkg-1",
        name: "自动化销售脚本包",
        ordinal: 1,
        skill_id: "skill-pkg-1",
        slug: "sales-script-pkg",
      },
    );

    let publishPayload: Record<string, unknown> | null = null;
    let publishKey: string | null = null;
    let updatedMdSkill = mdSkill;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (url.pathname === "/api/v1/admin/skills" && method === "GET") {
          return jsonResponse({ items: [updatedMdSkill, pkgSkill] });
        }
        if (url.pathname === "/api/v1/skills" && method === "GET") {
          return jsonResponse({ items: [] });
        }
        if (
          url.pathname === `/api/v1/admin/skills/${mdSkill.id}` &&
          method === "GET"
        ) {
          return jsonResponse(updatedMdSkill);
        }
        if (
          url.pathname === `/api/v1/admin/skills/${mdSkill.id}/versions` &&
          method === "GET"
        ) {
          return jsonResponse(versionListResponse(updatedMdSkill));
        }
        if (
          url.pathname ===
            `/api/v1/admin/skills/${mdSkill.id}/versions/markdown` &&
          method === "POST"
        ) {
          publishPayload = JSON.parse(String(init?.body)) as Record<
            string,
            unknown
          >;
          publishKey = new Headers(init?.headers).get("Idempotency-Key");
          const v2 = makeSkillVersion({
            description: String(publishPayload.description),
            guide: String(publishPayload.guide),
            id: "ver-md-2",
            label: String(publishPayload.label ?? "v2"),
            name: String(publishPayload.name),
            ordinal: 2,
            scripts: [],
            skill_id: mdSkill.id,
            slug: mdSkill.slug,
          });
          updatedMdSkill = {
            ...mdSkill,
            revision: 3,
            versions: [v2, ...mdSkill.versions],
          };
          return jsonResponse(updatedMdSkill, 201);
        }
        if (
          url.pathname === `/api/v1/admin/skills/${pkgSkill.id}` &&
          method === "GET"
        ) {
          return jsonResponse(pkgSkill);
        }
        if (
          url.pathname === `/api/v1/admin/skills/${pkgSkill.id}/versions` &&
          method === "GET"
        ) {
          return jsonResponse(versionListResponse(pkgSkill));
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );

    const user = userEvent.setup();
    renderApp("/admin/skills");

    // 列表卡片正确展示形态徽章、首页推荐徽章与引导语摘要
    expect(await screen.findByText("合同审查助手")).toBeInTheDocument();
    expect(screen.getByText("在线模板")).toBeInTheDocument();
    expect(screen.getByText("脚本包")).toBeInTheDocument();
    expect(screen.getByText("首页推荐")).toBeInTheDocument();
    expect(
      screen.getByText(/请粘贴或上传合同正文，我将标出核心风险条款。/),
    ).toBeInTheDocument();

    // 1. 打开在线模板技能详情：展示在线编辑按钮，隐藏 ZIP 上传区
    const viewButtons = screen.getAllByRole("button", {
      name: "查看版本与指南",
    });
    await user.click(viewButtons[0]);
    const mdDetail = await screen.findByRole("dialog", {
      name: "技能：合同审查助手",
    });
    expect(
      within(mdDetail).queryByLabelText("选择新版本 ZIP"),
    ).not.toBeInTheDocument();

    // 点击「在线编辑并发布新版本」：详情旧 Modal 关闭，仅留 1 个 Radix 发版 Dialog（零叠窗）
    await user.click(
      within(mdDetail).getByRole("button", { name: "在线编辑并发布新版本" }),
    );
    const publishDialog = await screen.findByRole("dialog", {
      name: /在线编辑并发布新版本：合同审查助手/,
    });
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    expect(within(publishDialog).getByLabelText("技能名称")).toHaveValue(
      "合同审查助手",
    );
    expect(
      within(publishDialog).getByLabelText("技能指南正文（Markdown）"),
    ).toHaveValue("# 合同审查指南\n\n检查违约责任与付款周期。");

    await user.type(
      within(publishDialog).getByLabelText("版本标签（可选）"),
      "v2.0-2026Q4",
    );
    await user.type(
      within(publishDialog).getByLabelText("技能指南正文（Markdown）"),
      "\n- 增补保密条款核查",
    );
    // 取消勾选「发布后立即设为当前生效版本」，测试 activate=false
    await user.click(
      within(publishDialog).getByRole("checkbox", {
        name: "发布后立即设为当前生效版本",
      }),
    );
    await user.click(
      within(publishDialog).getByRole("button", { name: "仅发布新版本" }),
    );

    await waitFor(() => expect(publishPayload).not.toBeNull());
    expect(publishKey).toMatch(/^[A-Za-z0-9._~-]+$/);
    expect(publishPayload).toEqual({
      activate: false,
      description: "合同条款快速审查",
      expected_revision: 2,
      guide: "# 合同审查指南\n\n检查违约责任与付款周期。\n- 增补保密条款核查",
      label: "v2.0-2026Q4",
      name: "合同审查助手",
    });

    // 关闭重新打开的详情窗后，验证 package 技能双向格式锁：保留 ZIP 上传，隐藏在线编辑入口
    const reopenedDetail = await screen.findByRole("dialog", {
      name: "技能：合同审查助手",
    });
    await user.click(within(reopenedDetail).getByRole("button", { name: "关闭" }));

    await user.click(
      screen.getAllByRole("button", { name: "查看版本与指南" })[1],
    );
    const pkgDetail = await screen.findByRole("dialog", {
      name: "技能：自动化销售脚本包",
    });
    expect(
      within(pkgDetail).getByLabelText("选择新版本 ZIP"),
    ).toBeInTheDocument();
    expect(
      within(pkgDetail).queryByRole("button", { name: "在线编辑并发布新版本" }),
    ).not.toBeInTheDocument();
  });

  it("详情页 PATCH 支持保存 featured=true、清空 starter_prompt 为 null 与更新部门附加共享授权", async () => {
    const initial = makeSkillDetails({
      enabled: true,
      featured: false,
      format: "markdown",
      id: "skill-patch-meta",
      name: "报销审核模板",
      owner_department_id: "dept-fin",
      revision: 4,
      scope: "department",
      slug: "fin-reimburse",
      starter_prompt: "旧版报销引导语",
      visibility_grants: [
        {
          created_at: "2026-11-24T05:00:00Z",
          id: "g-1",
          include_subdepartments: true,
          target_id: "dept-fin",
          target_type: "department",
        },
      ],
    });
    let patchBody: Record<string, unknown> | null = null;
    const updated: SkillAdminDetails = {
      ...initial,
      featured: true,
      revision: 5,
      starter_prompt: null,
      visibility_grants: [
        {
          created_at: "2026-11-24T05:00:00Z",
          id: "g-1",
          include_subdepartments: false,
          target_id: "dept-fin",
          target_type: "department",
        },
      ],
    };

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (url.pathname === "/api/v1/admin/org/departments") {
          return jsonResponse({ items: ORG_DEPARTMENTS });
        }
        if (url.pathname === "/api/v1/admin/skills" && method === "GET") {
          return jsonResponse({ items: [patchBody ? updated : initial] });
        }
        if (url.pathname === "/api/v1/skills" && method === "GET") {
          return jsonResponse({ items: [] });
        }
        if (
          url.pathname === `/api/v1/admin/skills/${initial.id}` &&
          method === "GET"
        ) {
          return jsonResponse(patchBody ? updated : initial);
        }
        if (
          url.pathname === `/api/v1/admin/skills/${initial.id}/versions` &&
          method === "GET"
        ) {
          return jsonResponse(versionListResponse(patchBody ? updated : initial));
        }
        if (
          url.pathname === `/api/v1/admin/skills/${initial.id}` &&
          method === "PATCH"
        ) {
          patchBody = JSON.parse(String(init?.body)) as Record<string, unknown>;
          return jsonResponse(updated);
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );

    const user = userEvent.setup();
    renderApp("/admin/skills");

    await user.click(
      await screen.findByRole("button", { name: "查看版本与指南" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "技能：报销审核模板",
    });

    await user.click(
      within(dialog).getByRole("switch", { name: "推荐至工作台首页" }),
    );
    const promptInput = within(dialog).getByLabelText("首页默认引导语");
    expect(promptInput).toHaveValue("旧版报销引导语");
    await user.clear(promptInput);

    // 取消勾选默认归属部门的「包含下级子部门」
    await user.click(
      within(dialog).getByRole("checkbox", { name: "包含下级子部门" }),
    );
    await user.click(
      within(dialog).getByRole("button", { name: "保存当前配置" }),
    );

    await waitFor(() => expect(patchBody).not.toBeNull());
    expect(patchBody).toEqual({
      current_version_id: initial.current_version_id,
      enabled: true,
      expected_revision: 4,
      featured: true,
      script_requires_approval: false,
      starter_prompt: null,
      visibility_grants: [
        {
          include_subdepartments: false,
          target_id: "dept-fin",
          target_type: "department",
        },
      ],
    });
  });

  it("纯函数校验与 M3.3 错误码映射符合契约边界", () => {
    expect(skillFormat({ scope: "personal" })).toBe("markdown");
    expect(skillFormat({ scope: "company" })).toBe("package");
    expect(skillFormat({ format: "markdown", scope: "company" })).toBe(
      "markdown",
    );

    expect(validateStarterPrompt("   ")).toBeNull();
    expect(validateStarterPrompt("请上传报销单\n逐项审核\t完成")).toBeNull();
    expect(validateStarterPrompt("a".repeat(501))).toMatch(/500/);
    expect(validateStarterPrompt("bad\u0007text")).toMatch(/控制字符/);

    expect(validateOptionalAdminSlug("")).toBeNull();
    expect(validateOptionalAdminSlug("a")).toBeNull();
    expect(validateOptionalAdminSlug("fin-expense-audit-2026")).toBeNull();
    expect(validateOptionalAdminSlug("Invalid_Slug")).toMatch(/小写字母/);
    expect(validateOptionalAdminSlug("-leading")).toMatch(/首尾/);

    expect(validateVersionLabel("")).toBeNull();
    expect(validateVersionLabel("v2.0-2026Q4")).toBeNull();
    expect(validateVersionLabel("x".repeat(65))).toMatch(/64/);
    expect(validateVersionLabel("v1\nv2")).toMatch(/控制字符/);

    expect(
      skillErrorMessage(new ApiError(409, "skill_markdown_edit_unsupported")),
    ).toContain("ZIP 技能包");
    expect(
      skillErrorMessage(new ApiError(409, "skill_package_upload_unsupported")),
    ).toContain("在线 Markdown");
    expect(
      skillErrorMessage(new ApiError(409, "skill_slug_conflict")),
    ).toContain("slug");
    expect(
      skillErrorMessage(new ApiError(403, "visibility_grant_forbidden")),
    ).toContain("没有权限");
    expect(
      skillErrorMessage(new ApiError(422, "visibility_grant_duplicate")),
    ).toContain("重复");
  });

  it("网络失败重试复用同一 Idempotency-Key、修改正文后轮换新键，且发版遇 409 skill_revision_conflict 给出修订冲突提示", async () => {
    const keys: string[] = [];
    let failFirstCreate = true;
    const mdSkill = makeSkillDetails({
      current_version_id: "ver-md-1",
      format: "markdown",
      id: "skill-md-conflict",
      name: "周报模板",
      revision: 3,
      scope: "company",
      slug: "weekly-md",
    });

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (url.pathname === "/api/v1/admin/skills" && method === "GET") {
          return jsonResponse({ items: [mdSkill] });
        }
        if (url.pathname === "/api/v1/skills" && method === "GET") {
          return jsonResponse({ items: [] });
        }
        if (
          url.pathname === "/api/v1/admin/skills/markdown" &&
          method === "POST"
        ) {
          const key = new Headers(init?.headers).get("Idempotency-Key") ?? "";
          keys.push(key);
          if (failFirstCreate) {
            failFirstCreate = false;
            return jsonResponse({ detail: "file_storage_unavailable" }, 503);
          }
          return jsonResponse(mdSkill, 201);
        }
        if (
          url.pathname === `/api/v1/admin/skills/${mdSkill.id}` &&
          method === "GET"
        ) {
          return jsonResponse(mdSkill);
        }
        if (
          url.pathname === `/api/v1/admin/skills/${mdSkill.id}/versions` &&
          method === "GET"
        ) {
          return jsonResponse(versionListResponse(mdSkill));
        }
        if (
          url.pathname ===
            `/api/v1/admin/skills/${mdSkill.id}/versions/markdown` &&
          method === "POST"
        ) {
          return jsonResponse({ detail: "skill_revision_conflict" }, 409);
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );

    const user = userEvent.setup();
    renderApp("/admin/skills");

    // 1. 新建遇 503 重试复用原 Idempotency-Key
    await user.click(
      await screen.findByRole("button", { name: "新建在线模板技能" }),
    );
    const createDialog = await screen.findByRole("dialog", {
      name: /新建在线模板技能/,
    });
    await user.type(within(createDialog).getByLabelText("技能名称"), "周报模板");
    await user.type(within(createDialog).getByLabelText("技能简介"), "整理周报");
    await user.type(
      within(createDialog).getByLabelText("技能指南正文（Markdown）"),
      "# 周报指南",
    );
    await user.click(
      within(createDialog).getByRole("button", { name: "创建并启用模板技能" }),
    );
    expect(
      await within(createDialog).findByText(/存储暂时不可用/),
    ).toBeInTheDocument();
    expect(keys).toHaveLength(1);

    // 未改动任何字段直接重试 -> 复用同一 Idempotency-Key
    await user.click(
      within(createDialog).getByRole("button", { name: "创建并启用模板技能" }),
    );
    await waitFor(() => expect(keys).toHaveLength(2));
    expect(keys[1]).toBe(keys[0]);

    // 2. 在线发版遇 409 skill_revision_conflict 给出明确冲突告警并保留草稿
    const detailDialog = await screen.findByRole("dialog", {
      name: "技能：周报模板",
    });
    await user.click(
      within(detailDialog).getByRole("button", { name: "在线编辑并发布新版本" }),
    );
    const versionDialog = await screen.findByRole("dialog", {
      name: /在线编辑并发布新版本：周报模板/,
    });
    await user.type(
      within(versionDialog).getByLabelText("技能指南正文（Markdown）"),
      "\n补充风险章节",
    );
    await user.click(
      within(versionDialog).getByRole("button", { name: "发布并设为当前版本" }),
    );

    expect(
      await within(versionDialog).findByText("修订号冲突"),
    ).toBeInTheDocument();
    // 409 修订号冲突合并展示为单一警告横幅，不重复渲染第二条 requestError
    expect(within(versionDialog).getAllByRole("alert")).toHaveLength(1);
    expect(
      within(versionDialog).getByLabelText("技能指南正文（Markdown）"),
    ).toHaveValue(
      "# 月度销售报表\n\n1. 读取输入文件。\n2. 生成交付。\n补充风险章节",
    );
  });

  it("无 frontmatter 导入保留已填名称/简介、slug 冲突精准标红字段，且在线发版取消/Escape 焦点回归「查看版本与指南」", async () => {
    const mdSkill = makeSkillDetails({
      current_version_id: "ver-md-focus",
      format: "markdown",
      id: "skill-md-focus",
      name: "焦点验证技能",
      revision: 1,
      scope: "company",
      slug: "focus-md-skill",
    });

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (url.pathname === "/api/v1/admin/skills" && method === "GET") {
          return jsonResponse({ items: [mdSkill] });
        }
        if (
          url.pathname === "/api/v1/admin/skills/parse-markdown" &&
          method === "POST"
        ) {
          // 模拟无 YAML frontmatter 的纯正文 .md 文件，后端返回空 name / description
          return jsonResponse({
            description: "",
            guide: "# 纯正文模板\n\n无元数据头。",
            name: "",
          });
        }
        if (
          url.pathname === "/api/v1/admin/skills/markdown" &&
          method === "POST"
        ) {
          return jsonResponse({ detail: "skill_slug_conflict" }, 409);
        }
        if (
          url.pathname === `/api/v1/admin/skills/${mdSkill.id}` &&
          method === "GET"
        ) {
          return jsonResponse(mdSkill);
        }
        if (
          url.pathname === `/api/v1/admin/skills/${mdSkill.id}/versions` &&
          method === "GET"
        ) {
          return jsonResponse(versionListResponse(mdSkill));
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );

    const user = userEvent.setup();
    renderApp("/admin/skills");

    // 1. 新建弹窗：先手填名称与简介，再导入无 frontmatter 的 .md -> 保留原名称与简介，仅更新正文；
    // 提交遇 409 skill_slug_conflict 时同步标红「自定义标识（可选）」字段
    await user.click(
      await screen.findByRole("button", { name: "新建在线模板技能" }),
    );
    const createDialog = await screen.findByRole("dialog", {
      name: /新建在线模板技能/,
    });
    await user.type(within(createDialog).getByLabelText("技能名称"), "原定技能名");
    await user.type(within(createDialog).getByLabelText("技能简介"), "原定简介");
    await user.type(
      within(createDialog).getByLabelText("自定义标识（可选）"),
      "dup-slug",
    );

    fireEvent.change(within(createDialog).getByLabelText("导入 .md"), {
      target: {
        files: [new File(["# 纯正文模板"], "plain.md", { type: "text/markdown" })],
      },
    });
    await waitFor(() =>
      expect(
        within(createDialog).getByLabelText("技能指南正文（Markdown）"),
      ).toHaveValue("# 纯正文模板\n\n无元数据头。"),
    );
    expect(within(createDialog).getByLabelText("技能名称")).toHaveValue(
      "原定技能名",
    );
    expect(within(createDialog).getByLabelText("技能简介")).toHaveValue(
      "原定简介",
    );

    await user.click(
      within(createDialog).getByRole("button", { name: "创建并启用模板技能" }),
    );
    const slugInput = within(createDialog).getByLabelText("自定义标识（可选）");
    await waitFor(() =>
      expect(slugInput).toHaveAttribute("aria-invalid", "true"),
    );
    await user.click(within(createDialog).getByRole("button", { name: "取消" }));

    // 2. 从列表「查看版本与指南」进入旧 <Modal> -> 切入在线发版 Radix Dialog -> Escape / 取消均把焦点归还至「查看版本与指南」
    const viewTrigger = await screen.findByRole("button", {
      name: "查看版本与指南",
    });
    await user.click(viewTrigger);
    const detailModal = await screen.findByRole("dialog", {
      name: "技能：焦点验证技能",
    });
    await user.click(
      within(detailModal).getByRole("button", { name: "在线编辑并发布新版本" }),
    );
    await screen.findByRole("dialog", {
      name: /在线编辑并发布新版本：焦点验证技能/,
    });
    await user.keyboard("{Escape}");
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: /在线编辑并发布新版本/ }),
      ).not.toBeInTheDocument(),
    );
    expect(viewTrigger).toHaveFocus();

    // 再次进入并点击「取消」同样将焦点安全归还至「查看版本与指南」
    await user.click(viewTrigger);
    const detailModal2 = await screen.findByRole("dialog", {
      name: "技能：焦点验证技能",
    });
    await user.click(
      within(detailModal2).getByRole("button", { name: "在线编辑并发布新版本" }),
    );
    const versionDialog2 = await screen.findByRole("dialog", {
      name: /在线编辑并发布新版本：焦点验证技能/,
    });
    await user.click(within(versionDialog2).getByRole("button", { name: "取消" }));
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: /在线编辑并发布新版本/ }),
      ).not.toBeInTheDocument(),
    );
    expect(viewTrigger).toHaveFocus();
  });
});
