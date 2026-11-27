import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  makeDependencyCheck,
  makeSkillDetails,
  makeSkillVersion,
  TEST_ADMIN,
} from "../../test/adminFixtures";
import { getRequestUrl, makeDeptAdmin } from "../../test/fixtures";
import { jsonResponse, renderApp } from "../../test/testUtils";
import { adminKeys } from "../admin/queryKeys";
import type { SkillAdminDetails, SkillVersion } from "./model";

function requestUrl(input: RequestInfo | URL): URL {
  return new URL(getRequestUrl(input), "http://localhost");
}

function versionListResponse(
  details: SkillAdminDetails,
  items: SkillVersion[] = details.versions,
  page = 1,
  total = items.length,
) {
  return {
    items,
    page,
    page_size: 20,
    total,
  };
}

describe("管理员技能管理", () => {
  it("首发导入使用 package 字段，导入后展示指南、文件与依赖且保持未启用", async () => {
    const imported = makeSkillDetails({
      dependency_check: makeDependencyCheck(),
      description: "从 ZIP 导入的真实技能。",
      enabled: false,
      id: "skill-imported",
      name: "导入技能",
      revision: 1,
      slug: "imported-skill",
    });
    let importBody: unknown = null;
    let importIdempotencyKey: string | null = null;
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (url.pathname === "/api/v1/admin/skills" && method === "GET") {
          return jsonResponse({ items: [] });
        }
        if (
          url.pathname === "/api/v1/admin/skills/import" &&
          method === "POST"
        ) {
          importBody = init?.body;
          importIdempotencyKey = new Headers(init?.headers).get(
            "Idempotency-Key",
          );
          return jsonResponse(imported, 201);
        }
        if (
          url.pathname ===
            `/api/v1/admin/skills/${imported.id}` &&
          method === "GET"
        ) {
          return jsonResponse(imported);
        }
        if (
          url.pathname ===
            `/api/v1/admin/skills/${imported.id}/versions` &&
          method === "GET"
        ) {
          return jsonResponse(versionListResponse(imported));
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/admin/skills");

    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "公共技能",
      }),
    ).toBeInTheDocument();
    await user.click(
      await screen.findByRole("button", { name: "导入第一个技能" }),
    );
    await waitFor(() =>
      expect(screen.getByLabelText("技能 ZIP")).toHaveFocus(),
    );
    const file = new File(["zip-content"], "sales-report.zip", {
      type: "application/zip",
    });
    await user.upload(screen.getByLabelText("技能 ZIP"), file);
    await user.click(
      screen.getByRole("button", { name: "导入并查看检查结果" }),
    );

    expect(
      await screen.findByRole("dialog", { name: "技能：导入技能" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "月度销售报表" }),
    ).toBeInTheDocument();
    expect(screen.getAllByText("scripts/build_report.py").length).toBeGreaterThan(0);
    expect(screen.getAllByText("依赖满足").length).toBeGreaterThan(0);
    expect(
      screen.getByRole("checkbox", {
        name: "向普通用户启用该技能",
      }),
    ).not.toBeChecked();
    expect(importBody).toBeInstanceOf(FormData);
    if (!(importBody instanceof FormData)) {
      throw new Error("导入请求没有使用 multipart FormData。");
    }
    expect(importBody.get("package")).toBe(file);
    expect(importIdempotencyKey).toMatch(/^[A-Za-z0-9._~-]+$/);
  });

  it("首发导入 422 显示违规 ZIP 成员路径且按纯文本渲染", async () => {
    const memberPath = "templates/中文\\x01<b>指南</b>.txt";
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = requestUrl(input);
          const method = init?.method ?? "GET";
          if (url.pathname.endsWith("/auth/me")) {
            return jsonResponse(TEST_ADMIN);
          }
          if (url.pathname === "/api/v1/admin/skills" && method === "GET") {
            return jsonResponse({ items: [] });
          }
          if (
            url.pathname === "/api/v1/admin/skills/import" &&
            method === "POST"
          ) {
            return jsonResponse(
              {
                detail: {
                  code: "skill_zip_path_invalid",
                  member_path: memberPath,
                },
              },
              422,
            );
          }
          throw new Error(`Unexpected request: ${method} ${url}`);
        },
      ),
    );
    const user = userEvent.setup();

    renderApp("/admin/skills");

    await user.click(
      await screen.findByRole("button", { name: "导入第一个技能" }),
    );
    await user.upload(
      screen.getByLabelText("技能 ZIP"),
      new File(["invalid"], "invalid-path.zip", {
        type: "application/zip",
      }),
    );
    await user.click(
      screen.getByRole("button", { name: "导入并查看检查结果" }),
    );

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "技能包包含不受支持的文件位置或目录规则",
    );
    expect(alert.textContent).toContain(`包内文件：${memberPath}`);
    expect(alert.querySelector("b")).toBeNull();
    expect(alert.querySelector("a")).toBeNull();
  });

  it("保存版本、启停和审批配置时发送 expected_revision 并刷新公开目录", async () => {
    const initial = makeSkillDetails();
    const updated = {
      ...initial,
      enabled: true,
      revision: 2,
      script_requires_approval: true,
    };
    let patchBody: Record<string, unknown> | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = requestUrl(input);
          const method = init?.method ?? "GET";
          if (url.pathname.endsWith("/auth/me")) {
            return jsonResponse(TEST_ADMIN);
          }
          if (url.pathname === "/api/v1/admin/skills" && method === "GET") {
            return jsonResponse({ items: [initial] });
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
            url.pathname ===
              `/api/v1/admin/skills/${initial.id}/versions` &&
            method === "GET"
          ) {
            return jsonResponse(
              versionListResponse(patchBody ? updated : initial),
            );
          }
          if (
            url.pathname === `/api/v1/admin/skills/${initial.id}` &&
            method === "PATCH"
          ) {
            patchBody = JSON.parse(String(init?.body)) as Record<
              string,
              unknown
            >;
            return jsonResponse(updated);
          }
          throw new Error(`Unexpected request: ${method} ${url}`);
        },
      ),
    );
    const user = userEvent.setup();

    renderApp("/admin/skills");

    await user.click(
      await screen.findByRole("button", {
        name: "查看版本与指南",
      }),
    );
    await screen.findByRole("dialog", { name: "技能：销售报表" });
    await user.click(
      screen.getByRole("checkbox", {
        name: "向普通用户启用该技能",
      }),
    );
    await user.click(
      screen.getByRole("checkbox", {
        name: "每次脚本执行均需用户审批",
      }),
    );
    await user.click(
      screen.getByRole("button", { name: "保存当前配置" }),
    );

    await waitFor(() => expect(patchBody).not.toBeNull());
    expect(patchBody).toEqual({
      current_version_id: "skill-version-1",
      enabled: true,
      expected_revision: 1,
      script_requires_approval: true,
    });
    expect(
      await screen.findByText("技能配置已保存；新 Run 才会使用本次当前版本与审批策略。"),
    ).toBeInTheDocument();
  });

  it("后台修订刷新不覆盖编辑草稿，显式重载后才更新表单", async () => {
    const initial = makeSkillDetails({
      id: "skill-background-refresh",
    });
    const latest = {
      ...initial,
      enabled: true,
      revision: 2,
      script_requires_approval: false,
    };
    let serverDetails = initial;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = requestUrl(input);
          const method = init?.method ?? "GET";
          if (url.pathname.endsWith("/auth/me")) {
            return jsonResponse(TEST_ADMIN);
          }
          if (url.pathname === "/api/v1/admin/skills" && method === "GET") {
            return jsonResponse({ items: [initial] });
          }
          if (
            url.pathname ===
              `/api/v1/admin/skills/${initial.id}` &&
            method === "GET"
          ) {
            return jsonResponse(serverDetails);
          }
          if (
            url.pathname ===
              `/api/v1/admin/skills/${initial.id}/versions` &&
            method === "GET"
          ) {
            return jsonResponse(versionListResponse(serverDetails));
          }
          throw new Error(`Unexpected request: ${method} ${url}`);
        },
      ),
    );
    const user = userEvent.setup();
    const view = renderApp("/admin/skills");

    await user.click(
      await screen.findByRole("button", {
        name: "查看版本与指南",
      }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "技能：销售报表",
    });
    const approvalToggle = within(dialog).getByRole("checkbox", {
      name: "每次脚本执行均需用户审批",
    });
    await user.click(approvalToggle);
    expect(approvalToggle).toBeChecked();

    serverDetails = latest;
    await view.client.refetchQueries({
      exact: true,
      queryKey: adminKeys.skill(initial.id),
    });

    expect(
      await within(dialog).findByText(/服务端配置已更新到 revision 2/),
    ).toBeInTheDocument();
    expect(approvalToggle).toBeChecked();
    expect(
      within(dialog).getByRole("checkbox", {
        name: "向普通用户启用该技能",
      }),
    ).not.toBeChecked();
    expect(
      within(dialog).getByRole("button", { name: "保存当前配置" }),
    ).toBeDisabled();

    await user.click(
      within(dialog).getByRole("button", {
        name: "重新载入服务端最新配置",
      }),
    );

    await waitFor(() => {
      expect(
        within(dialog).getByRole("checkbox", {
          name: "向普通用户启用该技能",
        }),
      ).toBeChecked();
    });
    expect(approvalToggle).not.toBeChecked();
    expect(
      within(dialog).queryByText(/服务端配置已更新到 revision 2/),
    ).not.toBeInTheDocument();
    expect(
      await within(dialog).findByText("已重新载入服务端最新配置。"),
    ).toBeInTheDocument();
  });

  it("当前版本不在详情窗口或分页列表时单独读取并保持可选可预览", async () => {
    const currentVersion = makeSkillVersion({
      guide: "# 当前旧版指南\n\n这是列表外的当前版本。",
      id: "skill-version-current-old",
      label: "当前旧版",
      ordinal: 1,
      skill_id: "skill-current-outside-list",
    });
    const newestVersion = makeSkillVersion({
      guide: "# 最新版本指南",
      id: "skill-version-newest",
      label: "最新版本",
      ordinal: 2,
      skill_id: currentVersion.skill_id,
    });
    const initial = {
      ...makeSkillDetails({
        current_version_id: currentVersion.id,
        current_version_label: currentVersion.label,
        id: currentVersion.skill_id,
      }),
      versions: [],
    };
    let currentVersionCalls = 0;
    let pageCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = requestUrl(input);
          const method = init?.method ?? "GET";
          if (url.pathname.endsWith("/auth/me")) {
            return jsonResponse(TEST_ADMIN);
          }
          if (url.pathname === "/api/v1/admin/skills" && method === "GET") {
            return jsonResponse({ items: [initial] });
          }
          if (
            url.pathname ===
              `/api/v1/admin/skills/${initial.id}` &&
            method === "GET"
          ) {
            return jsonResponse(initial);
          }
          if (
            url.pathname ===
              `/api/v1/admin/skills/${initial.id}/versions` &&
            method === "GET"
          ) {
            pageCalls += 1;
            expect(url.searchParams.get("page")).toBe("1");
            expect(url.searchParams.get("page_size")).toBe("20");
            return jsonResponse({
              items: [newestVersion],
              page: 1,
              page_size: 20,
              total: 2,
            });
          }
          if (
            url.pathname ===
              `/api/v1/admin/skills/${initial.id}/versions/${currentVersion.id}` &&
            method === "GET"
          ) {
            currentVersionCalls += 1;
            return jsonResponse(currentVersion);
          }
          throw new Error(`Unexpected request: ${method} ${url}`);
        },
      ),
    );
    const user = userEvent.setup();

    renderApp("/admin/skills");

    await user.click(
      await screen.findByRole("button", {
        name: "查看版本与指南",
      }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "技能：销售报表",
    });

    expect(
      await within(dialog).findByRole("heading", {
        name: "当前旧版指南",
      }),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("combobox", { name: "当前版本" }),
    ).toHaveTextContent(/当前旧版/);
    expect(
      within(dialog).getByRole("button", { name: /最新版本/ }),
    ).toBeInTheDocument();
    expect(currentVersionCalls).toBe(1);
    expect(pageCalls).toBe(1);
  });

  it("当前版本下拉展开时 Escape 先收起菜单，再关闭技能详情弹窗", async () => {
    const initial = makeSkillDetails();
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = requestUrl(input);
          const method = init?.method ?? "GET";
          if (url.pathname.endsWith("/auth/me")) {
            return jsonResponse(TEST_ADMIN);
          }
          if (url.pathname === "/api/v1/admin/skills" && method === "GET") {
            return jsonResponse({ items: [initial] });
          }
          if (
            url.pathname === `/api/v1/admin/skills/${initial.id}` &&
            method === "GET"
          ) {
            return jsonResponse(initial);
          }
          if (
            url.pathname ===
              `/api/v1/admin/skills/${initial.id}/versions` &&
            method === "GET"
          ) {
            return jsonResponse(versionListResponse(initial));
          }
          throw new Error(`Unexpected request: ${method} ${url}`);
        },
      ),
    );
    const user = userEvent.setup();
    renderApp("/admin/skills");

    const openDetails = await screen.findByRole("button", {
      name: "查看版本与指南",
    });
    await user.click(openDetails);
    const dialog = await screen.findByRole("dialog", {
      name: "技能：销售报表",
    });
    const combobox = within(dialog).getByRole("combobox", {
      name: "当前版本",
    });

    await user.click(combobox);
    expect(combobox).toHaveAttribute("aria-expanded", "true");
    // Radix SelectContent 通过 Portal 挂载到 document.body，需在全局 screen 中检索 listbox。
    const listbox = screen.getByRole("listbox", { name: "当前版本列表" });
    expect(listbox).toBeInTheDocument();
    expect(listbox).toHaveAttribute("aria-label", "当前版本列表");

    await user.keyboard("{Escape}");
    expect(combobox).toHaveAttribute("aria-expanded", "false");
    expect(
      screen.queryByRole("listbox", {
        name: "当前版本列表",
      }),
    ).not.toBeInTheDocument();
    expect(dialog).toBeInTheDocument();

    await user.keyboard("{Escape}");
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "技能：销售报表" }),
      ).not.toBeInTheDocument(),
    );
    expect(openDetails).toHaveFocus();
  });

  it("版本历史可翻页加载并选择列表外的旧版本", async () => {
    const newestVersion = makeSkillVersion({
      guide: "# 新版指南",
      id: "skill-version-page-newest",
      label: "新版",
      ordinal: 21,
      skill_id: "skill-version-pagination",
    });
    const historicalVersion = makeSkillVersion({
      guide: "# 历史上使用的指南\n\n旧版正文仍可查看。",
      id: "skill-version-page-history",
      label: "历史旧版",
      ordinal: 1,
      skill_id: newestVersion.skill_id,
    });
    const initial = {
      ...makeSkillDetails({
        current_version_id: newestVersion.id,
        current_version_label: newestVersion.label,
        id: newestVersion.skill_id,
      }),
      versions: [],
    };
    const requestedPages: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = requestUrl(input);
          const method = init?.method ?? "GET";
          if (url.pathname.endsWith("/auth/me")) {
            return jsonResponse(TEST_ADMIN);
          }
          if (url.pathname === "/api/v1/admin/skills" && method === "GET") {
            return jsonResponse({ items: [initial] });
          }
          if (
            url.pathname ===
              `/api/v1/admin/skills/${initial.id}` &&
            method === "GET"
          ) {
            return jsonResponse(initial);
          }
          if (
            url.pathname ===
              `/api/v1/admin/skills/${initial.id}/versions` &&
            method === "GET"
          ) {
            const page = url.searchParams.get("page") ?? "1";
            requestedPages.push(page);
            return jsonResponse({
              items:
                page === "2"
                  ? [historicalVersion]
                  : [newestVersion],
              page: Number(page),
              page_size: 20,
              total: 21,
            });
          }
          throw new Error(`Unexpected request: ${method} ${url}`);
        },
      ),
    );
    const user = userEvent.setup();

    renderApp("/admin/skills");

    await user.click(
      await screen.findByRole("button", {
        name: "查看版本与指南",
      }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "技能：销售报表",
    });
    expect(
      await within(dialog).findByRole("heading", { name: "新版指南" }),
    ).toBeInTheDocument();
    expect(
      within(dialog).queryByRole("button", { name: /历史旧版/ }),
    ).not.toBeInTheDocument();

    await user.click(
      within(dialog).getByRole("button", { name: "下一页版本" }),
    );

    const historicalTab = await within(dialog).findByRole("button", {
      name: /历史旧版/,
    });
    expect(
      within(dialog).getByText(/第 2 \/ 2 页/),
    ).toBeInTheDocument();
    await user.click(historicalTab);

    expect(
      await within(dialog).findByRole("heading", {
        name: "历史上使用的指南",
      }),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("combobox", { name: "当前版本" }),
    ).toHaveTextContent(/历史旧版/);
    expect(requestedPages).toEqual(["1", "2"]);
  });

  it("版本冲突保留本地草稿，只有显式重载才采用服务端最新修订", async () => {
    const unsatisfied = makeDependencyCheck({
      items: [
        {
          detail_code: "not_found",
          installed_version: null,
          marker_applies: true,
          package: "pandas",
          requirement: "pandas>=2",
          satisfied: false,
        },
      ],
      status: "dependency_unsatisfied",
    });
    const initial = makeSkillDetails(
      {
        dependency_check: unsatisfied,
        id: "skill-conflict",
      },
      { dependency_check: unsatisfied },
    );
    const latestVersion = makeSkillVersion({
      id: "skill-version-2",
      label: "v2",
      ordinal: 2,
      skill_id: initial.id,
    });
    const latest = {
      ...initial,
      current_version_id: latestVersion.id,
      current_version_label: latestVersion.label,
      dependency_check: makeDependencyCheck(),
      enabled: true,
      name: "另一位管理员保存后的名称",
      revision: 2,
      versions: [latestVersion, ...initial.versions],
    };
    let detailCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = requestUrl(input);
          const method = init?.method ?? "GET";
          if (url.pathname.endsWith("/auth/me")) {
            return jsonResponse(TEST_ADMIN);
          }
          if (url.pathname === "/api/v1/admin/skills" && method === "GET") {
            return jsonResponse({ items: [initial] });
          }
          if (
            url.pathname === `/api/v1/admin/skills/${initial.id}` &&
            method === "GET"
          ) {
            detailCalls += 1;
            return jsonResponse(detailCalls === 1 ? initial : latest);
          }
          if (
            url.pathname ===
              `/api/v1/admin/skills/${initial.id}/versions` &&
            method === "GET"
          ) {
            return jsonResponse(versionListResponse(initial));
          }
          if (
            url.pathname === `/api/v1/admin/skills/${initial.id}` &&
            method === "PATCH"
          ) {
            return jsonResponse(
              { detail: "skill_revision_conflict" },
              409,
            );
          }
          throw new Error(`Unexpected request: ${method} ${url}`);
        },
      ),
    );
    const user = userEvent.setup();

    renderApp("/admin/skills");

    await user.click(
      await screen.findByRole("button", {
        name: "查看版本与指南",
      }),
    );
    let dialog = await screen.findByRole("dialog", {
      name: "技能：销售报表",
    });
    expect(
      within(dialog).getByRole("checkbox", {
        name: "向普通用户启用该技能",
      }),
    ).not.toBeChecked();
    await user.click(
      within(dialog).getByRole("checkbox", {
        name: "每次脚本执行均需用户审批",
      }),
    );
    await user.click(
      within(dialog).getByRole("button", { name: "保存当前配置" }),
    );

    expect(
      await within(dialog).findByText(/服务端配置已更新到 revision 2/),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("checkbox", {
        name: "每次脚本执行均需用户审批",
      }),
    ).toBeChecked();
    expect(
      within(dialog).getByRole("button", { name: "保存当前配置" }),
    ).toBeDisabled();

    await user.click(
      within(dialog).getByRole("button", {
        name: "重新载入服务端最新配置",
      }),
    );

    expect(
      await within(dialog).findByText("已重新载入服务端最新配置。"),
    ).toBeInTheDocument();
    dialog = screen.getByRole("dialog", {
      name: "技能：另一位管理员保存后的名称",
    });
    expect(within(dialog).getByText(/revision 2/)).toBeInTheDocument();
    expect(
      within(dialog).getByRole("combobox", { name: "当前版本" }),
    ).toHaveTextContent(/v2/);
    expect(
      within(dialog).getByRole("checkbox", {
        name: "每次脚本执行均需用户审批",
      }),
    ).not.toBeChecked();
  });

  it("已启用版本的依赖后来失效时仍允许关闭技能", async () => {
    const unsatisfied = makeDependencyCheck({
      status: "dependency_unsatisfied",
    });
    const initial = makeSkillDetails(
      {
        dependency_check: unsatisfied,
        enabled: true,
        id: "skill-disable-stale",
      },
      { dependency_check: unsatisfied },
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = requestUrl(input);
          const method = init?.method ?? "GET";
          if (url.pathname.endsWith("/auth/me")) {
            return jsonResponse(TEST_ADMIN);
          }
          if (url.pathname === "/api/v1/admin/skills" && method === "GET") {
            return jsonResponse({ items: [initial] });
          }
          if (
            url.pathname === `/api/v1/admin/skills/${initial.id}` &&
            method === "GET"
          ) {
            return jsonResponse(initial);
          }
          if (
            url.pathname ===
              `/api/v1/admin/skills/${initial.id}/versions` &&
            method === "GET"
          ) {
            return jsonResponse(versionListResponse(initial));
          }
          throw new Error(`Unexpected request: ${method} ${url}`);
        },
      ),
    );
    const user = userEvent.setup();

    renderApp("/admin/skills");
    await user.click(
      await screen.findByRole("button", {
        name: "查看版本与指南",
      }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "技能：销售报表",
    });
    const enabledToggle = within(dialog).getByRole("checkbox", {
      name: "向普通用户启用该技能",
    });

    expect(enabledToggle).toBeChecked();
    expect(enabledToggle).toBeEnabled();
    await user.click(enabledToggle);
    expect(enabledToggle).not.toBeChecked();
  });

  it("上传新版本使用 package 与可选 label，且不会自动切换当前版本", async () => {
    const initial = makeSkillDetails();
    const nextVersion = makeSkillVersion({
      id: "skill-version-2",
      label: "2026 年 11 月",
      ordinal: 2,
      skill_id: initial.id,
    });
    let uploadedPackage: File | null = null;
    let uploadUrl = "";
    let uploadIdempotencyKey: string | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = requestUrl(input);
          const method = init?.method ?? "GET";
          if (url.pathname.endsWith("/auth/me")) {
            return jsonResponse(TEST_ADMIN);
          }
          if (url.pathname === "/api/v1/admin/skills" && method === "GET") {
            return jsonResponse({ items: [initial] });
          }
          if (
            url.pathname === `/api/v1/admin/skills/${initial.id}` &&
            method === "GET"
          ) {
            return jsonResponse({
              ...initial,
              versions: [nextVersion, ...initial.versions],
            });
          }
          if (
            url.pathname ===
              `/api/v1/admin/skills/${initial.id}/versions` &&
            method === "GET"
          ) {
            return jsonResponse(versionListResponse(initial));
          }
          if (
            url.pathname ===
              `/api/v1/admin/skills/${initial.id}/versions` &&
            method === "POST"
          ) {
            uploadUrl = url.toString();
            uploadIdempotencyKey = new Headers(init?.headers).get(
              "Idempotency-Key",
            );
            uploadedPackage = (
              init?.body as FormData
            ).get("package") as File;
            return jsonResponse(nextVersion, 201);
          }
          throw new Error(`Unexpected request: ${method} ${url}`);
        },
      ),
    );
    const user = userEvent.setup();

    renderApp("/admin/skills");

    await user.click(
      await screen.findByRole("button", {
        name: "查看版本与指南",
      }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "技能：销售报表",
    });
    const file = new File(["new"], "sales-v2.zip", {
      type: "application/zip",
    });
    await user.upload(
      within(dialog).getByLabelText("选择新版本 ZIP"),
      file,
    );
    await user.type(
      within(dialog).getByLabelText("版本标签（可选）"),
      "2026 年 11 月",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "上传新版本" }),
    );

    expect(
      await within(dialog).findByText(/已保存。它尚未自动成为当前版本/),
    ).toBeInTheDocument();
    expect(uploadedPackage).toBe(file);
    expect(uploadUrl).toContain("label=2026+%E5%B9%B4+11+%E6%9C%88");
    expect(uploadIdempotencyKey).toMatch(/^[A-Za-z0-9._~-]+$/);
    expect(
      within(dialog).getByRole("combobox", { name: "当前版本" }),
    ).toHaveTextContent(/v1/);
  });

  it("上传新版本 422 显示违规 ZIP 成员路径并保留当前版本", async () => {
    const initial = makeSkillDetails();
    const memberPath = "other/中文包/无效.csv";
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = requestUrl(input);
          const method = init?.method ?? "GET";
          if (url.pathname.endsWith("/auth/me")) {
            return jsonResponse(TEST_ADMIN);
          }
          if (url.pathname === "/api/v1/admin/skills" && method === "GET") {
            return jsonResponse({ items: [initial] });
          }
          if (
            url.pathname === `/api/v1/admin/skills/${initial.id}` &&
            method === "GET"
          ) {
            return jsonResponse(initial);
          }
          if (
            url.pathname ===
              `/api/v1/admin/skills/${initial.id}/versions` &&
            method === "GET"
          ) {
            return jsonResponse(versionListResponse(initial));
          }
          if (
            url.pathname ===
              `/api/v1/admin/skills/${initial.id}/versions` &&
            method === "POST"
          ) {
            return jsonResponse(
              {
                detail: {
                  code: "skill_zip_path_invalid",
                  member_path: memberPath,
                },
              },
              422,
            );
          }
          throw new Error(`Unexpected request: ${method} ${url}`);
        },
      ),
    );
    const user = userEvent.setup();

    renderApp("/admin/skills");

    await user.click(
      await screen.findByRole("button", {
        name: "查看版本与指南",
      }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "技能：销售报表",
    });
    await user.upload(
      within(dialog).getByLabelText("选择新版本 ZIP"),
      new File(["invalid"], "invalid-v2.zip", {
        type: "application/zip",
      }),
    );
    await user.click(
      within(dialog).getByRole("button", { name: "上传新版本" }),
    );

    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent(
      "技能包包含不受支持的文件位置或目录规则",
    );
    expect(alert.textContent).toContain(`包内文件：${memberPath}`);
    expect(alert.querySelector("a")).toBeNull();
    expect(
      within(dialog).getByRole("combobox", { name: "当前版本" }),
    ).toHaveTextContent(/v1/);
  });

  it("支持通过自定义版本下拉框切换版本并显示未保存变更，且支持清除已选 ZIP 文件", async () => {
    const v1 = makeSkillVersion({
      id: "skill-version-1",
      label: "稳定版",
      ordinal: 1,
      skill_id: "skill-custom-controls",
    });
    const v2 = makeSkillVersion({
      dependency_check: {
        ...makeDependencyCheck(),
        status: "dependency_unsatisfied",
      },
      id: "skill-version-2",
      label: "候选版",
      ordinal: 2,
      skill_id: "skill-custom-controls",
    });
    const initial = {
      ...makeSkillDetails({
        current_version_id: v1.id,
        current_version_label: v1.label,
        id: "skill-custom-controls",
      }),
      versions: [v2, v1],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (url.pathname === "/api/v1/admin/skills" && method === "GET") {
          return jsonResponse({ items: [initial] });
        }
        if (
          url.pathname === `/api/v1/admin/skills/${initial.id}` &&
          method === "GET"
        ) {
          return jsonResponse(initial);
        }
        if (
          url.pathname === `/api/v1/admin/skills/${initial.id}/versions` &&
          method === "GET"
        ) {
          return jsonResponse(versionListResponse(initial, [v2, v1]));
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
      name: "技能：销售报表",
    });

    const combobox = within(dialog).getByRole("combobox", {
      name: "当前版本",
    });
    expect(combobox).toHaveTextContent(/稳定版/);
    expect(within(dialog).queryByText("未保存变更")).not.toBeInTheDocument();

    const fileInput = within(dialog).getByLabelText("选择新版本 ZIP");
    const fileTrigger = within(dialog).getByRole("button", {
      name: "选择 ZIP 文件",
    });
    expect(fileInput).toHaveAttribute("tabindex", "-1");
    fileTrigger.focus();
    await user.tab();
    expect(within(dialog).getByLabelText("版本标签（可选）")).toHaveFocus();
    expect(fileInput).not.toHaveFocus();

    await user.click(combobox);
    // Radix SelectContent 通过 Portal 挂载到 document.body，先定位 listbox 再查找候选版本项。
    const listbox = screen.getByRole("listbox", { name: "当前版本列表" });
    const optionV2 = within(listbox).getByRole("option", { name: /候选版/ });
    expect(optionV2).toHaveTextContent("依赖未满足");
    await user.click(optionV2);

    expect(combobox).toHaveTextContent(/候选版/);
    expect(combobox).not.toHaveTextContent("依赖未满足");
    expect(within(dialog).getByText("未保存变更")).toBeInTheDocument();

    expect(within(dialog).getByText("未选择 .zip 文件")).toBeInTheDocument();
    await user.upload(
      fileInput,
      new File(["zip"], "candidate-v3.zip", { type: "application/zip" }),
    );
    expect(within(dialog).getByText(/candidate-v3\.zip/)).toBeInTheDocument();

    await user.click(
      within(dialog).getByRole("button", { name: "清除已选文件" }),
    );
    expect(within(dialog).getByText("未选择 .zip 文件")).toBeInTheDocument();
  });

  it("部门管理员展示「部门技能管理」、导入自动绑定唯一管辖部门并支持软删部门技能；全公司技能绝不显示删除按钮", async () => {
    const deptAdmin = makeDeptAdmin();
    const deptSkill = makeSkillDetails({
      id: "skill-fin-dept",
      name: "财务报销核对",
      owner_department_id: "dept-fin",
      revision: 3,
      scope: "department",
      slug: "fin-expense-check",
    });
    const companySkill = makeSkillDetails({
      id: "skill-company-global",
      name: "全公司通用模板",
      owner_department_id: null,
      revision: 1,
      scope: "company",
      slug: "company-template",
    });
    let currentSkills = [deptSkill];
    let importForm: FormData | null = null;
    let deletedUrl: string | null = null;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(deptAdmin);
        }
        if (url.pathname === "/api/v1/admin/skills" && method === "GET") {
          return jsonResponse({ items: currentSkills });
        }
        if (url.pathname === "/api/v1/skills" && method === "GET") {
          return jsonResponse({ items: [] });
        }
        if (
          url.pathname === "/api/v1/admin/skills/import" &&
          method === "POST"
        ) {
          importForm = init?.body as FormData;
          return jsonResponse(deptSkill, 201);
        }
        if (url.pathname === `/api/v1/admin/skills/${deptSkill.id}`) {
          if (method === "GET") {
            return jsonResponse(deptSkill);
          }
          if (method === "DELETE") {
            deletedUrl = `${url.pathname}${url.search}`;
            currentSkills = [];
            return new Response(null, { status: 204 });
          }
        }
        if (
          url.pathname === `/api/v1/admin/skills/${deptSkill.id}/versions` &&
          method === "GET"
        ) {
          return jsonResponse(versionListResponse(deptSkill));
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );
    const user = userEvent.setup();

    const firstView = renderApp("/admin/skills");
    expect(
      await screen.findByRole("heading", { level: 1, name: "部门技能管理" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("财务报销核对")).toBeInTheDocument();
    expect(
      within(screen.getByRole("main")).getByText("财务部"),
    ).toBeInTheDocument();

    // 1. 部门管理员导入技能：固定 department 范围并自动选中唯一管辖部门 dept-fin。
    await user.click(screen.getByRole("button", { name: "导入技能 ZIP" }));
    const importDialog = screen.getByRole("dialog", { name: "导入技能包" });
    expect(
      within(importDialog).queryByLabelText("发布范围"),
    ).not.toBeInTheDocument();
    expect(
      within(importDialog).getByRole("combobox", { name: "归属部门" }),
    ).toHaveTextContent("财务部");
    await user.upload(
      within(importDialog).getByLabelText("技能 ZIP"),
      new File(["zip"], "fin.zip", { type: "application/zip" }),
    );
    await user.click(
      within(importDialog).getByRole("button", { name: "导入并查看检查结果" }),
    );

    const detailDialog = await screen.findByRole("dialog", {
      name: "技能：财务报销核对",
    });
    expect(importForm).toBeInstanceOf(FormData);
    expect((importForm as unknown as FormData).get("scope")).toBe("department");
    expect((importForm as unknown as FormData).get("owner_department_id")).toBe(
      "dept-fin",
    );

    // 2. 部门级技能详情展示「删除部门技能」按钮并通过嵌套子 Modal 二次确认（支持取消、Escape 仅关栈顶确认框及携带 expected_revision=3 删除）。
    await user.click(
      within(detailDialog).getByRole("button", { name: "删除部门技能" }),
    );
    const firstConfirm = screen.getByRole("dialog", { name: "删除部门技能" });
    expect(firstConfirm).toHaveAttribute("aria-modal", "true");
    // 点击取消仅关闭嵌套确认弹窗，外层详情弹窗保持打开。
    await user.click(within(firstConfirm).getByRole("button", { name: "取消" }));
    expect(
      screen.queryByRole("dialog", { name: "删除部门技能" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("dialog", { name: "技能：财务报销核对" }),
    ).toBeInTheDocument();

    // 再次打开确认弹窗并按 Escape：仅栈顶确认弹窗关闭，外层详情弹窗不关闭。
    await user.click(
      within(detailDialog).getByRole("button", { name: "删除部门技能" }),
    );
    expect(
      screen.getByRole("dialog", { name: "删除部门技能" }),
    ).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(
      screen.queryByRole("dialog", { name: "删除部门技能" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("dialog", { name: "技能：财务报销核对" }),
    ).toBeInTheDocument();

    // 正式确认删除。
    await user.click(
      within(detailDialog).getByRole("button", { name: "删除部门技能" }),
    );
    const confirmDelete = screen.getByRole("dialog", {
      name: "删除部门技能",
    });
    await user.click(
      within(confirmDelete).getByRole("button", { name: "确认删除" }),
    );
    await waitFor(() => {
      expect(deletedUrl).toBe(
        `/api/v1/admin/skills/${deptSkill.id}?expected_revision=3`,
      );
    });
    expect(
      await screen.findByRole("heading", { name: "还没有技能包" }),
    ).toBeInTheDocument();
    firstView.unmount();

    // 3. 全公司（company）技能详情中绝不展示任何删除按钮。
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        const method = init?.method ?? "GET";
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (url.pathname === "/api/v1/admin/skills" && method === "GET") {
          return jsonResponse({ items: [companySkill] });
        }
        if (
          url.pathname === `/api/v1/admin/skills/${companySkill.id}` &&
          method === "GET"
        ) {
          return jsonResponse(companySkill);
        }
        if (
          url.pathname ===
            `/api/v1/admin/skills/${companySkill.id}/versions` &&
          method === "GET"
        ) {
          return jsonResponse(versionListResponse(companySkill));
        }
        throw new Error(`Unexpected request: ${method} ${url}`);
      }),
    );
    renderApp("/admin/skills");
    await user.click(
      await screen.findByRole("button", { name: "查看版本与指南" }),
    );
    const companyDialog = await screen.findByRole("dialog", {
      name: "技能：全公司通用模板",
    });
    expect(within(companyDialog).getByText("全公司")).toBeInTheDocument();
    expect(
      within(companyDialog).queryByRole("button", { name: /删除/ }),
    ).not.toBeInTheDocument();
  });
});
