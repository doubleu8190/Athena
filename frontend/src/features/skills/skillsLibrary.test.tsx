import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  makeSkill,
  makeSkillDetails,
} from "../../test/adminFixtures";
import {
  getRequestUrl,
  listResponse,
  TEST_USER,
} from "../../test/fixtures";
import { jsonResponse, renderApp } from "../../test/testUtils";

const PUBLIC_SKILL = makeSkill({
  current_version_label: "v3",
  description: "按统一模板生成项目复盘。",
  id: "public-review",
  name: "项目复盘",
  scope: "public",
  script_requires_approval: true,
  slug: "project-review",
});

const PERSONAL_SKILL = makeSkill({
  current_version_label: "v2",
  description: "只对本人可见的会议纪要方法。",
  enabled: true,
  id: "personal-notes",
  name: "会议纪要",
  scope: "personal",
  script_requires_approval: false,
  slug: "meeting-notes",
});

describe("技能库一级页面", () => {
  it("默认展示可用技能，并可切换到保留个人 CRUD 的我的技能", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/skills") {
          return jsonResponse({
            items: [PUBLIC_SKILL, PERSONAL_SKILL],
          });
        }
        if (url.pathname === "/api/v1/me/skills") {
          return jsonResponse({ items: [PERSONAL_SKILL] });
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );
    const user = userEvent.setup();
    const app = renderApp("/skills");

    expect(
      await screen.findByRole("heading", { level: 1, name: "技能库" }),
    ).toBeInTheDocument();
    const mainNav = screen.getByRole("navigation", { name: "主导航" });
    expect(
      within(mainNav).getByRole("link", { name: "技能库" }),
    ).toHaveAttribute("aria-current", "page");
    expect(
      screen.getAllByText(/公共技能由管理员发布/).length,
    ).toBeGreaterThan(0);
    expect(screen.getByText(/个人技能仅本人可见/)).toBeInTheDocument();

    const viewNav = screen.getByRole("navigation", {
      name: "技能库视图",
    });
    expect(
      within(viewNav).getByRole("link", { name: "可用技能" }),
    ).toHaveAttribute("aria-current", "page");
    expect(
      within(viewNav).getByRole("link", { name: "我的技能" }),
    ).not.toHaveAttribute("aria-current");
    expect(screen.queryByText("返回对话")).not.toBeInTheDocument();

    const publicCard = (
      await screen.findByRole("heading", { name: "项目复盘" })
    ).closest("li");
    const personalCardInAvailable = (
      await screen.findByRole("heading", { name: "会议纪要" })
    ).closest("li");
    expect(publicCard).not.toBeNull();
    expect(personalCardInAvailable).not.toBeNull();
    expect(
      within(publicCard as HTMLElement).getAllByText("全公司"),
    ).toHaveLength(2);
    expect(
      within(publicCard as HTMLElement).getByText("脚本执行需审批"),
    ).toBeVisible();
    expect(
      within(publicCard as HTMLElement).getByText("v3"),
    ).toBeVisible();
    expect(
      within(personalCardInAvailable as HTMLElement).getAllByText("个人"),
    ).toHaveLength(2);
    expect(
      within(personalCardInAvailable as HTMLElement).getByText("纯文档"),
    ).toBeVisible();
    expect(
      within(publicCard as HTMLElement).queryByRole("button"),
    ).not.toBeInTheDocument();

    const mineLink = within(viewNav).getByRole("link", {
      name: "我的技能",
    });
    mineLink.focus();
    await user.keyboard("{Enter}");

    expect(
      await screen.findByRole("button", {
        name: "编辑个人技能：会议纪要",
      }),
    ).toBeInTheDocument();
    expect(app.getLocation()).toEqual({
      pathname: "/skills",
      search: "?view=mine",
    });
    expect(
      within(viewNav).getByRole("link", { name: "我的技能" }),
    ).toHaveAttribute("aria-current", "page");
    expect(
      within(viewNav).getByRole("link", { name: "可用技能" }),
    ).not.toHaveAttribute("aria-current");
    expect(mineLink).toHaveFocus();
    expect(
      screen.getByRole("heading", { level: 1, name: "技能库" }),
    ).toBeInTheDocument();

    app.goBack();
    const availableLink = within(
      screen.getByRole("navigation", { name: "技能库视图" }),
    ).getByRole("link", { name: "可用技能" });
    await waitFor(() => expect(availableLink).toHaveFocus());
    expect(app.getLocation()).toEqual({
      pathname: "/skills",
      search: "",
    });
  });

  it("聊天侧栏不再提供重复的技能入口", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/projects") {
          return jsonResponse(listResponse([], 1, 100, 0));
        }
        if (url.pathname === "/api/v1/sessions") {
          return jsonResponse(listResponse([], 1, 8, 0));
        }
        if (url.pathname === "/api/v1/skills") {
          return jsonResponse({ items: [] });
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    renderApp("/chat");

    expect(
      await screen.findByRole("link", { name: "新建对话" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "我的技能" }),
    ).not.toBeInTheDocument();
    expect(document.querySelector(".sidebar-skill-entry")).toBeNull();
  });

  it("可用技能为空时同时说明公共与个人技能来源", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/skills") {
          return jsonResponse({ items: [] });
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );

    renderApp("/skills");

    expect(
      await screen.findByRole("heading", {
        name: "当前没有可用技能",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/管理员发布公共技能，或你在“我的技能”中启用个人技能后/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "创建个人技能" }),
    ).toBeInTheDocument();
  });

  it("我的技能成功提示不会串到可用技能视图", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/me/skills" && method === "GET") {
          return jsonResponse({ items: [PERSONAL_SKILL] });
        }
        if (
          url.pathname === "/api/v1/me/skills/personal-notes" &&
          method === "PATCH"
        ) {
          return jsonResponse({
            ...PERSONAL_SKILL,
            enabled: false,
            revision: 2,
          });
        }
        if (url.pathname === "/api/v1/skills" && method === "GET") {
          return jsonResponse({ items: [] });
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );
    const user = userEvent.setup();
    const app = renderApp("/skills?view=mine");

    await user.click(
      await screen.findByRole("button", {
        name: "停用个人技能：会议纪要",
      }),
    );
    expect(
      await screen.findByText(
        "已停用「会议纪要」，后续对话不会再把该技能列入可用目录。",
      ),
    ).toBeInTheDocument();

    await user.click(
      within(
        screen.getByRole("navigation", { name: "技能库视图" }),
      ).getByRole("link", { name: "可用技能" }),
    );
    expect(
      await screen.findByRole("heading", { name: "当前没有可用技能" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/已停用「会议纪要」/)).not.toBeInTheDocument();
    expect(app.getLocation()).toEqual({
      pathname: "/skills",
      search: "",
    });
  });

  it("切换视图会卸载旧的编辑和删除弹窗", async () => {
    const details = makeSkillDetails({
      ...PERSONAL_SKILL,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/me/skills" && method === "GET") {
          return jsonResponse({ items: [PERSONAL_SKILL] });
        }
        if (
          url.pathname === "/api/v1/me/skills/personal-notes" &&
          method === "GET"
        ) {
          return jsonResponse(details);
        }
        if (url.pathname === "/api/v1/skills" && method === "GET") {
          return jsonResponse({ items: [] });
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );
    const user = userEvent.setup();
    const app = renderApp("/skills?view=mine");

    await user.click(
      await screen.findByRole("button", {
        name: "编辑个人技能：会议纪要",
      }),
    );
    expect(
      await screen.findByRole("dialog", { name: "编辑个人技能" }),
    ).toBeInTheDocument();

    app.navigateTo("/skills");
    expect(
      await screen.findByRole("heading", { name: "当前没有可用技能" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("dialog", { name: "编辑个人技能" }),
    ).not.toBeInTheDocument();

    app.navigateTo("/skills?view=mine");
    await screen.findByRole("button", {
      name: "删除个人技能：会议纪要",
    });
    await user.click(
      screen.getByRole("button", { name: "删除个人技能：会议纪要" }),
    );
    expect(
      await screen.findByRole("dialog", { name: "删除「会议纪要」？" }),
    ).toBeInTheDocument();
    app.navigateTo("/skills");
    expect(
      await screen.findByRole("heading", { name: "当前没有可用技能" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("dialog", { name: "删除「会议纪要」？" }),
    ).not.toBeInTheDocument();
  });

  it("迟到的 mine mutation 不会把成功提示写进新视图", async () => {
    let resolvePatch: ((response: Response) => void) | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(getRequestUrl(input), "http://localhost");
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (url.pathname === "/api/v1/me/skills" && method === "GET") {
          return jsonResponse({ items: [PERSONAL_SKILL] });
        }
        if (
          url.pathname === "/api/v1/me/skills/personal-notes" &&
          method === "PATCH"
        ) {
          return new Promise<Response>((resolve) => {
            resolvePatch = resolve;
          });
        }
        if (url.pathname === "/api/v1/skills" && method === "GET") {
          return jsonResponse({ items: [] });
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );
    const user = userEvent.setup();
    const app = renderApp("/skills?view=mine");

    await user.click(
      await screen.findByRole("button", {
        name: "停用个人技能：会议纪要",
      }),
    );
    await waitFor(() => expect(resolvePatch).toBeTypeOf("function"));
    app.navigateTo("/skills");
    await screen.findByRole("heading", { name: "当前没有可用技能" });

    resolvePatch?.(
      jsonResponse({
        ...PERSONAL_SKILL,
        enabled: false,
        revision: 2,
      }),
    );
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    expect(screen.queryByText(/已停用「会议纪要」/)).not.toBeInTheDocument();
  });
});
