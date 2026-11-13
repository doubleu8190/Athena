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
  it("设默认只提交符合条件的版本，失败提示后重试成功", async () => {
    const eligible = makeModelProfile({
      id: "model-eligible",
      name: "候选模型",
    });
    const missingKey = makeModelProfile({
      has_api_key: false,
      id: "model-missing-key",
      name: "待重配模型",
    });
    let defaultCalls = 0;
    const fetchMock = baseFetch((url, init) => {
      if (
        url.pathname === "/api/v1/admin/llm/model-profiles" &&
        (!init?.method || init.method === "GET")
      ) {
        return jsonResponse({
          default_profile_id: PRIMARY.id,
          items: [PRIMARY, eligible, missingKey],
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
            prompt_template_version: null,
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
    const missingKeyCard = screen
      .getAllByRole("heading", { name: missingKey.name })
      .map((heading) => heading.closest("li"))
      .find((element) => element !== null) as HTMLElement;
    expect(
      within(missingKeyCard).getByRole("button", { name: "设为默认" }),
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
    expect(await screen.findByText("后端返回 3 个版本")).toBeInTheDocument();
    expect(screen.getAllByText(PRIMARY.base_url)).toHaveLength(3);
    expect(
      screen.getAllByText("已配置 API Key"),
    ).toHaveLength(3);
    expect(screen.getByText("当前默认")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /编辑模型|删除模型/ }),
    ).not.toBeInTheDocument();

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
      .find((heading) => heading.closest("li.admin-model-card"))
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
            prompt_template_version: null,
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
    await user.click(
      within(dialog).getByRole("checkbox", { name: "支持工具调用" }),
    );
    expect(
      within(dialog).getByRole("checkbox", {
        name: "支持并行工具调用",
      }),
    ).toBeDisabled();
    await user.click(
      within(dialog).getByRole("button", {
        name: "创建不可变版本",
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
        name: "创建不可变版本",
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
        name: "创建不可变版本",
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
      supports_parallel_tool_calls: false,
      supports_tools: true,
    });
  });

  it("压缩策略只列出已启用模型，校验五字段并保存真实请求", async () => {
    const unconfigured = makeCompressionPolicy({
      compression_model_profile_id: null,
      created_at: null,
      id: null,
      prompt_template_version: null,
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
    const modelSelect = within(dialog).getByLabelText(/^压缩模型/);
    expect(
      within(modelSelect).getByRole("option", {
        name: new RegExp(PRIMARY.name),
      }),
    ).toBeInTheDocument();
    expect(
      within(modelSelect).queryByRole("option", {
        name: new RegExp(DISABLED.name),
      }),
    ).not.toBeInTheDocument();
    await user.selectOptions(modelSelect, PRIMARY.id);

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
});
