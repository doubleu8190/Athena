import { expect, test, type Page, type Route } from "@playwright/test";

const NOW = "2026-11-19T08:00:00Z";

const USER = {
  created_at: "2026-11-01T00:00:00Z",
  display_name: "个人技能验收用户",
  email: "personal-skills@example.com",
  id: "user-personal-skills",
  role: "user",
};

interface SkillVersionFixture {
  created_at: string;
  description: string;
  dependency_check: null;
  dependencies: string[];
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
  dependency_check: null;
  description: string;
  enabled: boolean;
  id: string;
  name: string;
  revision: number;
  scope: "public" | "personal";
  script_requires_approval: boolean;
  slug: string;
  updated_at: string;
  versions?: SkillVersionFixture[];
}

interface PersonalSkillFixtureState {
  personal: SkillFixture | null;
  publicSkill: SkillFixture;
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
    dependency_check: null,
    dependencies: [],
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
    personal: null,
    publicSkill: {
      current_version_id: "public-version-1",
      current_version_label: "v1",
      dependency_check: null,
      description: "需要审批脚本的公共技能。",
      enabled: true,
      id: "public-skill-1",
      name: "公共审批技能",
      revision: 1,
      scope: "public",
      script_requires_approval: true,
      slug: "public-approval-skill",
      updated_at: NOW,
    },
  };

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === "/api/v1/auth/me") {
      return json(route, USER);
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
        dependency_check: null,
        description: input.description,
        enabled: true,
        id: identity.id,
        name: input.name,
        revision: 1,
        scope: "personal",
        script_requires_approval: false,
        slug: identity.slug,
        updated_at: NOW,
        versions: [makeVersion(identity, input)],
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
          state.publicSkill,
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
  test("桌面主流程：导航、创建、编辑、启停与删除", async ({ page }) => {
    await installPersonalSkillsFixtures(page);
    await page.setViewportSize({ height: 900, width: 1280 });

    await page.goto("/chat");
    await expect(
      page.getByRole("heading", { level: 1, name: "对话工作台" }),
    ).toBeVisible();
    await page.locator(".sidebar-skill-entry").click();
    await expect(page).toHaveURL(/\/skills$/);
    await expect(
      page.getByRole("heading", { level: 1, name: "我的技能" }),
    ).toBeVisible();

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

    await expect(page.getByText(/已创建并启用「会议纪要」/)).toBeVisible();
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
      page.getByText("已停用「会议纪要」，新任务将不再使用该技能。"),
    ).toBeVisible();
    await expect(card.getByText("已停用", { exact: true })).toBeVisible();

    await card
      .getByRole("button", { name: "启用个人技能：会议纪要" })
      .click();
    await expect(page.getByText("已启用「会议纪要」。")).toBeVisible();
    await expect(card.getByText("已启用", { exact: true })).toBeVisible();

    await card
      .getByRole("button", { name: "删除个人技能：会议纪要" })
      .click();
    const deleteDialog = page.getByRole("dialog", {
      name: "删除「会议纪要」？",
    });
    await expect(deleteDialog).toContainText(
      "新任务将无法使用该技能，进行中的任务和历史记录不受影响",
    );
    await deleteDialog
      .getByRole("button", { name: "确认删除" })
      .click();
    await expect(
      page.getByRole("heading", { name: "还没有个人技能" }),
    ).toBeVisible();
    await expectNoHorizontalOverflow(page);
  });

  test("窄屏导入、保存与聊天来源标签，并检查 390px 无水平溢出", async ({
    page,
  }) => {
    await installPersonalSkillsFixtures(page);
    await page.setViewportSize({ height: 844, width: 390 });

    await page.goto("/skills");
    await expect(
      page.getByRole("heading", { level: 1, name: "我的技能" }),
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
      page.getByRole("heading", { level: 1, name: "对话工作台" }),
    ).toBeVisible();
    const publicOption = page
      .locator(".skill-catalog-option.is-public")
      .filter({ hasText: "公共审批技能" });
    const personalOption = page
      .locator(".skill-catalog-option.is-personal")
      .filter({ hasText: "访谈整理" });
    await expect(publicOption.getByText("公共", { exact: true })).toBeVisible();
    await expect(
      publicOption.getByText("脚本执行需审批"),
    ).toBeVisible();
    await expect(
      personalOption.getByText("个人", { exact: true }),
    ).toBeVisible();
    await expect(
      personalOption.getByText("纯文档", { exact: true }),
    ).toBeVisible();
    await expect(personalOption).not.toContainText("脚本执行");
    await expectNoHorizontalOverflow(page);
  });
});
