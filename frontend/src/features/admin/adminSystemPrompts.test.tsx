import {
  act,
  fireEvent,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  ADMIN_FORBIDDEN_EVENT,
  AUTH_UNAUTHORIZED_EVENT,
} from "../../shared/api/client";
import { formatDateTime } from "../../shared/lib/format";
import {
  getRequestUrl,
  TEST_USER,
} from "../../test/fixtures";
import {
  makeSystemPromptSummary,
  makeSystemPromptVersion,
  TEST_ADMIN,
} from "../../test/adminFixtures";
import { jsonResponse, renderApp } from "../../test/testUtils";
import type {
  SystemPromptKind,
  SystemPromptVersion,
} from "./model";
import { adminKeys } from "./queryKeys";

function requestUrl(input: RequestInfo | URL): URL {
  return new URL(getRequestUrl(input), "http://localhost");
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

function historyPage(
  versions: SystemPromptVersion[],
  page = 1,
  total = versions.length,
) {
  return {
    items: versions.map(makeSystemPromptSummary),
    page,
    page_size: 10,
    total,
  };
}

function isCurrentPath(
  url: URL,
  kind: SystemPromptKind,
): boolean {
  return (
    url.pathname === `/api/v1/admin/system-prompts/${kind}` &&
    (!url.search || url.search === "")
  );
}

function isHistoryPath(
  url: URL,
  kind: SystemPromptKind,
): boolean {
  return (
    url.pathname ===
    `/api/v1/admin/system-prompts/${kind}/versions`
  );
}

function installSimplePromptFetch(main: SystemPromptVersion) {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async (input: RequestInfo | URL) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (isCurrentPath(url, "main")) {
          return jsonResponse(main);
        }
        if (isHistoryPath(url, "main")) {
          return jsonResponse(historyPage([main]));
        }
        throw new Error(`Unexpected request: GET ${url}`);
      },
    ),
  );
}

describe("管理员系统提示词页", () => {
  it("编辑完整正文并按 CAS 基线保存，成功后更新当前版本", async () => {
    const current = makeSystemPromptVersion({
      content: "原始主提示词\n第二行",
      id: "main-v1",
      kind: "main",
    });
    const saved = makeSystemPromptVersion({
      content: "新的主提示词\n保留换行",
      id: "main-v2",
      kind: "main",
    });
    let versions = [current];
    let saveBody: unknown;
    let saveCalls = 0;
    const saveGate = deferred<Response>();
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (isCurrentPath(url, "main")) {
          return jsonResponse(current);
        }
        if (isHistoryPath(url, "main") && init?.method === "POST") {
          saveCalls += 1;
          saveBody = JSON.parse(String(init.body));
          versions = [saved, ...versions];
          return saveGate.promise;
        }
        if (isHistoryPath(url, "main")) {
          return jsonResponse(historyPage(versions));
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/admin/system-prompts");

    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "系统提示词",
      }),
    ).toBeInTheDocument();
    const editor = await screen.findByLabelText(/完整正文/);
    expect(editor).toHaveValue(current.content);
    expect(
      screen.getByRole("button", { name: "保存并生效" }),
    ).toBeDisabled();
    expect(screen.getByText("主对话正文")).toBeInTheDocument();
    expect(screen.getByText(current.created_by ?? "")).toBeInTheDocument();

    fireEvent.change(editor, {
      target: { value: saved.content },
    });
    expect(
      screen.getByText("有未保存修改"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "保存并生效" }),
    ).toBeEnabled();

    await user.click(
      screen.getByRole("button", { name: "保存并生效" }),
    );
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "正在保存…" }),
      ).toBeDisabled();
    });
    await user.click(
      screen.getByRole("button", { name: "正在保存…" }),
    );
    expect(saveCalls).toBe(1);
    saveGate.resolve(jsonResponse(saved));

    expect(
      await screen.findByText(/已保存，新任务使用新版本/),
    ).toBeInTheDocument();
    expect(screen.getByLabelText(/完整正文/)).toHaveValue(
      saved.content,
    );
    expect(
      screen.getByRole("button", { name: "保存并生效" }),
    ).toBeDisabled();
    expect(screen.getByText(saved.id)).toBeInTheDocument();
    expect(saveBody).toEqual({
      content: saved.content,
      expected_current_version_id: current.id,
    });
  });

  it("离开弹窗打开后保存成功，保持不可撤销的成功语义", async () => {
    const current = makeSystemPromptVersion({
      content: "保存前的正文",
      id: "main-v1",
      kind: "main",
    });
    const saved = makeSystemPromptVersion({
      content: "延迟生效的正文",
      id: "main-v2",
      kind: "main",
    });
    const saveGate = deferred<Response>();
    let saveCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = requestUrl(input);
          if (url.pathname.endsWith("/auth/me")) {
            return jsonResponse(TEST_ADMIN);
          }
          if (isCurrentPath(url, "main")) {
            return jsonResponse(current);
          }
          if (isHistoryPath(url, "main") && init?.method === "POST") {
            saveCalls += 1;
            return saveGate.promise;
          }
          if (isHistoryPath(url, "main")) {
            return jsonResponse(historyPage([current]));
          }
          throw new Error(
            `Unexpected request: ${init?.method ?? "GET"} ${url}`,
          );
        },
      ),
    );
    const user = userEvent.setup();
    const app = renderApp("/admin/system-prompts");

    const editor = await screen.findByLabelText(/完整正文/);
    fireEvent.change(editor, {
      target: { value: saved.content },
    });
    await user.click(
      screen.getByRole("button", { name: "保存并生效" }),
    );
    expect(
      await screen.findByRole("button", { name: "正在保存…" }),
    ).toBeDisabled();
    expect(saveCalls).toBe(1);

    await user.click(screen.getByRole("button", { name: "账户菜单" }));
    await user.click(screen.getByRole("menuitem", { name: "个人页面" }));

    expect(
      screen.getByRole("heading", {
        name: "保存请求处理中，仍要离开？",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /保存请求已提交，离开不会撤销请求，配置仍可能生效/,
      ),
    ).toHaveTextContent(/请稍后返回此页并重新加载确认结果/);
    expect(
      screen.queryByRole("button", { name: "放弃并离开" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "留在此页" }),
    ).toBeInTheDocument();

    act(() => {
      saveGate.resolve(jsonResponse(saved));
    });

    expect(
      await screen.findByRole("heading", {
        name: "保存已完成，仍要离开？",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        new RegExp(`已保存，新任务会使用版本 ${saved.id}`),
      ),
    ).toHaveTextContent(/离开不会撤销保存结果/);
    expect(
      screen.queryByText(/当前正文有未保存修改/),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "放弃并离开" }),
    ).not.toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "仍然离开" }),
    );
    expect(
      await screen.findByRole("heading", { name: "个人资料" }),
    ).toBeInTheDocument();
    expect(app.getLocation().pathname).toBe("/profile");
  });

  it("离开弹窗打开后保存网络失败，仍保持结果待确认语义", async () => {
    const current = makeSystemPromptVersion({
      content: "保存前的正文",
      id: "main-v1",
      kind: "main",
    });
    const draftContent = "网络失败后仍需保留的正文";
    const saveGate = deferred<Response>();
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = requestUrl(input);
          if (url.pathname.endsWith("/auth/me")) {
            return jsonResponse(TEST_ADMIN);
          }
          if (isCurrentPath(url, "main")) {
            return jsonResponse(current);
          }
          if (isHistoryPath(url, "main") && init?.method === "POST") {
            return saveGate.promise;
          }
          if (isHistoryPath(url, "main")) {
            return jsonResponse(historyPage([current]));
          }
          throw new Error(
            `Unexpected request: ${init?.method ?? "GET"} ${url}`,
          );
        },
      ),
    );
    const user = userEvent.setup();
    const app = renderApp("/admin/system-prompts");

    const editor = await screen.findByLabelText(/完整正文/);
    fireEvent.change(editor, {
      target: { value: draftContent },
    });
    await user.click(
      screen.getByRole("button", { name: "保存并生效" }),
    );
    await screen.findByRole("button", { name: "正在保存…" });
    await user.click(screen.getByRole("button", { name: "账户菜单" }));
    await user.click(screen.getByRole("menuitem", { name: "个人页面" }));
    expect(
      screen.getByRole("heading", {
        name: "保存请求处理中，仍要离开？",
      }),
    ).toBeInTheDocument();

    act(() => {
      saveGate.reject(new TypeError("Failed to fetch"));
    });

    expect(
      await screen.findByRole("heading", {
        name: "保存结果待确认，仍要离开？",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /保存请求已提交，离开不会撤销请求，配置仍可能生效/,
      ),
    ).toHaveTextContent(/请稍后返回此页并重新加载确认结果/);
    expect(
      screen.queryByRole("button", { name: "放弃并离开" }),
    ).not.toBeInTheDocument();
    expect(editor).toHaveValue(draftContent);
    expect(app.getLocation().pathname).toBe("/admin/system-prompts");

    await user.click(
      screen.getByRole("button", { name: "继续编辑" }),
    );
    expect(
      screen.queryByRole("heading", {
        name: "保存结果待确认，仍要离开？",
      }),
    ).not.toBeInTheDocument();
    expect(editor).toHaveValue(draftContent);
  });

  it("未知保存结果后继续编辑再离开，同时提示旧请求和新增草稿风险", async () => {
    const current = makeSystemPromptVersion({
      content: "保存前的正文",
      id: "main-v1",
      kind: "main",
    });
    const submittedContent = "结果未知的已提交正文";
    const laterDraft = `${submittedContent}\n之后新增但尚未提交的修改`;
    const saveGate = deferred<Response>();
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = requestUrl(input);
          if (url.pathname.endsWith("/auth/me")) {
            return jsonResponse(TEST_ADMIN);
          }
          if (isCurrentPath(url, "main")) {
            return jsonResponse(current);
          }
          if (isHistoryPath(url, "main") && init?.method === "POST") {
            return saveGate.promise;
          }
          if (isHistoryPath(url, "main")) {
            return jsonResponse(historyPage([current]));
          }
          throw new Error(
            `Unexpected request: ${init?.method ?? "GET"} ${url}`,
          );
        },
      ),
    );
    const user = userEvent.setup();
    const app = renderApp("/admin/system-prompts");

    const editor = await screen.findByLabelText(/完整正文/);
    fireEvent.change(editor, {
      target: { value: submittedContent },
    });
    await user.click(
      screen.getByRole("button", { name: "保存并生效" }),
    );
    await screen.findByRole("button", { name: "正在保存…" });
    await user.click(screen.getByRole("button", { name: "账户菜单" }));
    await user.click(screen.getByRole("menuitem", { name: "个人页面" }));

    act(() => {
      saveGate.reject(new TypeError("Failed to fetch"));
    });
    await screen.findByRole("heading", {
      name: "保存结果待确认，仍要离开？",
    });
    await user.click(
      screen.getByRole("button", { name: "继续编辑" }),
    );

    fireEvent.change(editor, {
      target: { value: laterDraft },
    });
    await user.click(screen.getByRole("button", { name: "账户菜单" }));
    await user.click(screen.getByRole("menuitem", { name: "个人页面" }));

    expect(
      screen.getByRole("heading", {
        name: "保存结果待确认，仍要离开？",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /旧请求仍可能生效。此后正文又产生了尚未提交的新修改，离开会丢失这些修改/,
      ),
    ).toHaveTextContent(/重新加载确认旧请求结果/);
    expect(
      screen.queryByText(/当前正文有未保存修改/),
    ).not.toBeInTheDocument();
    expect(app.getLocation().pathname).toBe("/admin/system-prompts");
  });

  it("未知保存结果重载核实后清空旧 attempt，新草稿离开只提示未保存", async () => {
    const baseline = makeSystemPromptVersion({
      content: "保存前的正文",
      id: "main-v1",
      kind: "main",
    });
    const verified = makeSystemPromptVersion({
      content: "重新加载核实后的服务端正文",
      id: "main-v2",
      kind: "main",
    });
    const submittedContent = "网络结果未知的正文";
    const laterDraft = "重载核实之后的新草稿";
    const saveGate = deferred<Response>();
    let currentReads = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = requestUrl(input);
          if (url.pathname.endsWith("/auth/me")) {
            return jsonResponse(TEST_ADMIN);
          }
          if (isCurrentPath(url, "main")) {
            currentReads += 1;
            return jsonResponse(currentReads === 1 ? baseline : verified);
          }
          if (isHistoryPath(url, "main") && init?.method === "POST") {
            return saveGate.promise;
          }
          if (isHistoryPath(url, "main")) {
            return jsonResponse(historyPage([baseline, verified]));
          }
          throw new Error(
            `Unexpected request: ${init?.method ?? "GET"} ${url}`,
          );
        },
      ),
    );
    const user = userEvent.setup();
    const app = renderApp("/admin/system-prompts");

    const editor = await screen.findByLabelText(/完整正文/);
    fireEvent.change(editor, {
      target: { value: submittedContent },
    });
    await user.click(
      screen.getByRole("button", { name: "保存并生效" }),
    );
    await screen.findByRole("button", { name: "正在保存…" });
    await user.click(screen.getByRole("button", { name: "账户菜单" }));
    await user.click(screen.getByRole("menuitem", { name: "个人页面" }));
    act(() => {
      saveGate.reject(new TypeError("Failed to fetch"));
    });
    await screen.findByRole("heading", {
      name: "保存结果待确认，仍要离开？",
    });
    await user.click(
      screen.getByRole("button", { name: "继续编辑" }),
    );

    await user.click(
      screen.getByRole("button", {
        name: "放弃修改并重新加载",
      }),
    );
    await user.click(
      screen.getByRole("button", { name: "替换并重新加载" }),
    );
    await waitFor(() => {
      expect(editor).toHaveValue(verified.content);
    });

    fireEvent.change(editor, {
      target: { value: laterDraft },
    });
    await user.click(screen.getByRole("button", { name: "账户菜单" }));
    await user.click(screen.getByRole("menuitem", { name: "个人页面" }));

    expect(
      screen.getByRole("heading", {
        name: "离开系统提示词页面？",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/当前正文有未保存修改/),
    ).toHaveTextContent(/需要保留时可先复制/);
    expect(
      screen.queryByText(/旧请求仍可能生效/),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "放弃并离开" }),
    ).toBeInTheDocument();
    expect(app.getLocation().pathname).toBe("/admin/system-prompts");
  });

  it("409 冲突保留输入，只有明确确认后才用服务端版本替换本地基线", async () => {
    const first = makeSystemPromptVersion({
      content: "管理员 A 读取的正文",
      id: "main-v1",
      kind: "main",
    });
    const latest = makeSystemPromptVersion({
      content: "管理员 B 已保存的正文",
      id: "main-v2",
      kind: "main",
      created_by: "admin-b",
    });
    let currentReads = 0;
    let saveCalls = 0;
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (isCurrentPath(url, "main")) {
          currentReads += 1;
          return jsonResponse(currentReads === 1 ? first : latest);
        }
        if (isHistoryPath(url, "main") && init?.method === "POST") {
          saveCalls += 1;
          return jsonResponse(
            { detail: "system_prompt_version_conflict" },
            409,
          );
        }
        if (isHistoryPath(url, "main")) {
          return jsonResponse(
            historyPage(currentReads === 1 ? [first] : [latest, first]),
          );
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/admin/system-prompts");

    const editor = await screen.findByLabelText(/完整正文/);
    fireEvent.change(editor, {
      target: { value: "必须保留的并发草稿" },
    });
    await user.click(
      screen.getByRole("button", { name: "保存并生效" }),
    );

    expect(
      await screen.findByText(/当前配置已被其他管理员修改/),
    ).toBeInTheDocument();
    expect(editor).toHaveValue("必须保留的并发草稿");
    expect(saveCalls).toBe(1);

    await user.click(
      screen.getByRole("button", {
        name: "放弃修改并重新加载",
      }),
    );
    expect(
      screen.getByRole("heading", {
        name: "重新载入当前版本？",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/重新加载会用服务端正文替换当前本地输入/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "复制当前输入" }),
    ).toBeInTheDocument();
    expect(editor).toHaveValue("必须保留的并发草稿");

    await user.click(
      screen.getByRole("button", { name: "替换并重新加载" }),
    );

    await waitFor(() => {
      expect(editor).toHaveValue(latest.content);
    });
    expect(
      screen.getByText(/已重新载入服务端当前版本/),
    ).toBeInTheDocument();
    expect(saveCalls).toBe(1);
  });

  it("编辑基线 A 保留草稿时后台读到版本 B，仍展示 A 的完整元数据", async () => {
    const baseline = makeSystemPromptVersion({
      content: "A 管理员加载的正文",
      created_at: "2026-11-11T01:00:00Z",
      created_by: "管理员 A",
      id: "main-v1",
      kind: "main",
    });
    const latest = makeSystemPromptVersion({
      content: "B 管理员保存的正文",
      created_at: "2026-11-12T02:30:00Z",
      created_by: "管理员 B",
      id: "main-v2",
      kind: "main",
    });
    let currentReads = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = requestUrl(input);
          if (url.pathname.endsWith("/auth/me")) {
            return jsonResponse(TEST_ADMIN);
          }
          if (isCurrentPath(url, "main")) {
            currentReads += 1;
            return jsonResponse(currentReads === 1 ? baseline : latest);
          }
          if (isHistoryPath(url, "main")) {
            return jsonResponse(historyPage([baseline]));
          }
          throw new Error(
            `Unexpected request: ${init?.method ?? "GET"} ${url}`,
          );
        },
      ),
    );
    const app = renderApp("/admin/system-prompts");

    const editor = await screen.findByLabelText(/完整正文/);
    fireEvent.change(editor, {
      target: { value: "尚未保存的本地草稿" },
    });

    await act(async () => {
      await app.client.refetchQueries({
        exact: true,
        queryKey: adminKeys.systemPrompt("main"),
      });
    });
    expect(currentReads).toBe(2);
    expect(
      await screen.findByText(/服务端当前版本已在编辑期间变化/),
    ).toBeInTheDocument();

    expect(
      screen.getByText(
        (_content, element) =>
          element?.classList.contains("admin-status-badge") === true &&
          element.textContent.includes("编辑基线 main-v1"),
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("管理员 A")).toBeInTheDocument();
    expect(
      screen.getByText(formatDateTime(baseline.created_at)),
    ).toBeInTheDocument();
    expect(screen.queryByText("管理员 B")).not.toBeInTheDocument();
    expect(
      screen.queryByText(formatDateTime(latest.created_at)),
    ).not.toBeInTheDocument();
    expect(editor).toHaveValue("尚未保存的本地草稿");
    expect(
      screen.getByText(
        new RegExp(`服务端当前版本为 ${latest.id}`),
      ),
    ).toBeInTheDocument();
  });

  it("历史列表按服务端 total 分页，可查看完整正文并恢复为新版本", async () => {
    const sharedContent = "历史正文\n保留原始换行";
    const current = makeSystemPromptVersion({
      content: sharedContent,
      id: "main-v3",
      kind: "main",
    });
    const old = makeSystemPromptVersion({
      content: sharedContent,
      id: "main-v1",
      kind: "main",
    });
    const restored = makeSystemPromptVersion({
      content: old.content,
      id: "main-v4",
      kind: "main",
      restored_from_version_id: old.id,
    });
    let restoreBody: unknown;
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_ADMIN);
        }
        if (isCurrentPath(url, "main")) {
          return jsonResponse(current);
        }
        if (
          url.pathname ===
            `/api/v1/admin/system-prompts/main/versions/${old.id}`
        ) {
          return jsonResponse(old);
        }
        if (
          url.pathname ===
            "/api/v1/admin/system-prompts/main/restore" &&
          init?.method === "POST"
        ) {
          restoreBody = JSON.parse(String(init.body));
          return jsonResponse(restored);
        }
        if (isHistoryPath(url, "main")) {
          const page = Number(url.searchParams.get("page") ?? 1);
          return jsonResponse(
            historyPage(page === 1 ? [current] : [old], page, 21),
          );
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    const app = renderApp("/admin/system-prompts");

    expect(await screen.findByLabelText(/完整正文/)).toHaveValue(
      current.content,
    );
    expect(screen.getByText("共 21 个版本")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "下一页" }));

    expect(await screen.findByText(old.id)).toBeInTheDocument();
    expect(app.getLocation().pathname).toBe("/admin/system-prompts");
    await user.click(
      screen.getByRole("button", {
        name: `查看版本 ${old.id}`,
      }),
    );

    expect(
      await screen.findByText(
        (_content, element) =>
          element?.tagName === "PRE" &&
          element.textContent === old.content,
      ),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "恢复此版本" }),
    );
    expect(
      screen.getByRole("heading", {
        name: "恢复这个历史版本？",
      }),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "恢复并生效" }),
    );

    expect(
      await screen.findByText(/已恢复。旧内容已生成新版本/),
    ).toBeInTheDocument();
    expect(screen.getByLabelText(/完整正文/)).toHaveValue(
      old.content,
    );
    expect(restoreBody).toEqual({
      expected_current_version_id: current.id,
      source_version_id: old.id,
    });
  });

  it("恢复响应不确定且存在草稿时，保留草稿并可直接重读当前版本与历史核实", async () => {
    const current = makeSystemPromptVersion({
      content: "恢复前的当前正文",
      id: "main-v3",
      kind: "main",
    });
    const old = makeSystemPromptVersion({
      content: "待恢复的历史正文",
      id: "main-v1",
      kind: "main",
    });
    const restored = makeSystemPromptVersion({
      content: old.content,
      id: "main-v4",
      kind: "main",
      restored_from_version_id: old.id,
    });
    const draftContent = "恢复结果未知时仍必须保留的草稿";
    let currentReads = 0;
    let historyReads = 0;
    let restoreCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = requestUrl(input);
          if (url.pathname.endsWith("/auth/me")) {
            return jsonResponse(TEST_ADMIN);
          }
          if (isCurrentPath(url, "main")) {
            currentReads += 1;
            return jsonResponse(
              currentReads === 1 ? current : restored,
            );
          }
          if (
            url.pathname ===
            `/api/v1/admin/system-prompts/main/versions/${old.id}`
          ) {
            return jsonResponse(old);
          }
          if (
            url.pathname ===
              "/api/v1/admin/system-prompts/main/restore" &&
            init?.method === "POST"
          ) {
            restoreCalls += 1;
            throw new TypeError("Failed to fetch");
          }
          if (isHistoryPath(url, "main")) {
            historyReads += 1;
            return jsonResponse(
              historyPage(
                historyReads === 1
                  ? [current, old]
                  : [restored, current, old],
              ),
            );
          }
          throw new Error(
            `Unexpected request: ${init?.method ?? "GET"} ${url}`,
          );
        },
      ),
    );
    const user = userEvent.setup();

    renderApp("/admin/system-prompts");

    const editor = await screen.findByLabelText(/完整正文/);
    fireEvent.change(editor, {
      target: { value: draftContent },
    });
    await user.click(
      screen.getByRole("button", { name: `查看版本 ${old.id}` }),
    );
    expect(
      await screen.findByText(
        (_content, element) =>
          element?.tagName === "PRE" &&
          element.textContent === old.content,
      ),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "恢复此版本" }),
    );
    await user.click(
      screen.getByRole("button", { name: "恢复并生效" }),
    );

    expect(
      await screen.findByText(/本次恢复.*结果不确定/),
    ).toHaveTextContent(/来源版本 main-v1/);
    expect(editor).toHaveValue(draftContent);
    expect(screen.getAllByText(old.id).length).toBeGreaterThan(0);
    expect(restoreCalls).toBe(1);

    const laterDraft = `${draftContent}\n结果未知后继续编辑`;
    fireEvent.change(editor, {
      target: { value: laterDraft },
    });
    expect(
      screen.getByRole("button", {
        name: "重新读取当前版本与历史",
      }),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", {
        name: "重新读取当前版本与历史",
      }),
    );

    expect(
      await screen.findByText(
        /已重新读取服务端当前版本与版本历史/,
      ),
    ).toHaveTextContent(/本地输入未被替换/);
    expect(currentReads).toBe(2);
    expect(historyReads).toBe(2);
    expect(restoreCalls).toBe(1);
    expect(editor).toHaveValue(laterDraft);
    expect(
      screen.getByText(
        new RegExp(`服务端当前版本为 ${restored.id}`),
      ),
    ).toBeInTheDocument();
  });

  it("恢复响应不确定且无草稿时，核实后自动采用服务端当前版本且不重复恢复", async () => {
    const current = makeSystemPromptVersion({
      content: "恢复前的当前正文",
      id: "main-v3",
      kind: "main",
    });
    const old = makeSystemPromptVersion({
      content: "待恢复的历史正文",
      id: "main-v1",
      kind: "main",
    });
    const restored = makeSystemPromptVersion({
      content: old.content,
      id: "main-v4",
      kind: "main",
      restored_from_version_id: old.id,
    });
    let currentReads = 0;
    let historyReads = 0;
    let restoreCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = requestUrl(input);
          if (url.pathname.endsWith("/auth/me")) {
            return jsonResponse(TEST_ADMIN);
          }
          if (isCurrentPath(url, "main")) {
            currentReads += 1;
            return jsonResponse(
              currentReads === 1 ? current : restored,
            );
          }
          if (
            url.pathname ===
            `/api/v1/admin/system-prompts/main/versions/${old.id}`
          ) {
            return jsonResponse(old);
          }
          if (
            url.pathname ===
              "/api/v1/admin/system-prompts/main/restore" &&
            init?.method === "POST"
          ) {
            restoreCalls += 1;
            throw new TypeError("Failed to fetch");
          }
          if (isHistoryPath(url, "main")) {
            historyReads += 1;
            return jsonResponse(
              historyPage(
                historyReads === 1
                  ? [current, old]
                  : [restored, current, old],
              ),
            );
          }
          throw new Error(
            `Unexpected request: ${init?.method ?? "GET"} ${url}`,
          );
        },
      ),
    );
    const user = userEvent.setup();

    renderApp("/admin/system-prompts");

    const editor = await screen.findByLabelText(/完整正文/);
    expect(editor).toHaveValue(current.content);
    await user.click(
      screen.getByRole("button", { name: `查看版本 ${old.id}` }),
    );
    await screen.findByText(
      (_content, element) =>
        element?.tagName === "PRE" &&
        element.textContent === old.content,
    );
    await user.click(
      screen.getByRole("button", { name: "恢复此版本" }),
    );
    await user.click(
      screen.getByRole("button", { name: "恢复并生效" }),
    );

    expect(
      await screen.findByText(/本次恢复.*结果不确定/),
    ).toBeInTheDocument();
    expect(editor).toHaveValue(current.content);
    expect(restoreCalls).toBe(1);

    await user.click(
      screen.getByRole("button", {
        name: "重新读取当前版本与历史",
      }),
    );

    expect(
      await screen.findByText(
        /已重新读取服务端当前版本与版本历史/,
      ),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(editor).toHaveValue(restored.content);
    });
    expect(currentReads).toBe(2);
    expect(historyReads).toBe(2);
    expect(restoreCalls).toBe(1);
    expect(
      screen.getByText(/与加载版本一致/),
    ).toBeInTheDocument();
    expect(screen.queryByText(restored.id)).toBeInTheDocument();
  });

  it("恢复明确返回冲突时标记为未生效，不误报结果不确定", async () => {
    const current = makeSystemPromptVersion({
      content: "恢复前的当前正文",
      id: "main-v3",
      kind: "main",
    });
    const old = makeSystemPromptVersion({
      content: "待恢复的历史正文",
      id: "main-v1",
      kind: "main",
    });
    let restoreCalls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = requestUrl(input);
          if (url.pathname.endsWith("/auth/me")) {
            return jsonResponse(TEST_ADMIN);
          }
          if (isCurrentPath(url, "main")) {
            return jsonResponse(current);
          }
          if (
            url.pathname ===
            `/api/v1/admin/system-prompts/main/versions/${old.id}`
          ) {
            return jsonResponse(old);
          }
          if (
            url.pathname ===
              "/api/v1/admin/system-prompts/main/restore" &&
            init?.method === "POST"
          ) {
            restoreCalls += 1;
            return jsonResponse(
              { detail: "system_prompt_version_conflict" },
              409,
            );
          }
          if (isHistoryPath(url, "main")) {
            return jsonResponse(historyPage([current, old]));
          }
          throw new Error(
            `Unexpected request: ${init?.method ?? "GET"} ${url}`,
          );
        },
      ),
    );
    const user = userEvent.setup();

    renderApp("/admin/system-prompts");

    const editor = await screen.findByLabelText(/完整正文/);
    fireEvent.change(editor, {
      target: { value: "冲突后仍要保留的草稿" },
    });
    await user.click(
      screen.getByRole("button", { name: `查看版本 ${old.id}` }),
    );
    await screen.findByText(
      (_content, element) =>
        element?.tagName === "PRE" &&
        element.textContent === old.content,
    );
    await user.click(
      screen.getByRole("button", { name: "恢复此版本" }),
    );
    await user.click(
      screen.getByRole("button", { name: "恢复并生效" }),
    );

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("本次恢复");
    expect(alert).toHaveTextContent("未生效");
    expect(alert).not.toHaveTextContent("结果不确定");
    expect(editor).toHaveValue("冲突后仍要保留的草稿");
    expect(restoreCalls).toBe(1);
  });

  it("未保存正文切换类别时要求明确放弃，并可继续编辑", async () => {
    const main = makeSystemPromptVersion({
      content: "主对话正文",
      id: "main-v1",
      kind: "main",
    });
    const compression = makeSystemPromptVersion({
      content: "历史压缩正文",
      id: "compression-v1",
      kind: "compression",
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (input: RequestInfo | URL, init?: RequestInit) => {
          const url = requestUrl(input);
          if (url.pathname.endsWith("/auth/me")) {
            return jsonResponse(TEST_ADMIN);
          }
          if (isCurrentPath(url, "main")) {
            return jsonResponse(main);
          }
          if (isCurrentPath(url, "compression")) {
            return jsonResponse(compression);
          }
          if (isHistoryPath(url, "main")) {
            return jsonResponse(historyPage([main]));
          }
          if (isHistoryPath(url, "compression")) {
            return jsonResponse(historyPage([compression]));
          }
          throw new Error(
            `Unexpected request: ${init?.method ?? "GET"} ${url}`,
          );
        },
      ),
    );
    const user = userEvent.setup();

    renderApp("/admin/system-prompts");

    const editor = await screen.findByLabelText(/完整正文/);
    fireEvent.change(editor, {
      target: { value: "尚未保存的主对话修改" },
    });
    await user.click(
      screen.getByRole("tab", { name: /历史压缩/ }),
    );

    expect(
      screen.getByRole("heading", {
        name: "切换到“历史压缩”？",
      }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "继续编辑" }));
    expect(editor).toHaveValue("尚未保存的主对话修改");
    expect(
      screen.getByRole("tab", { name: /主对话/ }),
    ).toHaveAttribute("aria-selected", "true");

    await user.click(
      screen.getByRole("tab", { name: /历史压缩/ }),
    );
    await user.click(
      screen.getByRole("button", { name: "放弃修改并切换" }),
    );

    expect(
      await screen.findByDisplayValue(compression.content),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: /历史压缩/ }),
    ).toHaveAttribute("aria-selected", "true");
  });

  it("存在未保存正文时拦截站内离开，确认后才执行导航", async () => {
    const main = makeSystemPromptVersion({
      content: "主对话正文",
      id: "main-v1",
      kind: "main",
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (input: RequestInfo | URL) => {
          const url = requestUrl(input);
          if (url.pathname.endsWith("/auth/me")) {
            return jsonResponse(TEST_ADMIN);
          }
          if (isCurrentPath(url, "main")) {
            return jsonResponse(main);
          }
          if (isHistoryPath(url, "main")) {
            return jsonResponse(historyPage([main]));
          }
          throw new Error(`Unexpected request: GET ${url}`);
        },
      ),
    );
    const user = userEvent.setup();
    const app = renderApp("/admin/system-prompts");

    const editor = await screen.findByLabelText(/完整正文/);
    fireEvent.change(editor, {
      target: { value: "离开前必须确认的草稿" },
    });
    await user.click(screen.getByRole("button", { name: "账户菜单" }));
    await user.click(screen.getByRole("menuitem", { name: "个人页面" }));

    expect(
      screen.getByRole("heading", {
        name: "离开系统提示词页面？",
      }),
    ).toBeInTheDocument();
    expect(app.getLocation().pathname).toBe("/admin/system-prompts");
    await user.click(
      screen.getByRole("button", { name: "放弃并离开" }),
    );

    expect(
      await screen.findByRole("heading", { name: "个人资料" }),
    ).toBeInTheDocument();
    expect(app.getLocation().pathname).toBe("/profile");
  });

  it("编程式 navigate 取消时保留草稿，确认后才离开", async () => {
    const main = makeSystemPromptVersion({
      content: "主对话正文",
      id: "main-v1",
      kind: "main",
    });
    installSimplePromptFetch(main);
    const user = userEvent.setup();
    const app = renderApp("/admin/system-prompts");

    const editor = await screen.findByLabelText(/完整正文/);
    fireEvent.change(editor, {
      target: { value: "编程式导航前的草稿" },
    });

    act(() => {
      void app.navigateTo("/profile");
    });
    expect(
      await screen.findByRole("heading", {
        name: "离开系统提示词页面？",
      }),
    ).toBeInTheDocument();
    expect(app.getLocation().pathname).toBe("/admin/system-prompts");
    await user.click(screen.getByRole("button", { name: "继续编辑" }));

    expect(app.getLocation().pathname).toBe("/admin/system-prompts");
    expect(editor).toHaveValue("编程式导航前的草稿");

    act(() => {
      void app.navigateTo("/profile");
    });
    await screen.findByRole("heading", {
      name: "离开系统提示词页面？",
    });
    await user.click(
      screen.getByRole("button", { name: "放弃并离开" }),
    );
    expect(
      await screen.findByRole("heading", { name: "个人资料" }),
    ).toBeInTheDocument();
    expect(app.getLocation().pathname).toBe("/profile");
  });

  it("浏览器后退被取消时保留草稿，再次确认后才后退", async () => {
    const main = makeSystemPromptVersion({
      content: "主对话正文",
      id: "main-v1",
      kind: "main",
    });
    installSimplePromptFetch(main);
    const user = userEvent.setup();
    const app = renderApp(
      ["/profile", "/admin/system-prompts"],
      { initialIndex: 1 },
    );

    const editor = await screen.findByLabelText(/完整正文/);
    fireEvent.change(editor, {
      target: { value: "后退前必须确认的草稿" },
    });
    act(() => {
      void app.goBack();
    });
    expect(
      await screen.findByRole("heading", {
        name: "离开系统提示词页面？",
      }),
    ).toBeInTheDocument();
    expect(app.getLocation().pathname).toBe("/admin/system-prompts");
    await user.click(screen.getByRole("button", { name: "继续编辑" }));
    expect(editor).toHaveValue("后退前必须确认的草稿");

    act(() => {
      void app.goBack();
    });
    await screen.findByRole("heading", {
      name: "离开系统提示词页面？",
    });
    await user.click(
      screen.getByRole("button", { name: "放弃并离开" }),
    );
    expect(
      await screen.findByRole("heading", { name: "个人资料" }),
    ).toBeInTheDocument();
    expect(app.getLocation().pathname).toBe("/profile");
  });

  it("浏览器前进被取消时保留草稿，再次确认后才前进", async () => {
    const main = makeSystemPromptVersion({
      content: "主对话正文",
      id: "main-v1",
      kind: "main",
    });
    installSimplePromptFetch(main);
    const user = userEvent.setup();
    const app = renderApp(
      ["/admin/system-prompts", "/profile"],
      { initialIndex: 0 },
    );

    const editor = await screen.findByLabelText(/完整正文/);
    fireEvent.change(editor, {
      target: { value: "前进前必须确认的草稿" },
    });
    act(() => {
      void app.goForward();
    });
    expect(
      await screen.findByRole("heading", {
        name: "离开系统提示词页面？",
      }),
    ).toBeInTheDocument();
    expect(app.getLocation().pathname).toBe("/admin/system-prompts");
    await user.click(screen.getByRole("button", { name: "继续编辑" }));
    expect(editor).toHaveValue("前进前必须确认的草稿");

    act(() => {
      void app.goForward();
    });
    await screen.findByRole("heading", {
      name: "离开系统提示词页面？",
    });
    await user.click(
      screen.getByRole("button", { name: "放弃并离开" }),
    );
    expect(
      await screen.findByRole("heading", { name: "个人资料" }),
    ).toBeInTheDocument();
    expect(app.getLocation().pathname).toBe("/profile");
  });

  it("dirty 状态收到 401 时强制进入登录页且不弹离开确认", async () => {
    const main = makeSystemPromptVersion({
      content: "主对话正文",
      id: "main-v1",
      kind: "main",
    });
    installSimplePromptFetch(main);
    const app = renderApp("/admin/system-prompts");

    const editor = await screen.findByLabelText(/完整正文/);
    fireEvent.change(editor, {
      target: { value: "身份失效前不能阻塞的草稿" },
    });

    act(() => {
      window.dispatchEvent(new CustomEvent(AUTH_UNAUTHORIZED_EVENT));
    });

    expect(
      await screen.findByRole("heading", { name: "欢迎回来" }),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(app.getLocation().pathname).toBe("/login");
    });
    expect(
      screen.queryByRole("heading", {
        name: "离开系统提示词页面？",
      }),
    ).not.toBeInTheDocument();
  });

  it("dirty 状态管理员撤权后立即离开管理页且不被 blocker 留住", async () => {
    const main = makeSystemPromptVersion({
      content: "主对话正文",
      id: "main-v1",
      kind: "main",
    });
    let authReads = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (input: RequestInfo | URL) => {
          const url = requestUrl(input);
          if (url.pathname.endsWith("/auth/me")) {
            authReads += 1;
            return jsonResponse(authReads === 1 ? TEST_ADMIN : TEST_USER);
          }
          if (isCurrentPath(url, "main")) {
            return jsonResponse(main);
          }
          if (isHistoryPath(url, "main")) {
            return jsonResponse(historyPage([main]));
          }
          throw new Error(`Unexpected request: GET ${url}`);
        },
      ),
    );
    renderApp("/admin/system-prompts");

    const editor = await screen.findByLabelText(/完整正文/);
    fireEvent.change(editor, {
      target: { value: "撤权前不能阻塞的草稿" },
    });

    act(() => {
      window.dispatchEvent(new CustomEvent(ADMIN_FORBIDDEN_EVENT));
    });

    expect(
      await screen.findByRole("heading", {
        name: "当前账号没有管理员权限",
      }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", {
        name: "离开系统提示词页面？",
      }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "系统提示词" }),
    ).not.toBeInTheDocument();
  });

  it("普通用户不能进入系统提示词管理页", async () => {
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        throw new Error(`Unexpected request: GET ${url}`);
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    renderApp("/admin/system-prompts");

    expect(
      await screen.findByRole("heading", {
        name: "当前账号没有管理员权限",
      }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "系统提示词" }),
    ).not.toBeInTheDocument();
    expect(
      fetchMock.mock.calls.every(([input]) =>
        requestUrl(input).pathname.endsWith("/auth/me"),
      ),
    ).toBe(true);
  });
});
