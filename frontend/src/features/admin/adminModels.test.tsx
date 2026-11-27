import {
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  TEST_USER,
  getRequestUrl,
} from "../../test/fixtures";
import {
  makeCompressionPolicy,
  makeModelProfile,
  TEST_ADMIN,
} from "../../test/adminFixtures";
import { jsonResponse, renderApp } from "../../test/testUtils";
import { adminKeys } from "./queryKeys";

function requestUrl(input: RequestInfo | URL): URL {
  return new URL(getRequestUrl(input), "http://localhost");
}

const PRIMARY = makeModelProfile({
  id: "model-primary",
  name: "主力模型",
});
const DISABLED = makeModelProfile({
  enabled: false,
  id: "model-disabled",
  model: "atlas-disabled",
  name: "备用模型",
});
const NO_TOOLS = makeModelProfile({
  id: "model-no-tools",
  model: "atlas-text-only",
  name: "纯文本模型",
  supports_parallel_tool_calls: false,
  supports_tools: false,
});

function baseFetch(
  handler: (
    url: URL,
    init?: RequestInit,
  ) => Response | Promise<Response> | null,
) {
  return vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = requestUrl(input);
      if (url.pathname.endsWith("/auth/me")) {
        return jsonResponse(TEST_ADMIN);
      }
      const handled = handler(url, init);
      if (handled) {
        return handled;
      }
      throw new Error(
        `Unexpected request: ${init?.method ?? "GET"} ${url}`,
      );
    },
  );
}

describe("管理模型页", () => {
  function visibleProfileNames(): string[] {
    return screen
      .getAllByRole("heading", { level: 3 })
      .map((heading) => heading.textContent ?? "");
  }

  it("设默认只提交符合条件的版本，失败提示后重试成功", async () => {
    const eligible = makeModelProfile({
      id: "model-eligible",
      name: "候选模型",
    });
    const disabledProfile = makeModelProfile({
      enabled: false,
      id: "model-disabled-candidate",
      name: "停用模型",
    });
    let defaultCalls = 0;
    const fetchMock = baseFetch((url, init) => {
      if (
        url.pathname === "/api/v1/admin/llm/model-profiles" &&
        (!init?.method || init.method === "GET")
      ) {
        return jsonResponse({
          default_profile_id: PRIMARY.id,
          items: [PRIMARY, eligible, disabledProfile],
        });
      }
      if (
        url.pathname ===
          "/api/v1/admin/llm/model-profiles/default" &&
        init?.method === "PUT"
      ) {
        defaultCalls += 1;
        return defaultCalls === 1
          ? jsonResponse(
              { detail: "llm_model_profile_conflict" },
              409,
            )
          : jsonResponse(eligible);
      }
      if (
        url.pathname ===
        "/api/v1/admin/llm/compression-policy"
      ) {
        return jsonResponse(
          makeCompressionPolicy({
            compression_model_profile_id: null,
            created_at: null,
            id: null,
            recent_ratio: null,
            recent_token_cap: null,
            summary_ratio: null,
            summary_token_cap: null,
            trigger_tokens: null,
          }),
        );
      }
      return null;
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/admin/models");

    await screen.findByText(eligible.name);
    const disabledCard = screen
      .getAllByRole("heading", { name: disabledProfile.name })
      .map((heading) => heading.closest("li"))
      .find((element) => element !== null) as HTMLElement;
    expect(
      within(disabledCard).getByRole("button", { name: "设为默认" }),
    ).toBeDisabled();
    const eligibleCard = screen
      .getAllByRole("heading", { name: eligible.name })
      .map((heading) => heading.closest("li"))
      .find((element) => element !== null) as HTMLElement;
    await user.click(
      within(eligibleCard).getByRole("button", {
        name: "设为默认",
      }),
    );
    expect(
      await screen.findByText(/该模型配置与已有版本冲突/),
    ).toBeInTheDocument();
    expect(
      within(eligibleCard).queryByText("当前默认"),
    ).not.toBeInTheDocument();

    await user.click(
      within(eligibleCard).getByRole("button", {
        name: "设为默认",
      }),
    );
    await waitFor(() => {
      expect(
        within(eligibleCard).getByText("当前默认"),
      ).toBeInTheDocument();
    });
    const calls = fetchMock.mock.calls.filter(
      ([input, init]) =>
        requestUrl(input).pathname ===
          "/api/v1/admin/llm/model-profiles/default" &&
        init?.method === "PUT",
    );
    expect(calls).toHaveLength(2);
    expect(JSON.parse(String(calls[0][1]?.body))).toEqual({
      profile_id: eligible.id,
    });
  });

  it("默认版本置顶，其余版本按创建时间倒序并每页展示五项", async () => {
    const defaultProfile = makeModelProfile({
      created_at: "2026-01-01T00:00:00Z",
      id: "model-default-oldest",
      name: "默认旧版本",
    });
    const datedProfiles = Array.from({ length: 11 }, (_, index) =>
      makeModelProfile({
        created_at: `2026-01-${String(index + 2).padStart(2, "0")}T00:00:00Z`,
        id: `model-${index + 1}`,
        name: `版本 ${String(index + 1).padStart(2, "0")}`,
      }),
    );
    const profiles = [defaultProfile, ...datedProfiles];
    let defaultProfileId = defaultProfile.id;
    const fetchMock = baseFetch((url, init) => {
      if (
        url.pathname === "/api/v1/admin/llm/model-profiles" &&
        (!init?.method || init.method === "GET")
      ) {
        return jsonResponse({
          default_profile_id: defaultProfileId,
          items: [...profiles].reverse(),
        });
      }
      if (
        url.pathname ===
          "/api/v1/admin/llm/model-profiles/default" &&
        init?.method === "PUT"
      ) {
        const profileId = (
          JSON.parse(String(init.body)) as { profile_id: string }
        ).profile_id;
        const selected = profiles.find(
          (profile) => profile.id === profileId,
        );
        if (!selected) {
          throw new Error(`Unknown model profile: ${profileId}`);
        }
        defaultProfileId = selected.id;
        return jsonResponse(selected);
      }
      if (
        url.pathname ===
        "/api/v1/admin/llm/compression-policy"
      ) {
        return jsonResponse(
          makeCompressionPolicy({
            compression_model_profile_id: null,
            created_at: null,
            id: null,
            recent_ratio: null,
            recent_token_cap: null,
            summary_ratio: null,
            summary_token_cap: null,
            trigger_tokens: null,
          }),
        );
      }
      return null;
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/admin/models");

    await screen.findByText(defaultProfile.name);
    expect(await screen.findByText("共 12 个版本")).toBeInTheDocument();
    expect(screen.getByText("第 1 页 · 共 12 项")).toBeInTheDocument();
    expect(visibleProfileNames()).toEqual([
      defaultProfile.name,
      "版本 11",
      "版本 10",
      "版本 09",
      "版本 08",
    ]);

    await user.click(
      screen.getByRole("button", { name: "下一页" }),
    );
    expect(screen.getByText("第 2 页 · 共 12 项")).toBeInTheDocument();
    expect(visibleProfileNames()).toEqual([
      "版本 07",
      "版本 06",
      "版本 05",
      "版本 04",
      "版本 03",
    ]);

    const targetCard = screen
      .getByRole("heading", { name: "版本 07" })
      .closest("li");
    expect(targetCard).not.toBeNull();
    await user.click(
      within(targetCard as HTMLElement).getByRole("button", {
        name: "设为默认",
      }),
    );

    await waitFor(() => {
      expect(visibleProfileNames()).toEqual([
        "版本 06",
        "版本 05",
        "版本 04",
        "版本 03",
        "版本 02",
      ]);
    });
    await user.click(
      screen.getByRole("button", { name: "上一页" }),
    );
    expect(visibleProfileNames()).toEqual([
      "版本 07",
      "版本 11",
      "版本 10",
      "版本 09",
      "版本 08",
    ]);
  });

  it("从后续页创建模型成功后回到第一页并显示新版本", async () => {
    const existingProfiles = Array.from({ length: 6 }, (_, index) =>
      makeModelProfile({
        created_at: `2026-01-0${index + 1}T00:00:00Z`,
        id: `model-existing-${index + 1}`,
        name: `已有版本 ${index + 1}`,
      }),
    );
    const created = makeModelProfile({
      created_at: "2026-02-01T00:00:00Z",
      id: "model-new",
      name: "新建版本",
    });
    let createdOnServer = false;
    const fetchMock = baseFetch((url, init) => {
      if (
        url.pathname === "/api/v1/admin/llm/model-profiles" &&
        init?.method === "POST"
      ) {
        createdOnServer = true;
        return jsonResponse(created, 201);
      }
      if (
        url.pathname === "/api/v1/admin/llm/model-profiles" &&
        (!init?.method || init.method === "GET")
      ) {
        return jsonResponse({
          default_profile_id: null,
          items: createdOnServer
            ? [...existingProfiles, created]
            : existingProfiles,
        });
      }
      if (
        url.pathname ===
        "/api/v1/admin/llm/compression-policy"
      ) {
        return jsonResponse(
          makeCompressionPolicy({
            compression_model_profile_id: null,
            created_at: null,
            id: null,
            recent_ratio: null,
            recent_token_cap: null,
            summary_ratio: null,
            summary_token_cap: null,
            trigger_tokens: null,
          }),
        );
      }
      return null;
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/admin/models");

    await screen.findByText("第 1 页 · 共 6 项");
    await user.click(
      screen.getByRole("button", { name: "下一页" }),
    );
    expect(screen.getByText("第 2 页 · 共 6 项")).toBeInTheDocument();
    expect(visibleProfileNames()).toEqual(["已有版本 1"]);

    await user.click(
      screen.getByRole("button", { name: "创建模型版本" }),
    );
    const dialog = screen.getByRole("dialog", {
      name: "创建模型版本",
    });
    await user.type(
      within(dialog).getByLabelText("版本名称"),
      created.name,
    );
    await user.type(
      within(dialog).getByLabelText("模型标识"),
      "atlas-new",
    );
    await user.type(
      within(dialog).getByLabelText(/^HTTPS Base URL/),
      "https://llm.example/v1",
    );
    await user.type(
      within(dialog).getByLabelText(/^API Key/),
      "test-key",
    );
    await user.click(
      within(dialog).getByRole("button", {
        name: "创建模型版本",
      }),
    );

    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", { name: "创建模型版本" }),
      ).not.toBeInTheDocument();
    });
    expect(await screen.findByText("第 1 页 · 共 7 项")).toBeInTheDocument();
    expect(screen.getByText("共 7 个版本")).toBeInTheDocument();
    expect(visibleProfileNames()).toEqual([
      created.name,
      "已有版本 6",
      "已有版本 5",
      "已有版本 4",
      "已有版本 3",
    ]);
  });

  it("展示真实字段、限制默认资格并确认停用当前默认版本", async () => {
    let profiles = [PRIMARY, DISABLED, NO_TOOLS];
    const fetchMock = baseFetch((url, init) => {
      if (
        url.pathname === "/api/v1/admin/llm/model-profiles" &&
        (!init?.method || init.method === "GET")
      ) {
        return jsonResponse({
          default_profile_id: PRIMARY.id,
          items: profiles,
        });
      }
      if (
        url.pathname ===
          `/api/v1/admin/llm/model-profiles/${PRIMARY.id}/enabled` &&
        init?.method === "PUT"
      ) {
        const disabled = { ...PRIMARY, enabled: false };
        profiles = [
          disabled,
          DISABLED,
          NO_TOOLS,
        ];
        return jsonResponse(disabled);
      }
      if (
        url.pathname ===
        "/api/v1/admin/llm/compression-policy"
      ) {
        return jsonResponse(makeCompressionPolicy());
      }
      return null;
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/admin/models");

    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "模型与压缩策略",
      }),
    ).toBeInTheDocument();
    expect(await screen.findByText("共 3 个版本")).toBeInTheDocument();
    expect(screen.getAllByText(PRIMARY.base_url)).toHaveLength(3);
    expect(
      screen.getAllByText("已配置 API Key"),
    ).toHaveLength(3);
    expect(screen.getByText("当前默认")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: `编辑配置：${PRIMARY.name}` }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: `删除模型：${PRIMARY.name}` }),
    ).toBeInTheDocument();

    const noToolsCard = screen
      .getByRole("heading", { name: NO_TOOLS.name })
      .closest("li");
    const disabledCard = screen
      .getByRole("heading", { name: DISABLED.name })
      .closest("li");
    expect(noToolsCard).not.toBeNull();
    expect(disabledCard).not.toBeNull();
    expect(
      within(noToolsCard as HTMLElement).getByRole("button", {
        name: "设为默认",
      }),
    ).toBeDisabled();
    expect(
      within(disabledCard as HTMLElement).getByRole("button", {
        name: "设为默认",
      }),
    ).toBeDisabled();

    const primaryCard = screen
      .getAllByRole("heading", { name: PRIMARY.name })
      .find((heading) => heading.closest("li"))
      ?.closest("li");
    expect(primaryCard).not.toBeNull();
    await user.click(
      within(primaryCard as HTMLElement).getByRole("button", {
        name: "停用版本",
      }),
    );
    const dialog = screen.getByRole("dialog", {
      name: "停用当前默认模型",
    });
    expect(
      within(dialog).getByText(/新 Run 可能因没有可用默认模型而失败/),
    ).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(
        ([input, init]) =>
          requestUrl(input).pathname.endsWith("/enabled") &&
          init?.method === "PUT",
      ),
    ).toBe(false);

    await user.click(
      within(dialog).getByRole("checkbox", {
        name: /我了解新 Run 可能失败/,
      }),
    );
    await user.click(
      within(dialog).getByRole("button", {
        name: "确认停用默认版本",
      }),
    );

    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", {
          name: "停用当前默认模型",
        }),
      ).not.toBeInTheDocument();
      expect(
        within(primaryCard as HTMLElement).getByText("已停用"),
      ).toBeInTheDocument();
    });
    const putCall = fetchMock.mock.calls.find(
      ([input, init]) =>
        requestUrl(input).pathname ===
          `/api/v1/admin/llm/model-profiles/${PRIMARY.id}/enabled` &&
        init?.method === "PUT",
    );
    expect(JSON.parse(String(putCall?.[1]?.body))).toEqual({
      enabled: false,
    });
  });

  it("创建表单拦截 URL、容量、凭据与工具并行约束，并提交真实字段", async () => {
    const created = makeModelProfile({
      id: "model-created",
      name: "审核模型",
    });
    const fetchMock = baseFetch((url, init) => {
      if (
        url.pathname === "/api/v1/admin/llm/model-profiles" &&
        init?.method === "POST"
      ) {
        return jsonResponse(created, 201);
      }
      if (
        url.pathname === "/api/v1/admin/llm/model-profiles" &&
        (!init?.method || init.method === "GET")
      ) {
        return jsonResponse({
          default_profile_id: null,
          items: [created],
        });
      }
      if (
        url.pathname ===
        "/api/v1/admin/llm/compression-policy"
      ) {
        return jsonResponse(
          makeCompressionPolicy({
            compression_model_profile_id: null,
            created_at: null,
            id: null,
            recent_ratio: null,
            recent_token_cap: null,
            summary_ratio: null,
            summary_token_cap: null,
            trigger_tokens: null,
          }),
        );
      }
      return null;
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/admin/models");

    await screen.findByRole("heading", {
      level: 1,
      name: "模型与压缩策略",
    });
    await user.click(
      screen.getByRole("button", { name: "创建模型版本" }),
    );
    const dialog = screen.getByRole("dialog", {
      name: "创建模型版本",
    });
    await user.type(
      within(dialog).getByLabelText("版本名称"),
      "审核模型",
    );
    await user.type(
      within(dialog).getByLabelText("模型标识"),
      "atlas-review",
    );
    await user.type(
      within(dialog).getByLabelText(/^HTTPS Base URL/),
      "http://llm.example/v1",
    );
    const apiKey = within(dialog).getByLabelText(/^API Key/);
    await user.type(
      within(dialog).getByLabelText("场景标签（可选）"),
      "专业审核",
    );
    await user.type(
      within(dialog).getByLabelText("场景说明（可选）"),
      "用于多模态文档核验。",
    );
    await user.click(
      within(dialog).getByRole("checkbox", { name: "支持工具调用" }),
    );
    await user.click(
      within(dialog).getByRole("checkbox", { name: "支持识图 / 多模态" }),
    );
    await user.click(
      within(dialog).getByRole("checkbox", { name: "对普通用户可见" }),
    );
    expect(
      within(dialog).getByRole("checkbox", {
        name: "支持并行工具调用",
      }),
    ).toBeDisabled();
    await user.click(
      within(dialog).getByRole("button", {
        name: "创建模型版本",
      }),
    );

    expect(
      within(dialog).getByText("地址必须使用 HTTPS。"),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByText(
        "API Key 不能为空、不能包含 NUL 字符，且加密内容不能超过 16 KiB。",
      ),
    ).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(
        ([input, init]) =>
          requestUrl(input).pathname ===
            "/api/v1/admin/llm/model-profiles" &&
          init?.method === "POST",
      ),
    ).toBe(false);

    const baseUrl = within(dialog).getByLabelText(/^HTTPS Base URL/);
    await user.clear(baseUrl);
    await user.type(baseUrl, "https://llm.example/v1");
    await user.type(apiKey, "test-key");
    await user.click(
      within(dialog).getByRole("checkbox", { name: "支持工具调用" }),
    );
    const context = within(dialog).getByLabelText(
      "上下文窗口 tokens",
    );
    await user.clear(context);
    await user.type(context, "100");
    await user.click(
      within(dialog).getByRole("button", {
        name: "创建模型版本",
      }),
    );
    expect(
      within(dialog).getByText(
        "最大输出需为正整数且不能超过上下文窗口。",
      ),
    ).toBeInTheDocument();

    await user.clear(context);
    await user.type(context, "128000");
    await user.click(
      within(dialog).getByRole("button", {
        name: "创建模型版本",
      }),
    );

    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", { name: "创建模型版本" }),
      ).not.toBeInTheDocument();
    });
    const createCall = fetchMock.mock.calls.find(
      ([input, init]) =>
        requestUrl(input).pathname ===
          "/api/v1/admin/llm/model-profiles" &&
        init?.method === "POST",
    );
    expect(JSON.parse(String(createCall?.[1]?.body))).toMatchObject({
      base_url: "https://llm.example/v1",
      context_window_tokens: 128000,
      api_key: "test-key",
      model: "atlas-review",
      name: "审核模型",
      scenario_label: "专业审核",
      scenario_description: "用于多模态文档核验。",
      supports_parallel_tool_calls: false,
      supports_tools: true,
      supports_vision: true,
      visible_to_users: false,
    });
  });

  it("压缩策略只列出已启用模型，校验五字段并保存真实请求", async () => {
    const unconfigured = makeCompressionPolicy({
      compression_model_profile_id: null,
      created_at: null,
      id: null,
      recent_ratio: null,
      recent_token_cap: null,
      summary_ratio: null,
      summary_token_cap: null,
      trigger_tokens: null,
    });
    const saved = makeCompressionPolicy({
      recent_token_cap: 70000,
      summary_token_cap: 50000,
      trigger_tokens: 300000,
    });
    const fetchMock = baseFetch((url, init) => {
      if (
        url.pathname === "/api/v1/admin/llm/model-profiles" &&
        (!init?.method || init.method === "GET")
      ) {
        return jsonResponse({
          default_profile_id: PRIMARY.id,
          items: [PRIMARY, DISABLED],
        });
      }
      if (
        url.pathname ===
          "/api/v1/admin/llm/compression-policy" &&
        init?.method === "PUT"
      ) {
        return jsonResponse(saved);
      }
      if (
        url.pathname ===
        "/api/v1/admin/llm/compression-policy"
      ) {
        return jsonResponse(unconfigured);
      }
      return null;
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/admin/models");

    expect(
      await screen.findByRole("heading", {
        name: "尚未配置上下文压缩",
      }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("switch", { name: /关闭.*压缩/ }),
    ).not.toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "配置压缩策略" }),
    );
    const dialog = screen.getByRole("dialog", {
      name: "配置上下文压缩",
    });
    const modelSelect = within(dialog).getByRole("combobox", {
      name: "压缩模型",
    });
    await user.click(modelSelect);
    const primaryOption = screen.getByRole("option", {
      name: new RegExp(PRIMARY.name),
    });
    expect(primaryOption).toBeInTheDocument();
    expect(
      screen.queryByRole("option", {
        name: new RegExp(DISABLED.name),
      }),
    ).not.toBeInTheDocument();
    await user.click(primaryOption);

    const triggerTokens = within(dialog).getByLabelText("触发 Token");
    const recent = within(dialog).getByLabelText("近期原文比例");
    const recentTokenCap =
      within(dialog).getByLabelText("近期原文 Token 上限");
    const summary = within(dialog).getByLabelText("摘要比例");
    const summaryTokenCap =
      within(dialog).getByLabelText("摘要 Token 上限");
    await user.clear(triggerTokens);
    await user.type(triggerTokens, "300000");
    await user.clear(recentTokenCap);
    await user.type(recentTokenCap, "70000");
    await user.clear(summaryTokenCap);
    await user.type(summaryTokenCap, "50000");
    await user.clear(recent);
    await user.type(recent, "0.6");
    await user.clear(summary);
    await user.type(summary, "0.5");
    await user.click(
      within(dialog).getByRole("button", {
        name: "保存压缩策略",
      }),
    );
    expect(
      within(dialog).getByText(
        "近期原文与摘要比例之和必须小于 1。",
      ),
    ).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(
        ([input, init]) =>
          requestUrl(input).pathname ===
            "/api/v1/admin/llm/compression-policy" &&
          init?.method === "PUT",
      ),
    ).toBe(false);

    await user.clear(recent);
    await user.type(recent, "0.2");
    await user.clear(summary);
    await user.type(summary, "0.1");
    expect(
      within(dialog).getByText(
        "派生近期原文预算：60,000 Token · 派生摘要预算：30,000 Token",
      ),
    ).toBeInTheDocument();
    await user.click(
      within(dialog).getByRole("button", {
        name: "保存压缩策略",
      }),
    );
    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", { name: "配置上下文压缩" }),
      ).not.toBeInTheDocument();
    });
    expect(
      await screen.findByText(/策略版本 policy-1/),
    ).toBeInTheDocument();
    const putCall = fetchMock.mock.calls.find(
      ([input, init]) =>
        requestUrl(input).pathname ===
          "/api/v1/admin/llm/compression-policy" &&
        init?.method === "PUT",
    );
    expect(JSON.parse(String(putCall?.[1]?.body))).toEqual({
      compression_model_profile_id: PRIMARY.id,
      recent_ratio: 0.2,
      recent_token_cap: 70000,
      summary_ratio: 0.1,
      summary_token_cap: 50000,
      trigger_tokens: 300000,
    });
    const policyStats = screen.getByLabelText(
      "压缩策略 Token 与派生预算",
    );
    expect(within(policyStats).getByText("300,000 Token")).toBeInTheDocument();
    expect(within(policyStats).getByText("70,000 Token")).toBeInTheDocument();
    expect(within(policyStats).getByText("60,000 Token")).toBeInTheDocument();
    expect(within(policyStats).getByText("30,000 Token")).toBeInTheDocument();
  });

  it("403 admin_required 会清空管理缓存并重新核验为无权限", async () => {
    let authCalls = 0;
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          authCalls += 1;
          return jsonResponse(
            authCalls === 1 ? TEST_ADMIN : TEST_USER,
          );
        }
        if (url.pathname.startsWith("/api/v1/admin/")) {
          return jsonResponse({ detail: "admin_required" }, 403);
        }
        throw new Error(`Unexpected request: ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    const app = renderApp("/admin/models");

    expect(
      await screen.findByRole("heading", {
        name: "当前账号没有管理员权限",
      }),
    ).toBeInTheDocument();
    expect(authCalls).toBeGreaterThanOrEqual(2);
    expect(
      app.client.getQueryCache().findAll({
        queryKey: adminKeys.all,
      }),
    ).toHaveLength(0);
    expect(
      screen.queryByRole("heading", { name: "模型版本" }),
    ).not.toBeInTheDocument();
  });

  it("兼容展示一期可选字段：场景标签、仅后台可见标识与识图能力标签", async () => {
    const visionProfile = makeModelProfile({
      id: "model-vision",
      name: "多模态办公模型",
      scenario_label: "综合办公",
      supports_vision: true,
      visible_to_users: false,
    });
    vi.stubGlobal(
      "fetch",
      baseFetch((url) => {
        if (url.pathname === "/api/v1/admin/llm/model-profiles") {
          return jsonResponse({
            default_profile_id: visionProfile.id,
            items: [visionProfile],
          });
        }
        return null;
      }),
    );

    renderApp("/admin/models");

    const card = (
      await screen.findByRole("heading", { name: "多模态办公模型" })
    ).closest("li") as HTMLElement;
    expect(within(card).getByText("综合办公")).toBeInTheDocument();
    expect(within(card).getByText("仅内部使用")).toBeInTheDocument();
    expect(within(card).getByText("识图")).toBeInTheDocument();
  });

  it("支持局部编辑模型配置：留空 API Key 保留原密文并处理 409 版本冲突", async () => {
    let profile = makeModelProfile({
      id: "model-editable",
      name: "日常模型",
      scenario_label: "日常办公",
      scenario_description: "旧场景说明",
      supports_vision: false,
      visible_to_users: true,
      version: 1,
    });
    let listCalls = 0;
    let patchCalls = 0;
    const patchBodies: Array<Record<string, unknown>> = [];

    vi.stubGlobal(
      "fetch",
      baseFetch((url, init) => {
        if (
          url.pathname === "/api/v1/admin/llm/model-profiles" &&
          (!init?.method || init.method === "GET")
        ) {
          listCalls += 1;
          if (listCalls >= 2 && patchCalls === 1) {
            profile = {
              ...profile,
              name: "日常模型 v2",
              version: 2,
            };
          }
          return jsonResponse({
            default_profile_id: profile.id,
            items: [profile],
          });
        }
        if (
          url.pathname ===
            `/api/v1/admin/llm/model-profiles/${profile.id}` &&
          init?.method === "PATCH"
        ) {
          patchCalls += 1;
          const body = JSON.parse(String(init.body)) as Record<string, unknown>;
          patchBodies.push(body);
          if (patchCalls === 1) {
            return jsonResponse(
              { detail: "model_profile_version_conflict" },
              409,
            );
          }
          profile = {
            ...profile,
            scenario_label: (body.scenario_label as string | null) ?? null,
            scenario_description:
              (body.scenario_description as string | null) ?? null,
            supports_vision: Boolean(body.supports_vision),
            visible_to_users: Boolean(body.visible_to_users),
            version: 3,
          };
          return jsonResponse(profile);
        }
        if (url.pathname === "/api/v1/admin/llm/compression-policy") {
          return jsonResponse(makeCompressionPolicy());
        }
        return null;
      }),
    );
    const user = userEvent.setup();

    renderApp("/admin/models");

    await user.click(
      await screen.findByRole("button", { name: "编辑配置：日常模型" }),
    );
    const dialog = screen.getByRole("dialog", { name: "编辑模型配置" });
    expect(within(dialog).getByLabelText(/^API Key/)).toHaveValue("");
    expect(
      within(dialog).getByText("本次提交的 expected_version：1"),
    ).toBeInTheDocument();

    await user.click(
      within(dialog).getByRole("button", { name: "保存修改" }),
    );
    expect(
      await within(dialog).findByText(
        "模型配置已被其他管理员修改，请刷新最新配置后重试。",
      ),
    ).toBeInTheDocument();
    expect(patchBodies[0]).not.toHaveProperty("api_key");
    expect(patchBodies[0].expected_version).toBe(1);

    await user.click(
      within(dialog).getByRole("button", { name: "刷新最新配置" }),
    );
    await waitFor(() => {
      expect(within(dialog).getByLabelText("版本名称")).toHaveValue(
        "日常模型 v2",
      );
      expect(
        within(dialog).getByText("本次提交的 expected_version：2"),
      ).toBeInTheDocument();
    });

    const labelInput = within(dialog).getByLabelText("场景标签（可选）");
    await user.clear(labelInput);
    await user.type(labelInput, "新版办公");
    const descInput = within(dialog).getByLabelText("场景说明（可选）");
    await user.clear(descInput);
    await user.click(
      within(dialog).getByRole("checkbox", { name: "支持识图 / 多模态" }),
    );
    await user.click(
      within(dialog).getByRole("button", { name: "保存修改" }),
    );

    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", { name: "编辑模型配置" }),
      ).not.toBeInTheDocument();
    });
    expect(patchBodies[1]).toMatchObject({
      expected_version: 2,
      scenario_label: "新版办公",
      scenario_description: null,
      supports_vision: true,
    });
    expect(patchBodies[1]).not.toHaveProperty("api_key");
    expect(await screen.findByText("新版办公")).toBeInTheDocument();
  });

  it("支持删除模型并展示 409 强引用冲突中文提示", async () => {
    const deletable = makeModelProfile({
      id: "model-deletable",
      name: "待清理模型",
      model: "atlas-old",
    });
    let profiles = [PRIMARY, deletable];
    let deleteCalls = 0;

    vi.stubGlobal(
      "fetch",
      baseFetch((url, init) => {
        if (
          url.pathname === "/api/v1/admin/llm/model-profiles" &&
          (!init?.method || init.method === "GET")
        ) {
          return jsonResponse({
            default_profile_id: PRIMARY.id,
            items: profiles,
          });
        }
        if (
          url.pathname ===
            `/api/v1/admin/llm/model-profiles/${deletable.id}` &&
          init?.method === "DELETE"
        ) {
          deleteCalls += 1;
          if (deleteCalls === 1) {
            return jsonResponse(
              { detail: "model_profile_in_use_by_subagent" },
              409,
            );
          }
          profiles = [PRIMARY];
          return new Response(null, { status: 204 });
        }
        if (url.pathname === "/api/v1/admin/llm/compression-policy") {
          return jsonResponse(makeCompressionPolicy());
        }
        return null;
      }),
    );
    const user = userEvent.setup();

    renderApp("/admin/models");

    await user.click(
      await screen.findByRole("button", { name: `删除模型：${deletable.name}` }),
    );
    const dialog = screen.getByRole("dialog", {
      name: `删除模型「${deletable.name}」`,
    });
    await user.click(within(dialog).getByRole("button", { name: "确认删除" }));
    expect(
      await within(dialog).findByText(
        "该模型仍被子智能体绑定，请先解除绑定后再删除。",
      ),
    ).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "确认删除" }));
    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", { name: `删除模型「${deletable.name}」` }),
      ).not.toBeInTheDocument();
    });
    expect(
      screen.queryByRole("heading", { name: deletable.name }),
    ).not.toBeInTheDocument();
  });
});
