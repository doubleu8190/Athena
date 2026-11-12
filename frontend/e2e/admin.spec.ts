import { expect, test } from "@playwright/test";

import { installAdminFixtures } from "./adminFixtures";

test.beforeEach(async ({ page }) => {
  await installAdminFixtures(page);
});

test("管理导航在四个真实页面间切换", async ({ page }) => {
  await page.goto("/admin/models");
  await expect(
    page.getByRole("heading", {
      level: 1,
      name: "模型与压缩策略",
    }),
  ).toBeVisible();

  const nav = page.getByRole("navigation", { name: "管理子导航" });
  await nav.getByRole("link", { name: "MCP Server" }).click();
  await expect(
    page.getByRole("heading", { level: 1, name: "MCP Server" }),
  ).toBeVisible();

  await nav.getByRole("link", { name: "工具审批策略" }).click();
  await expect(
    page.getByRole("heading", {
      level: 1,
      name: "工具审批策略",
    }),
  ).toBeVisible();
  await expect(page.getByText("后端返回 1 个工具")).toBeVisible();

  await nav.getByRole("link", { name: "技能管理" }).click();
  await expect(
    page.getByRole("heading", { level: 1, name: "技能管理" }),
  ).toBeVisible();
  await expect(page.getByText("还没有技能包")).toBeVisible();
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
    base_url: "https://llm.example/v1",
    api_key: "test-secret-key",
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
    { heading: "技能管理", path: "/admin/skills" },
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
