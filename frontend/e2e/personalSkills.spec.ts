import { expect, test, type Page, type Route } from "@playwright/test";

const NOW = "2026-11-19T08:00:00Z";

// 对齐二期三层角色与企业组织身份投影契约（member）。
const USER = {
  auth_provider: "mock" as const,
  avatar_url: null,
  created_at: "2026-11-01T00:00:00Z",
  departments: [
    {
      id: "dept-product",
      is_primary: true,
      name: "产品部",
    },
  ],
  display_name: "个人技能验收用户",
  email: "personal-skills@example.com",
  employee_no: "E1008",
  id: "user-personal-skills",
  job_title: "产品经理",
  managed_department_ids: [] as string[],
  role: "member" as const,
};

// 对齐后端 SkillDependencyCheckResponse 契约：版本级 dependency_check 始终为非空对象。
interface SkillDependencyCheckFixture {
  checked_at: string;
  items: unknown[];
  python_implementation: string;
  python_version: string;
  status: "satisfied";
}

const MARKDOWN_DEPENDENCY_CHECK: SkillDependencyCheckFixture = {
  checked_at: NOW,
  items: [],
  python_implementation: "markdown-only",
  python_version: "not-checked",
  status: "satisfied",
};

interface SkillVersionFixture {
  created_at: string;
  dependencies: string[];
  dependency_check: SkillDependencyCheckFixture;
  description: string;
  files: Array<{
    path: string;
    sha256: string;
    size_bytes: number;
    text: boolean;
  }>;
  guide: string;
  id: string;
  label: string;
  name: string;
  ordinal: number;
  package_sha256: string;
  scripts: unknown[];
  skill_id: string;
  slug: string;
}

interface SkillFixture {
  current_version_id: string;
  current_version_label: string;
  dependency_check: SkillDependencyCheckFixture | null;
  description: string;
  enabled: boolean;
  featured: boolean;
  format: "markdown" | "package";
  has_scripts: boolean;
  id: string;
  name: string;
  owner_department_id: string | null;
  revision: number;
  scope: "company" | "personal";
  script_requires_approval: boolean;
  slug: string;
  starter_prompt: string | null;
  updated_at: string;
  versions?: SkillVersionFixture[];
  visibility_grants: unknown[];
}

interface PersonalSkillFixtureState {
  companySkill: SkillFixture;
  personal: SkillFixture | null;
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    body: JSON.stringify(body),
    contentType: "application/json",
    status,
  });
}

function makeVersion(
  skill: Pick<SkillFixture, "id" | "slug">,
  input: { description: string; guide: string; name: string },
  ordinal = 1,
): SkillVersionFixture {
  return {
    created_at: NOW,
    dependencies: [],
    // 个人纯 Markdown 技能版本固定返回非空 satisfied 依赖检查结果，供 toSkillVersion 解析。
    dependency_check: MARKDOWN_DEPENDENCY_CHECK,
    description: input.description,
    files: [
      {
        path: "SKILL.md",
        sha256: "a".repeat(64),
        size_bytes: 128,
        text: true,
      },
    ],
    guide: input.guide,
    id: `${skill.id}-version-${ordinal}`,
    label: `v${ordinal}`,
    name: input.name,
    ordinal,
    package_sha256: String(ordinal).padEnd(64, "b"),
    scripts: [],
    skill_id: skill.id,
    slug: skill.slug,
  };
}

async function installPersonalSkillsFixtures(
  page: Page,
): Promise<PersonalSkillFixtureState> {
  const state: PersonalSkillFixtureState = {
    companySkill: {
      current_version_id: "company-version-1",
      current_version_label: "v1",
      dependency_check: null,
      description: "需要审批脚本的全公司技能。",
      enabled: true,
      featured: false,
      format: "package",
      has_scripts: true,
      id: "company-skill-1",
      name: "全公司审批技能",
      owner_department_id: null,
      revision: 1,
      scope: "company",
      script_requires_approval: true,
      slug: "company-approval-skill",
      starter_prompt: null,
      updated_at: NOW,
      visibility_grants: [],
    },
    personal: null,
  };

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === "/api/v1/auth/providers" && method === "GET") {
      return json(route, {
        feishu_enabled: false,
        local_login_enabled: true,
        mock_enabled: true,
      });
    }
    if (path === "/api/v1/auth/me") {
      return json(route, USER);
    }
    if (path === "/api/v1/models" && method === "GET") {
      return json(route, {
        default_model_id: "model-general",
        items: [
          {
            id: "model-general",
            is_default: true,
            name: "Athena General",
            scenario_description: "日常办公问答与文档协作",
            scenario_label: "通用办公",
            supports_vision: false,
          },
        ],
      });
    }
    if (path === "/api/v1/knowledge-bases" && method === "GET") {
      return json(route, {
        items: [],
        page: 1,
        page_size: Number(url.searchParams.get("page_size") ?? 20),
        total: 0,
      });
    }
    if (path === "/api/v1/me/skills" && method === "GET") {
      return json(route, {
        items: state.personal ? [state.personal] : [],
      });
    }
    if (path === "/api/v1/me/skills" && method === "POST") {
      const input = request.postDataJSON() as {
        description: string;
        guide: string;
        name: string;
      };
      const identity = {
        id: "personal-skill-e2e",
        slug: "personal-skill-e2e",
      };
      state.personal = {
        current_version_id: "personal-skill-e2e-version-1",
        current_version_label: "v1",
        dependency_check: MARKDOWN_DEPENDENCY_CHECK,
        description: input.description,
        enabled: true,
        featured: false,
        format: "markdown",
        has_scripts: false,
        id: identity.id,
        name: input.name,
        owner_department_id: null,
        revision: 1,
        scope: "personal",
        script_requires_approval: false,
        slug: identity.slug,
        starter_prompt: null,
        updated_at: NOW,
        versions: [makeVersion(identity, input)],
        visibility_grants: [],
      };
      return json(route, state.personal, 201);
    }
    if (path === "/api/v1/me/skills/parse" && method === "POST") {
      return json(route, {
        description: "按证据梳理访谈并标注未知项。",
        guide: "# 访谈整理\n\n只记录有原文依据的结论。",
        name: "访谈整理",
      });
    }

    const personalMatch =
      /^\/api\/v1\/me\/skills\/([^/]+)(\/versions)?$/.exec(path);
    if (personalMatch && state.personal) {
      const skillId = decodeURIComponent(personalMatch[1]);
      const versionsPath = Boolean(personalMatch[2]);
      if (skillId !== state.personal.id) {
        return json(route, { detail: "skill_not_found" }, 404);
      }
      if (!versionsPath && method === "GET") {
        return json(route, state.personal);
      }
      if (versionsPath && method === "POST") {
        const input = request.postDataJSON() as {
          description: string;
          expected_revision: number;
          guide: string;
          name: string;
        };
        const ordinal = state.personal.versions!.length + 1;
        const version = makeVersion(
          {
            id: state.personal.id,
            slug: state.personal.slug,
          },
          input,
          ordinal,
        );
        state.personal = {
          ...state.personal,
          current_version_id: version.id,
          current_version_label: version.label,
          description: input.description,
          enabled: true,
          name: input.name,
          revision: input.expected_revision + 1,
          updated_at: NOW,
          versions: [version, ...state.personal.versions!],
        };
        return json(route, state.personal, 201);
      }
      if (!versionsPath && method === "PATCH") {
        const input = request.postDataJSON() as {
          enabled: boolean;
          expected_revision: number;
        };
        state.personal = {
          ...state.personal,
          enabled: input.enabled,
          revision: input.expected_revision + 1,
          updated_at: NOW,
        };
        return json(route, state.personal);
      }
      if (!versionsPath && method === "DELETE") {
        state.personal = null;
        return route.fulfill({ status: 204 });
      }
    }
    if (path === "/api/v1/skills" && method === "GET") {
      return json(route, {
        items: [
          state.companySkill,
          ...(state.personal?.enabled ? [state.personal] : []),
        ],
      });
    }
    if (path === "/api/v1/projects" && method === "GET") {
      return json(route, {
        items: [],
        page: 1,
        page_size: Number(url.searchParams.get("page_size") ?? 20),
        total: 0,
      });
    }
    if (path === "/api/v1/sessions" && method === "GET") {
      return json(route, {
        items: [],
        page: Number(url.searchParams.get("page") ?? 1),
        page_size: Number(url.searchParams.get("page_size") ?? 20),
        total: 0,
      });
    }

    return json(route, { detail: "personal_skills_e2e_mock_missing" }, 404);
  });

  return state;
}

async function expectNoHorizontalOverflow(page: Page) {
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          document.documentElement.scrollWidth - window.innerWidth,
      ),
    )
    .toBeLessThanOrEqual(1);
}

test.describe("个人技能前端 mock 浏览器验收", () => {
  test("技能库顶部 Tabs 键盘切换与浏览器后退保持有效焦点", async ({
    page,
  }) => {
    await installPersonalSkillsFixtures(page);
    await page.setViewportSize({ height: 900, width: 1280 });
    await page.goto("/skills");

    const viewTabs = page.getByRole("tablist", {
      name: "技能库视图",
    });
    const availableTab = viewTabs.getByRole("tab", { name: "可用技能" });
    const mineTab = viewTabs.getByRole("tab", { name: "我的技能" });

    await availableTab.focus();
    await page.keyboard.press("ArrowRight");
    await expect(page).toHaveURL(/\/skills\?view=mine$/);
    await expect(mineTab).toHaveAttribute("aria-selected", "true");
    await expect(mineTab).toBeFocused();

    await page.goBack();
    await expect(page).toHaveURL(/\/skills$/);
    await expect(availableTab).toHaveAttribute("aria-selected", "true");
    await expect(availableTab).toBeFocused();
  });

  test("桌面主流程：导航、创建、编辑、启停与删除", async ({ page }) => {
    await installPersonalSkillsFixtures(page);
    await page.setViewportSize({ height: 900, width: 1280 });

    await page.goto("/chat");
    await expect(
      page.getByRole("heading", { level: 1, name: "你好，个人技能验收用户" }),
    ).toBeVisible();
    await page.getByRole("link", { name: "技能库" }).click();
    await expect(page).toHaveURL(/\/skills$/);
    await expect(
      page.getByRole("heading", { level: 1, name: "技能库" }),
    ).toBeVisible();
    await expect(
      page.getByRole("link", { name: "技能库" }),
    ).toHaveAttribute("aria-current", "page");
    await expect(
      page.getByRole("heading", { name: "全公司审批技能" }),
    ).toBeVisible();

    await page
      .getByRole("tablist", { name: "技能库视图" })
      .getByRole("tab", { name: "我的技能" })
      .click();
    await expect(page).toHaveURL(/\/skills\?view=mine$/);

    await page
      .getByRole("button", { name: "新建第一个个人技能" })
      .click();
    const createDialog = page.getByRole("dialog", {
      name: "新建个人技能",
    });
    await createDialog.getByLabel("技能名称").fill("会议纪要");
    await createDialog
      .getByLabel("技能描述")
      .fill("提取决策、负责人和截止时间。");
    await createDialog
      .getByLabel("Markdown 正文")
      .fill("# 会议纪要\n\n只记录明确确认的信息。");
    await createDialog
      .getByRole("button", { name: "创建并启用" })
      .click();

    await expect(
      page.getByText(/已创建并启用「会议纪要」/),
    ).toContainText("不会自动加入当前对话");
    const card = page
      .locator(".personal-skill-card")
      .filter({ hasText: "会议纪要" });
    await expect(card).toBeVisible();
    await expect(card.getByText("个人", { exact: true })).toBeVisible();
    await expect(card.getByText("已启用", { exact: true })).toBeVisible();
    await expectNoHorizontalOverflow(page);

    await card
      .getByRole("button", { name: "编辑个人技能：会议纪要" })
      .click();
    const editDialog = page.getByRole("dialog", {
      name: "编辑个人技能",
    });
    await editDialog
      .getByLabel("技能描述")
      .fill("按项目拆分决策与风险。");
    await editDialog
      .getByRole("button", { name: "保存并启用新版本" })
      .click();
    await expect(
      page.getByText(/已保存「会议纪要」的新版本并启用/),
    ).toBeVisible();
    await expect(card.getByText("v2", { exact: true })).toBeVisible();

    await card
      .getByRole("button", { name: "停用个人技能：会议纪要" })
      .click();
    await expect(
      page.getByText(
        "已停用「会议纪要」，后续对话不会再把该技能列入可用目录。",
      ),
    ).toBeVisible();
    await expect(card.getByText("已停用", { exact: true })).toBeVisible();

    await card
      .getByRole("button", { name: "启用个人技能：会议纪要" })
      .click();
    await expect(
      page.getByText(
        "已启用「会议纪要」；后续对话可按任务使用，每次是否执行仍由 Agent 判断。",
      ),
    ).toBeVisible();
    await expect(card.getByText("已启用", { exact: true })).toBeVisible();

    await card
      .getByRole("button", { name: "删除个人技能：会议纪要" })
      .click();
    const deleteDialog = page.getByRole("dialog", {
      name: "删除「会议纪要」？",
    });
    await expect(deleteDialog).toContainText(
      "后续对话将不再使用该技能，进行中的任务和历史记录不受影响",
    );
    await deleteDialog
      .getByRole("button", { name: "确认删除" })
      .click();
    await expect(
      page.getByRole("heading", { name: "还没有个人技能" }),
    ).toBeVisible();
    await expectNoHorizontalOverflow(page);
  });

  test("窄屏导入、保存与对话挂载技能 Popover，并检查 390px 无水平溢出", async ({
    page,
  }) => {
    await installPersonalSkillsFixtures(page);
    await page.setViewportSize({ height: 844, width: 390 });

    await page.goto("/skills?view=mine");
    await expect(
      page.getByRole("heading", { level: 1, name: "技能库" }),
    ).toBeVisible();
    await expectNoHorizontalOverflow(page);

    await page
      .getByRole("button", { name: "新建第一个个人技能" })
      .click();
    const dialog = page.getByRole("dialog", {
      name: "新建个人技能",
    });
    const dialogBox = await dialog.boundingBox();
    expect(dialogBox).not.toBeNull();
    expect(dialogBox!.x).toBeGreaterThanOrEqual(0);
    expect(dialogBox!.x + dialogBox!.width).toBeLessThanOrEqual(390);

    await dialog.getByRole("button", { name: "导入 Markdown" }).click();
    const fileInput = dialog.getByLabel("选择单个 Markdown 文件");
    await expect(fileInput).toHaveAttribute("accept", ".md");
    await expect(fileInput).not.toHaveAttribute("multiple", "");
    await fileInput.setInputFiles({
      buffer: Buffer.from(
        "---\nname: 访谈整理\ndescription: 按证据梳理访谈\n---\n# 访谈整理",
      ),
      mimeType: "text/markdown",
      name: "interview.MD",
    });
    await expect(dialog.getByLabel("技能名称")).toHaveValue("访谈整理");
    await expect(
      dialog.getByText(/文件已填入下方表单，尚未发布/),
    ).toBeVisible();
    await expectNoHorizontalOverflow(page);

    await dialog
      .getByRole("button", { name: "创建并启用" })
      .click();
    const personalCard = page
      .locator(".personal-skill-card")
      .filter({ hasText: "访谈整理" });
    await expect(personalCard).toBeVisible();
    await expectNoHorizontalOverflow(page);

    await page.goto("/chat");
    await expect(
      page.getByRole("heading", { level: 1, name: "你好，个人技能验收用户" }),
    ).toBeVisible();

    const trigger = page.getByRole("button", { name: /本次技能/ });
    await trigger.click();

    const skillPopover = page.getByRole("dialog", {
      name: "选择本次技能",
    });
    await expect(skillPopover).toBeVisible();
    const popoverBox = await skillPopover.boundingBox();
    expect(popoverBox).not.toBeNull();
    expect(popoverBox!.x).toBeGreaterThanOrEqual(0);
    expect(popoverBox!.x + popoverBox!.width).toBeLessThanOrEqual(390);

    // 验证按三层可见性分组并正确展示全公司 / 个人范围标签与执行模式。
    const companySection = skillPopover.getByRole("region", {
      name: "全公司技能",
    });
    const personalSection = skillPopover.getByRole("region", {
      name: "我的技能",
    });
    await expect(companySection.getByText("全公司审批技能")).toBeVisible();
    await expect(companySection.getByText("全公司", { exact: true })).toBeVisible();
    await expect(companySection.getByText("脚本执行需审批")).toBeVisible();
    await expect(personalSection.getByText("访谈整理")).toBeVisible();
    await expect(personalSection.getByText("个人", { exact: true })).toBeVisible();
    await expect(personalSection.getByText("纯文档", { exact: true })).toBeVisible();
    await expectNoHorizontalOverflow(page);

    // 勾选个人技能后关闭浮层，验证输入区已回显已挂载技能。
    await skillPopover.getByRole("checkbox", { name: "访谈整理" }).click();
    await skillPopover.getByRole("button", { name: "完成" }).click();
    await expect(skillPopover).toBeHidden();
    await expect(trigger).toContainText("访谈整理");

    // 再次打开后使用 Escape 关闭，验证焦点稳定归还触发按钮。
    await trigger.click();
    await expect(skillPopover).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(skillPopover).toBeHidden();
    await expect(trigger).toBeFocused();
  });
});
