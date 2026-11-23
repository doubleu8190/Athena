import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  getRequestUrl,
  knowledgeListResponse,
  KNOWLEDGE_BASE_A,
  makeKnowledgeDocument,
  makeKnowledgeHit,
  makeKnowledgeVersion,
  TEST_USER,
} from "../../test/fixtures";
import { jsonResponse, renderApp } from "../../test/testUtils";

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

function createFetchMock(
  handler: (
    url: URL,
    init?: RequestInit,
  ) => Response | Promise<Response>,
) {
  return vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = requestUrl(input);
      if (url.pathname.endsWith("/auth/me")) {
        return jsonResponse(TEST_USER);
      }
      if (
        url.pathname === "/api/v1/knowledge-bases" &&
        (!init?.method || init.method === "GET")
      ) {
        const pageSize = Number(
          url.searchParams.get("page_size") ?? 20,
        );
        return jsonResponse(
          knowledgeListResponse([KNOWLEDGE_BASE_A], 1, pageSize),
        );
      }
      return handler(url, init);
    },
  );
}

describe("知识库文档状态", () => {
  it("从版本接口展示真实状态、片段数与处理中限制", async () => {
    const readyDocument = makeKnowledgeDocument({
      active_version_id: "version-ready",
      id: "document-ready",
      title: "已就绪文档",
    });
    const pendingDocument = makeKnowledgeDocument({
      active_version_id: "version-old",
      id: "document-pending",
      pending_version_id: "version-queued",
      title: "处理中文档",
    });
    const fetchMock = createFetchMock((url, init) => {
      if (url.pathname === `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}`) {
        return jsonResponse(KNOWLEDGE_BASE_A);
      }
      if (
        url.pathname ===
        `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}/documents`
      ) {
        return jsonResponse(
          knowledgeListResponse(
            [readyDocument, pendingDocument],
            1,
            10,
          ),
        );
      }
      if (
        url.pathname.endsWith(
          "/documents/document-ready/versions",
        )
      ) {
        return jsonResponse([
          makeKnowledgeVersion({
            chunk_count: 12,
            document_id: readyDocument.id,
            id: "version-ready",
          }),
        ]);
      }
      if (
        url.pathname.endsWith(
          "/documents/document-pending/versions",
        )
      ) {
        return jsonResponse([
          makeKnowledgeVersion({
            chunk_count: 0,
            completed_at: null,
            document_id: pendingDocument.id,
            id: "version-queued",
            state: "queued",
          }),
        ]);
      }
      throw new Error(
        `Unexpected request: ${init?.method ?? "GET"} ${url}`,
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    renderApp(`/knowledge/${KNOWLEDGE_BASE_A.id}`);

    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: KNOWLEDGE_BASE_A.name,
      }),
    ).toBeInTheDocument();
    expect(await screen.findByText("已就绪文档")).toBeInTheDocument();
    expect(await screen.findByText("处理中文档")).toBeInTheDocument();
    expect(await screen.findByText("已就绪")).toBeInTheDocument();
    expect(screen.getByText("12 个检索片段")).toBeInTheDocument();
    expect(await screen.findByText("排队中")).toBeInTheDocument();
    expect(
      screen.getByText("当前仍使用上一已就绪版本。"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: "添加新版本：处理中文档",
      }),
    ).toBeDisabled();
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
  });

  it(
    "排队中的版本会在轮询后更新为真实就绪状态",
    async () => {
      let versionCalls = 0;
      const document = makeKnowledgeDocument({
        active_version_id: "version-old",
        id: "document-polling",
        pending_version_id: "version-new",
        title: "轮询文档",
      });
      const fetchMock = createFetchMock((url, init) => {
        if (
          url.pathname ===
          `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}`
        ) {
          return jsonResponse(KNOWLEDGE_BASE_A);
        }
        if (
          url.pathname ===
          `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}/documents`
        ) {
          return jsonResponse(
            knowledgeListResponse([document], 1, 10),
          );
        }
        if (url.pathname.endsWith("/versions")) {
          versionCalls += 1;
          return jsonResponse([
            versionCalls === 1
              ? makeKnowledgeVersion({
                  chunk_count: 0,
                  completed_at: null,
                  document_id: document.id,
                  id: "version-new",
                  state: "queued",
                })
              : makeKnowledgeVersion({
                  chunk_count: 6,
                  document_id: document.id,
                  id: "version-new",
                }),
          ]);
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      });
      vi.stubGlobal("fetch", fetchMock);

      renderApp(`/knowledge/${KNOWLEDGE_BASE_A.id}`);

      expect(await screen.findByText("排队中")).toBeInTheDocument();
      await waitFor(
        () => {
          expect(screen.getByText("已就绪")).toBeInTheDocument();
          expect(screen.getByText("6 个检索片段")).toBeInTheDocument();
        },
        { timeout: 5_000 },
      );
      expect(versionCalls).toBeGreaterThanOrEqual(2);
    },
    8_000,
  );

  it("失败的最新版本可重试并回到排队状态", async () => {
    let retried = false;
    const document = makeKnowledgeDocument({
      active_version_id: null,
      id: "document-failed",
      pending_version_id: null,
      title: "失败文档",
    });
    const failedVersion = makeKnowledgeVersion({
      chunk_count: 0,
      completed_at: "2026-11-10T04:32:00Z",
      document_id: document.id,
      error_code: "knowledge_document_text_empty",
      id: "version-failed",
      state: "failed",
    });
    const fetchMock = createFetchMock((url, init) => {
      if (
        url.pathname ===
        `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}`
      ) {
        return jsonResponse(KNOWLEDGE_BASE_A);
      }
      if (
        url.pathname ===
        `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}/documents`
      ) {
        return jsonResponse(
          knowledgeListResponse([document], 1, 10),
        );
      }
      if (
        url.pathname.endsWith(
          "/documents/document-failed/versions/version-failed/retry",
        ) &&
        init?.method === "POST"
      ) {
        retried = true;
        return jsonResponse(
          { ...failedVersion, state: "queued" },
          202,
        );
      }
      if (url.pathname.endsWith("/documents/document-failed/versions")) {
        return jsonResponse([
          retried ? { ...failedVersion, state: "queued" } : failedVersion,
        ]);
      }
      throw new Error(
        `Unexpected request: ${init?.method ?? "GET"} ${url}`,
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp(`/knowledge/${KNOWLEDGE_BASE_A.id}`);

    expect(await screen.findByText("处理失败")).toBeInTheDocument();
    expect(
      screen.getByText(/没有提取到可检索文本/),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "重试失败版本" }),
    );

    expect(await screen.findByText("已重新排队")).toBeInTheDocument();
    expect(await screen.findByText("排队中")).toBeInTheDocument();
    expect(screen.getByText("处理完成后才可检索。")).toBeInTheDocument();
  });

  it(
    "列表返回 latest_version 摘要时渲染 5 篇文档仅发 1 次列表请求、0 次 /versions 请求，并在列表轮询与重试后刷新",
    async () => {
      let documentListCalls = 0;
      let versionCalls = 0;
      let retriedFailed = false;
      const docReady = makeKnowledgeDocument({
        active_version_id: "ver-ready",
        id: "doc-1",
        latest_version: makeKnowledgeVersion({
          chunk_count: 14,
          document_id: "doc-1",
          id: "ver-ready",
          state: "ready",
          warnings: ["ocr_fallback_used"],
        }),
        pending_version_id: null,
        title: "就绪文档 A",
      });
      const docQueued = makeKnowledgeDocument({
        active_version_id: "ver-q-old",
        id: "doc-2",
        latest_version: makeKnowledgeVersion({
          chunk_count: 0,
          completed_at: null,
          document_id: "doc-2",
          id: "ver-q-new",
          state: "queued",
        }),
        pending_version_id: "ver-q-new",
        title: "排队文档 B",
      });
      const docProcessing = makeKnowledgeDocument({
        active_version_id: null,
        id: "doc-3",
        latest_version: makeKnowledgeVersion({
          chunk_count: 0,
          completed_at: null,
          document_id: "doc-3",
          id: "ver-p-new",
          state: "processing",
        }),
        pending_version_id: "ver-p-new",
        title: "处理中文档 C",
      });
      const failedVersion = makeKnowledgeVersion({
        chunk_count: 0,
        completed_at: "2026-11-10T04:32:00Z",
        document_id: "doc-4",
        error_code: "knowledge_document_text_empty",
        id: "ver-failed",
        state: "failed",
      });
      const docFailed = makeKnowledgeDocument({
        active_version_id: null,
        id: "doc-4",
        latest_version: failedVersion,
        pending_version_id: null,
        title: "失败文档 D",
      });
      const docEmpty = makeKnowledgeDocument({
        active_version_id: null,
        id: "doc-5",
        latest_version: null,
        pending_version_id: null,
        title: "空版本文档 E",
      });

      const fetchMock = createFetchMock((url, init) => {
        if (
          url.pathname === `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}`
        ) {
          return jsonResponse(KNOWLEDGE_BASE_A);
        }
        if (
          url.pathname ===
          `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}/documents`
        ) {
          documentListCalls += 1;
          const polledReady = documentListCalls >= 2;
          return jsonResponse(
            knowledgeListResponse(
              [
                docReady,
                polledReady
                  ? {
                      ...docQueued,
                      active_version_id: "ver-q-new",
                      latest_version: makeKnowledgeVersion({
                        chunk_count: 9,
                        document_id: "doc-2",
                        id: "ver-q-new",
                        state: "ready",
                      }),
                      pending_version_id: null,
                    }
                  : docQueued,
                polledReady
                  ? {
                      ...docProcessing,
                      active_version_id: "ver-p-new",
                      latest_version: makeKnowledgeVersion({
                        chunk_count: 5,
                        document_id: "doc-3",
                        id: "ver-p-new",
                        state: "ready",
                      }),
                      pending_version_id: null,
                    }
                  : docProcessing,
                retriedFailed
                  ? {
                      ...docFailed,
                      latest_version: {
                        ...failedVersion,
                        completed_at: null,
                        error_code: null,
                        state: "queued",
                      },
                      pending_version_id: failedVersion.id,
                    }
                  : docFailed,
                docEmpty,
              ],
              1,
              10,
            ),
          );
        }
        if (
          url.pathname.endsWith(
            "/documents/doc-4/versions/ver-failed/retry",
          ) &&
          init?.method === "POST"
        ) {
          retriedFailed = true;
          return jsonResponse({ ...failedVersion, state: "queued" }, 202);
        }
        if (url.pathname.includes("/versions")) {
          versionCalls += 1;
          return jsonResponse([docReady.latest_version!]);
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      });
      vi.stubGlobal("fetch", fetchMock);
      const user = userEvent.setup();

      renderApp(`/knowledge/${KNOWLEDGE_BASE_A.id}`);

      expect(await screen.findByText("就绪文档 A")).toBeInTheDocument();
      expect(screen.getByText("排队文档 B")).toBeInTheDocument();
      expect(screen.getByText("处理中文档 C")).toBeInTheDocument();
      expect(screen.getByText("失败文档 D")).toBeInTheDocument();
      expect(screen.getByText("空版本文档 E")).toBeInTheDocument();

      // 首屏 5 篇文档全部由列表内联摘要渲染：仅 1 次列表请求、0 次 /versions 请求。
      expect(documentListCalls).toBe(1);
      expect(versionCalls).toBe(0);
      expect(screen.getByText(/14 个检索片段/)).toBeInTheDocument();
      expect(screen.getByText(/1 条警告/)).toBeInTheDocument();
      expect(screen.getByText("排队中")).toBeInTheDocument();
      expect(screen.getByText("处理中")).toBeInTheDocument();
      expect(screen.getByText("处理失败")).toBeInTheDocument();
      expect(screen.getByText("后端未返回版本记录。")).toBeInTheDocument();

      // 列表自动轮询在 3 秒后将排队/处理中文档更新为就绪态，期间仍保持 0 次 /versions 请求。
      await waitFor(
        () => {
          expect(screen.getByText("9 个检索片段")).toBeInTheDocument();
          expect(screen.getByText("5 个检索片段")).toBeInTheDocument();
        },
        { timeout: 5_000 },
      );
      expect(versionCalls).toBe(0);

      // 失败行点击重试后通过列表失效刷新状态。
      await user.click(
        screen.getByRole("button", { name: "重试失败版本" }),
      );
      expect(await screen.findByText("已重新排队")).toBeInTheDocument();
      await waitFor(() => {
        expect(screen.getByText("排队中")).toBeInTheDocument();
      });
      expect(versionCalls).toBe(0);

      // 显式打开「版本历史」弹窗时才按需拉取完整版本列表。
      await user.click(
        screen.getByRole("button", { name: "版本历史：就绪文档 A" }),
      );
      expect(
        await screen.findByRole("dialog", { name: "版本历史" }),
      ).toBeInTheDocument();
      await waitFor(() => expect(versionCalls).toBe(1));
    },
    10_000,
  );
});

describe("知识库版本替换", () => {
  it(
    "文本替换成功会刷新版本缓存并轮询新状态",
    async () => {
      let replaced = false;
      let replacementVersionCalls = 0;
      let latestState: "queued" | "processing" | "ready" =
        "queued";
      const document = makeKnowledgeDocument({
        active_version_id: "version-old",
        id: "document-text-replace",
        pending_version_id: null,
        title: "待替换文档",
      });
      const oldVersion = makeKnowledgeVersion({
        chunk_count: 3,
        document_id: document.id,
        id: "version-old",
        state: "ready",
      });
      const fetchMock = createFetchMock((url, init) => {
        if (
          url.pathname ===
          `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}`
        ) {
          return jsonResponse(KNOWLEDGE_BASE_A);
        }
        if (
          url.pathname ===
          `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}/documents`
        ) {
          return jsonResponse(
            knowledgeListResponse(
              [
                {
                  ...document,
                  active_version_id:
                    latestState === "ready"
                      ? "version-new"
                      : "version-old",
                  pending_version_id:
                    replaced && latestState !== "ready"
                      ? "version-new"
                      : null,
                },
              ],
              1,
              10,
            ),
          );
        }
        if (
          url.pathname.endsWith(
            `/documents/${document.id}/versions`,
          )
        ) {
          if (!replaced) {
            return jsonResponse([oldVersion]);
          }
          replacementVersionCalls += 1;
          if (replacementVersionCalls === 1) {
            latestState = "queued";
            return jsonResponse([
              makeKnowledgeVersion({
                chunk_count: 0,
                completed_at: null,
                document_id: document.id,
                id: "version-new",
                state: "queued",
              }),
            ]);
          }
          if (replacementVersionCalls === 2) {
            latestState = "processing";
            return jsonResponse([
              makeKnowledgeVersion({
                chunk_count: 0,
                completed_at: null,
                document_id: document.id,
                id: "version-new",
                state: "processing",
              }),
            ]);
          }
          latestState = "ready";
          return jsonResponse([
            makeKnowledgeVersion({
              chunk_count: 8,
              document_id: document.id,
              id: "version-new",
              state: "ready",
            }),
          ]);
        }
        if (
          url.pathname.endsWith(
            `/documents/${document.id}/versions/text`,
          ) &&
          init?.method === "POST"
        ) {
          replaced = true;
          return jsonResponse(
            {
              document_id: document.id,
              state: "queued",
              status_url: "/status",
              version_id: "version-new",
            },
            202,
          );
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      });
      vi.stubGlobal("fetch", fetchMock);
      const user = userEvent.setup();

      renderApp(`/knowledge/${KNOWLEDGE_BASE_A.id}`);

      expect(await screen.findByText("3 个检索片段")).toBeInTheDocument();
      await user.click(
        screen.getByRole("button", {
          name: "添加新版本：待替换文档",
        }),
      );
      const dialog = screen.getByRole("dialog", {
        name: "添加文档新版本",
      });
      await user.click(
        within(dialog).getByRole("button", { name: "粘贴文本" }),
      );
      await user.type(
        within(dialog).getByLabelText("文本内容"),
        "这是新的版本正文。",
      );
      await user.click(
        within(dialog).getByRole("button", {
          name: "提交新版本",
        }),
      );

      expect(
        await screen.findByText("排队中", {}, { timeout: 2_000 }),
      ).toBeInTheDocument();
      expect(
        await screen.findByText("处理中", {}, { timeout: 5_000 }),
      ).toBeInTheDocument();
      expect(
        await screen.findByText("8 个检索片段", {}, { timeout: 5_000 }),
      ).toBeInTheDocument();

      const replaceCall = fetchMock.mock.calls.find(
        ([input, init]) =>
          requestUrl(input).pathname.endsWith(
            `/documents/${document.id}/versions/text`,
          ) && init?.method === "POST",
      );
      expect(JSON.parse(String(replaceCall?.[1]?.body))).toEqual({
        text: "这是新的版本正文。",
        title: "待替换文档",
      });
      expect(replacementVersionCalls).toBeGreaterThanOrEqual(3);
    },
    15_000,
  );

  it(
    "文件替换失败后会刷新为新版本失败状态并保留旧活动版本说明",
    async () => {
      let replaced = false;
      let replacementVersionCalls = 0;
      let latestState: "processing" | "failed" = "processing";
      const document = makeKnowledgeDocument({
        active_version_id: "version-old",
        id: "document-file-replace",
        pending_version_id: null,
        title: "待替换文件文档",
      });
      const oldVersion = makeKnowledgeVersion({
        chunk_count: 0,
        completed_at: "2026-11-10T04:32:00Z",
        document_id: document.id,
        error_code: "knowledge_parser_transient",
        id: "version-old",
        state: "failed",
      });
      const fetchMock = createFetchMock((url, init) => {
        if (
          url.pathname ===
          `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}`
        ) {
          return jsonResponse(KNOWLEDGE_BASE_A);
        }
        if (
          url.pathname ===
          `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}/documents`
        ) {
          return jsonResponse(
            knowledgeListResponse(
              [
                {
                  ...document,
                  pending_version_id:
                    replaced && latestState === "processing"
                      ? "version-new"
                      : null,
                },
              ],
              1,
              10,
            ),
          );
        }
        if (
          url.pathname.endsWith(
            `/documents/${document.id}/versions`,
          )
        ) {
          if (!replaced) {
            return jsonResponse([oldVersion]);
          }
          replacementVersionCalls += 1;
          if (replacementVersionCalls === 1) {
            latestState = "processing";
            return jsonResponse([
              makeKnowledgeVersion({
                chunk_count: 0,
                completed_at: null,
                document_id: document.id,
                id: "version-new",
                state: "processing",
              }),
            ]);
          }
          latestState = "failed";
          return jsonResponse([
            makeKnowledgeVersion({
              chunk_count: 0,
              completed_at: "2026-11-10T04:35:00Z",
              document_id: document.id,
              error_code: "knowledge_document_text_empty",
              id: "version-new",
              state: "failed",
            }),
          ]);
        }
        if (
          url.pathname.endsWith(
            `/documents/${document.id}/versions/upload`,
          ) &&
          init?.method === "POST"
        ) {
          replaced = true;
          return jsonResponse(
            {
              document_id: document.id,
              state: "queued",
              status_url: "/status",
              version_id: "version-new",
            },
            202,
          );
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      });
      vi.stubGlobal("fetch", fetchMock);
      const user = userEvent.setup();

      renderApp(`/knowledge/${KNOWLEDGE_BASE_A.id}`);

      expect(
        await screen.findByText(/解析服务暂时不可用/),
      ).toBeInTheDocument();
      await user.click(
        screen.getByRole("button", {
          name: "添加新版本：待替换文件文档",
        }),
      );
      const dialog = screen.getByRole("dialog", {
        name: "添加文档新版本",
      });
      await user.upload(
        within(dialog).getByLabelText("新版本文件"),
        new File(["真实内容"], "修订版.txt", { type: "text/plain" }),
      );
      await user.click(
        within(dialog).getByRole("button", {
          name: "提交新版本",
        }),
      );

      expect(
        await screen.findByText("处理中", {}, { timeout: 2_000 }),
      ).toBeInTheDocument();
      expect(
        await screen.findByText(
          /没有提取到可检索文本/,
          {},
          { timeout: 5_000 },
        ),
      ).toBeInTheDocument();
      expect(
        screen.getByText(/当前检索仍使用上一已就绪版本/),
      ).toBeInTheDocument();
      expect(replacementVersionCalls).toBeGreaterThanOrEqual(2);
    },
    10_000,
  );
});

describe("知识库资料导入", () => {
  it("文本导入网络失败后保留表单并复用同一幂等键", async () => {
    let posted = false;
    let postAttempts = 0;
    const firstAttempt = deferred<Response>();
    const document = makeKnowledgeDocument({
      active_version_id: null,
      id: "document-new",
      pending_version_id: "version-new",
      title: "访谈摘要",
    });
    const fetchMock = createFetchMock((url, init) => {
      if (
        url.pathname ===
        `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}`
      ) {
        return jsonResponse(KNOWLEDGE_BASE_A);
      }
      if (
        url.pathname ===
        `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}/documents`
      ) {
        return jsonResponse(
          knowledgeListResponse(posted ? [document] : [], 1, 10),
        );
      }
      if (
        url.pathname ===
          `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}/documents/text` &&
        init?.method === "POST"
      ) {
        postAttempts += 1;
        if (postAttempts === 1) {
          return firstAttempt.promise;
        }
        posted = true;
        return jsonResponse(
          {
            document_id: document.id,
            state: "queued",
            status_url: "/status",
            version_id: "version-new",
          },
          202,
        );
      }
      if (url.pathname.endsWith("/documents/document-new/versions")) {
        return jsonResponse([
          makeKnowledgeVersion({
            chunk_count: 0,
            completed_at: null,
            document_id: document.id,
            id: "version-new",
            state: "queued",
          }),
        ]);
      }
      throw new Error(
        `Unexpected request: ${init?.method ?? "GET"} ${url}`,
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp(`/knowledge/${KNOWLEDGE_BASE_A.id}`);
    await screen.findByRole("heading", {
      level: 1,
      name: KNOWLEDGE_BASE_A.name,
    });
    const main = screen.getByRole("main");
    const addButtons = await within(main).findAllByRole("button", {
      name: "添加资料",
    });
    await user.click(addButtons[0]);
    const dialog = screen.getByRole("dialog", { name: "添加资料" });
    await user.click(
      within(dialog).getByRole("button", { name: "粘贴文本" }),
    );
    await user.type(
      within(dialog).getByLabelText("文档标题"),
      "访谈摘要",
    );
    await user.type(
      within(dialog).getByLabelText("文本内容"),
      "这是真实访谈文本。",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "添加资料" }),
    );

    await waitFor(() => expect(postAttempts).toBe(1));
    expect(
      within(dialog).getByLabelText("文档标题"),
    ).toBeDisabled();
    expect(
      within(dialog).getByLabelText("文本内容"),
    ).toBeDisabled();
    expect(
      within(dialog).getByRole("button", { name: "上传文件" }),
    ).toBeDisabled();
    expect(
      within(dialog).getByRole("button", { name: "粘贴文本" }),
    ).toBeDisabled();
    expect(
      within(dialog).getByRole("button", { name: "取消" }),
    ).toBeDisabled();
    expect(
      within(dialog).getByRole("button", { name: "正在提交资料…" }),
    ).toBeDisabled();

    firstAttempt.reject(new TypeError("Failed to fetch"));
    expect(
      await within(dialog).findByText(/无法连接服务器/),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByLabelText("文本内容"),
    ).toHaveValue("这是真实访谈文本。");
    expect(
      within(dialog).getByLabelText("文本内容"),
    ).toBeEnabled();
    await user.click(
      within(dialog).getByRole("button", { name: "添加资料" }),
    );

    expect(await screen.findByText("访谈摘要")).toBeInTheDocument();
    expect(
      screen.queryByRole("dialog", { name: "添加资料" }),
    ).not.toBeInTheDocument();
    const postCalls = fetchMock.mock.calls.filter(
      ([input, init]) =>
        requestUrl(input).pathname.endsWith("/documents/text") &&
        init?.method === "POST",
    );
    expect(postCalls).toHaveLength(2);
    const firstHeaders = new Headers(postCalls[0][1]?.headers);
    const secondHeaders = new Headers(postCalls[1][1]?.headers);
    expect(firstHeaders.get("Idempotency-Key")).toBeTruthy();
    expect(secondHeaders.get("Idempotency-Key")).toBe(
      firstHeaders.get("Idempotency-Key"),
    );
    expect(JSON.parse(String(postCalls[1][1]?.body))).toEqual({
      text: "这是真实访谈文本。",
      title: "访谈摘要",
    });
  });

  it("文件上传使用 FormData 且保留幂等键", async () => {
    const readyDocument = makeKnowledgeDocument({
      id: "document-ready",
      title: "现有文档",
    });
    let uploaded = false;
    const uploadResponse = deferred<Response>();
    const fetchMock = createFetchMock((url, init) => {
      if (
        url.pathname ===
        `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}`
      ) {
        return jsonResponse(KNOWLEDGE_BASE_A);
      }
      if (
        url.pathname ===
        `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}/documents`
      ) {
        return jsonResponse(
          knowledgeListResponse([readyDocument], 1, 10),
        );
      }
      if (url.pathname.endsWith("/documents/document-ready/versions")) {
        return jsonResponse([
          makeKnowledgeVersion({
            document_id: readyDocument.id,
            id: "version-1",
          }),
        ]);
      }
      if (
        url.pathname.endsWith("/documents/upload") &&
        init?.method === "POST"
      ) {
        uploaded = true;
        return uploadResponse.promise;
      }
      throw new Error(
        `Unexpected request: ${init?.method ?? "GET"} ${url}`,
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp(`/knowledge/${KNOWLEDGE_BASE_A.id}`);
    await screen.findByRole("heading", {
      level: 1,
      name: KNOWLEDGE_BASE_A.name,
    });
    const main = screen.getByRole("main");
    const addButton = await within(main).findByRole("button", {
      name: "添加资料",
    });
    await user.click(addButton);
    const dialog = screen.getByRole("dialog", { name: "添加资料" });
    const titleInput = within(dialog).getByLabelText("文档标题");
    await user.clear(titleInput);
    await user.type(titleInput, "上传文档");
    await user.upload(
      within(dialog).getByLabelText("文件"),
      new File(["真实内容"], "访谈.txt", { type: "text/plain" }),
    );
    await user.click(
      within(dialog).getByRole("button", { name: "添加资料" }),
    );

    await waitFor(() => expect(uploaded).toBe(true));
    expect(
      within(dialog).getByLabelText("文档标题"),
    ).toBeDisabled();
    expect(within(dialog).getByLabelText("文件")).toBeDisabled();
    expect(
      within(dialog).getByRole("button", { name: "上传文件" }),
    ).toBeDisabled();
    expect(
      within(dialog).getByRole("button", { name: "粘贴文本" }),
    ).toBeDisabled();
    expect(
      within(dialog).getByRole("button", { name: "取消" }),
    ).toBeDisabled();
    expect(
      within(dialog).getByRole("button", { name: "正在提交资料…" }),
    ).toBeDisabled();
    const uploadCall = fetchMock.mock.calls.find(
      ([input, init]) =>
        requestUrl(input).pathname.endsWith("/documents/upload") &&
        init?.method === "POST",
    );
    const headers = new Headers(uploadCall?.[1]?.headers);
    const body = uploadCall?.[1]?.body as FormData;
    expect(headers.get("Idempotency-Key")).toBeTruthy();
    expect(headers.has("Content-Type")).toBe(false);
    expect(body.get("title")).toBe("上传文档");
    expect((body.get("file") as File).name).toBe("访谈.txt");
    uploadResponse.resolve(
      jsonResponse(
        {
          document_id: "document-uploaded",
          state: "queued",
          status_url: "/status",
          version_id: "version-uploaded",
        },
        202,
      ),
    );
    await waitFor(() => {
      expect(
        screen.queryByRole("dialog", { name: "添加资料" }),
      ).not.toBeInTheDocument();
    });
  });
});

describe("知识库检索", () => {
  it("展示真实命中、降级原因和来源片段详情", async () => {
    const hit = makeKnowledgeHit({
      chunk_id: "chunk-pdf",
      text: "这是来自 PDF 第三页的真实片段。",
    });
    const fetchMock = createFetchMock((url, init) => {
      if (
        url.pathname ===
        `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}`
      ) {
        return jsonResponse(KNOWLEDGE_BASE_A);
      }
      if (
        url.pathname ===
        `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}/documents`
      ) {
        return jsonResponse(knowledgeListResponse([], 1, 10));
      }
      if (
        url.pathname ===
          `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}/search` &&
        init?.method === "POST"
      ) {
        return jsonResponse({
          channels: ["keyword", "semantic"],
          degraded_reasons: ["semantic_search_unavailable"],
          hits: [hit],
        });
      }
      if (
        url.pathname ===
        `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}/chunks/${hit.chunk_id}`
      ) {
        return jsonResponse(hit);
      }
      throw new Error(
        `Unexpected request: ${init?.method ?? "GET"} ${url}`,
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp(`/knowledge/${KNOWLEDGE_BASE_A.id}`);
    await screen.findByRole("heading", {
      level: 1,
      name: KNOWLEDGE_BASE_A.name,
    });
    await user.click(
      screen.getByRole("button", { name: "检索测试" }),
    );
    const dialog = screen.getByRole("dialog", { name: "检索测试" });
    await user.type(
      within(dialog).getByLabelText("检索内容"),
      "市场 判断",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "开始检索" }),
    );

    expect(
      await within(dialog).findByText("1 个命中片段"),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByText(
        "检索已降级：语义检索暂不可用",
      ),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByText("关键词 + 语义 · PDF 第 3 页"),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByText("这是来自 PDF 第三页的真实片段。"),
    ).toBeInTheDocument();

    await user.click(
      within(dialog).getByRole("button", { name: "查看片段" }),
    );
    const chunkDialog = screen.getByRole("dialog", {
      name: "来源片段",
    });
    expect(
      await within(chunkDialog).findByText(
        "这是来自 PDF 第三页的真实片段。",
      ),
    ).toBeInTheDocument();
  });

  it("检索服务 503 时显示稳定错误提示", async () => {
    const fetchMock = createFetchMock((url, init) => {
      if (
        url.pathname ===
        `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}`
      ) {
        return jsonResponse(KNOWLEDGE_BASE_A);
      }
      if (
        url.pathname ===
        `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}/documents`
      ) {
        return jsonResponse(knowledgeListResponse([], 1, 10));
      }
      if (
        url.pathname ===
          `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}/search` &&
        init?.method === "POST"
      ) {
        return jsonResponse(
          { detail: "knowledge_search_unavailable" },
          503,
        );
      }
      throw new Error(
        `Unexpected request: ${init?.method ?? "GET"} ${url}`,
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp(`/knowledge/${KNOWLEDGE_BASE_A.id}`);
    await screen.findByRole("heading", {
      level: 1,
      name: KNOWLEDGE_BASE_A.name,
    });
    await user.click(
      screen.getByRole("button", { name: "检索测试" }),
    );
    const dialog = screen.getByRole("dialog", { name: "检索测试" });
    await user.type(
      within(dialog).getByLabelText("检索内容"),
      "检索",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "开始检索" }),
    );

    expect(
      await within(dialog).findByText(
        "检索服务暂时不可用，请稍后重试。",
      ),
    ).toBeInTheDocument();
  });
});
