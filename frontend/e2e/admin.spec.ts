import { expect, test } from "@playwright/test";

import { installAdminFixtures } from "./adminFixtures";

test.beforeEach(async ({ page }) => {
  await installAdminFixtures(page);
});

test("管理导航在单层侧栏的真实页面间切换", async ({ page }) => {
  await page.setViewportSize({ height: 900, width: 1280 });
  await page.goto("/admin/models");
  await expect(
    page.getByRole("heading", {
      level: 1,
      name: "模型与压缩策略",
    }),
  ).toBeVisible();

  // 二期单层侧栏进入 /admin/* 时自动展开「管理后台」折叠分组。
  const adminGroup = page.getByRole("group", { name: "管理后台" });
  await expect(adminGroup).toBeVisible();

  await adminGroup.getByRole("link", { name: "MCP 服务" }).click();
  await expect(
    page.getByRole("heading", { level: 1, name: "MCP Server" }),
  ).toBeVisible();

  await adminGroup.getByRole("link", { name: "工具审批策略" }).click();
  await expect(
    page.getByRole("heading", {
      level: 1,
      name: "工具审批策略",
    }),
  ).toBeVisible();
  await expect(page.getByText("后端返回 1 个工具")).toBeVisible();

  await adminGroup.getByRole("link", { name: "企业与部门技能" }).click();
  await expect(
    page.getByRole("heading", { level: 1, name: "公共技能" }),
  ).toBeVisible();
  await expect(page.getByText("还没有技能包")).toBeVisible();

  await adminGroup.getByRole("link", { name: "子智能体" }).click();
  await expect(
    page.getByRole("heading", { level: 1, name: "子智能体管理" }),
  ).toBeVisible();
  await expect(
    page.getByText("端到端子智能体与研究资料分析助手"),
  ).toBeVisible();
});

test("模型创建表单提交真实字段并更新列表", async ({ page }) => {
  await page.goto("/admin/models");
  await page.getByRole("button", { name: "创建模型版本" }).click();
  const dialog = page.getByRole("dialog", { name: "创建模型版本" });
  await dialog.getByLabel("版本名称").fill("端到端模型");
  await dialog.getByLabel("模型标识").fill("atlas-e2e");
  await dialog.getByLabel("HTTPS Base URL").fill("https://llm.example/v1");
  await dialog.getByLabel("API Key").fill("test-secret-key");

  const requestPromise = page.waitForRequest(
    (request) =>
      request.method() === "POST" &&
      new URL(request.url()).pathname ===
        "/api/v1/admin/llm/model-profiles",
  );
  await dialog
    .getByRole("button", { name: "创建不可变版本" })
    .click();
  const request = await requestPromise;
  expect(request.postDataJSON()).toMatchObject({
    api_key: "test-secret-key",
    base_url: "https://llm.example/v1",
    model: "atlas-e2e",
    name: "端到端模型",
  });
  await expect(dialog).toBeHidden();
  await expect(page.getByText("端到端模型")).toBeVisible();
});

test("MCP 表单可切换 STDIO 并提交绝对路径", async ({ page }) => {
  await page.goto("/admin/mcp");
  await page.getByRole("button", { name: "新建 MCP Server" }).click();
  const dialog = page.getByRole("dialog", {
    name: "新建 MCP Server",
  });
  await dialog.getByLabel("Server 名称").fill("本地工具桥");
  await dialog.getByRole("radio", { name: "STDIO" }).check();
  await expect(dialog.getByLabel("绝对命令路径")).toBeVisible();
  await dialog.getByLabel("绝对命令路径").fill("/usr/local/bin/mcp");
  await dialog
    .getByLabel("工作目录（可选，必须是绝对路径）")
    .fill("/srv/mcp");

  const requestPromise = page.waitForRequest(
    (request) =>
      request.method() === "POST" &&
      new URL(request.url()).pathname === "/api/v1/admin/mcp/servers",
  );
  await dialog
    .getByRole("button", { name: "创建并发现工具" })
    .click();
  expect((await requestPromise).postDataJSON()).toMatchObject({
    command: "/usr/local/bin/mcp",
    cwd: "/srv/mcp",
    name: "本地工具桥",
    transport: "stdio",
  });
  await expect(dialog).toBeHidden();
  await expect(page.getByText("本地工具桥")).toBeVisible();
});

test("工具审批开关提交单工具 PATCH", async ({ page }) => {
  await page.goto("/admin/tools");
  const toggle = page.getByRole("switch", {
    name: /mcp__eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee 需要审批/,
  });
  await expect(toggle).toHaveAttribute("aria-checked", "false");

  const requestPromise = page.waitForRequest(
    (request) =>
      request.method() === "PATCH" &&
      new URL(request.url()).pathname ===
        "/api/v1/admin/tools/tool-e2e",
  );
  await toggle.click();
  expect((await requestPromise).postDataJSON()).toEqual({
    requires_approval: true,
  });
  await expect(toggle).toHaveAttribute("aria-checked", "true");
});

test("920px 与 390px 管理页无水平溢出", async ({ page }) => {
  const pages = [
    {
      heading: "模型与压缩策略",
      path: "/admin/models",
    },
    { heading: "MCP Server", path: "/admin/mcp" },
    { heading: "工具审批策略", path: "/admin/tools" },
    { heading: "公共技能", path: "/admin/skills" },
    { heading: "子智能体管理", path: "/admin/subagents" },
  ];

  for (const target of pages) {
    await page.goto(target.path);
    await expect(
      page.getByRole("heading", {
        level: 1,
        name: target.heading,
      }),
    ).toBeVisible();
    for (const width of [920, 390]) {
      await page.setViewportSize({ height: 844, width });
      await expect
        .poll(async () =>
          page.evaluate(
            () =>
              document.documentElement.scrollWidth -
              window.innerWidth,
          ),
        )
        .toBeLessThanOrEqual(1);
    }
  }
});

test("短视口保留单层侧栏主导航与底部账户入口", async ({ page }) => {
  // 桌面断点为 >=1024px，在 1280×420 短视口下核验侧栏内部独立滚动且底部账户区始终可见。
  await page.setViewportSize({ height: 420, width: 1280 });
  await page.goto("/admin/subagents");

  const sidebar = page.getByRole("complementary", {
    name: "工作区侧栏",
  });
  await expect(sidebar).toBeVisible();
  const navigation = sidebar.getByRole("navigation", { name: "主导航" });
  const account = sidebar.getByRole("button", { name: "账户菜单" });
  await expect(navigation).toBeVisible();
  await expect(account).toBeVisible();

  const layout = await sidebar.evaluate((element) => {
    const nav = element.querySelector<HTMLElement>('nav[aria-label="主导航"]');
    const accountBtn = element.querySelector<HTMLElement>(
      'button[aria-label="账户菜单"]',
    );
    if (!nav || !accountBtn) {
      return null;
    }
    const sidebarRect = element.getBoundingClientRect();
    const navRect = nav.getBoundingClientRect();
    const accountRect = accountBtn.getBoundingClientRect();
    return {
      accountVisible: accountRect.bottom <= sidebarRect.bottom + 1,
      navigationBeforeAccount: navRect.bottom <= accountRect.top + 1,
    };
  });
  expect(layout).toEqual({
    accountVisible: true,
    navigationBeforeAccount: true,
  });

  await account.click();
  await expect(page.getByRole("menu")).toBeVisible();
});

test("390px 子智能体卡片与表单无水平溢出", async ({ page }) => {
  await page.setViewportSize({ height: 844, width: 390 });
  await page.goto("/admin/subagents");

  await expect(
    page.getByRole("button", {
      name: "编辑子智能体：端到端子智能体与研究资料分析助手",
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", {
      name: "删除子智能体：端到端子智能体与研究资料分析助手",
    }),
  ).toBeVisible();

  await page.getByRole("button", { name: "新建子智能体" }).click();
  const dialog = page.getByRole("dialog", { name: "新建子智能体" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByLabel("绑定模型")).toBeVisible();
  await expect(
    dialog.getByRole("checkbox", {
      name: /知识检索.*查询知识资料/,
    }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("checkbox", {
      name: "创建后立即启用该子智能体",
    }),
  ).toBeVisible();

  await expect
    .poll(async () =>
      page.evaluate(
        () =>
          document.documentElement.scrollWidth - window.innerWidth,
      ),
    )
    .toBeLessThanOrEqual(1);
});

test("管理端详情列表左对齐、模型表单报错双列对齐与子智能体弹窗规格符合预期", async ({
  page,
}) => {
  await page.setViewportSize({ height: 844, width: 1280 });
  await page.goto("/admin/models");

  // 验证模型创建弹窗触发必填校验错误时，「版本名称」与「模型标识」输入框仍保持同行水平对齐
  await page.getByRole("button", { name: "创建模型版本" }).click();
  const modelDialog = page.getByRole("dialog", { name: "创建模型版本" });
  await modelDialog.getByRole("button", { name: "创建不可变版本" }).click();
  await expect(modelDialog.locator(".field-error").first()).toBeVisible();
  const nameBox = await modelDialog.getByLabel("版本名称").boundingBox();
  const modelBox = await modelDialog.getByLabel("模型标识").boundingBox();
  expect(nameBox).not.toBeNull();
  expect(modelBox).not.toBeNull();
  expect(Math.abs(nameBox!.y - modelBox!.y)).toBeLessThanOrEqual(2);

  await page.goto("/admin/subagents");

  // 验证管理卡片详情列表内 dd 与 dt 左边缘对齐（消除浏览器默认 40px 缩进）
  const detailCell = page.locator("dl > div").first();
  await expect(detailCell).toBeVisible();
  const detailOffset = await detailCell.evaluate((cell) => {
    const dt = cell.querySelector("dt");
    const dd = cell.querySelector("dd");
    if (!dt || !dd) {
      return null;
    }
    return Math.abs(
      dd.getBoundingClientRect().left - dt.getBoundingClientRect().left,
    );
  });
  expect(detailOffset).not.toBeNull();
  expect(detailOffset!).toBeLessThanOrEqual(1);

  // 验证子智能体新建弹窗桌面端宽度符合宽弹窗规格（> 600px），且字段 label 字重 >= 600
  await page.getByRole("button", { name: "新建子智能体" }).click();
  const subagentDialog = page.getByRole("dialog", { name: "新建子智能体" });
  await expect(subagentDialog).toBeVisible();
  const dialogBox = await subagentDialog.boundingBox();
  expect(dialogBox).not.toBeNull();
  expect(dialogBox!.width).toBeGreaterThan(600);

  const labelFontWeight = await subagentDialog
    .locator("form label[for]")
    .first()
    .evaluate((el) =>
      Number.parseInt(window.getComputedStyle(el).fontWeight, 10),
    );
  expect(labelFontWeight).toBeGreaterThanOrEqual(600);
});
