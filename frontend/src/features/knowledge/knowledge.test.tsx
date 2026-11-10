import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  getRequestUrl,
  knowledgeListResponse,
  KNOWLEDGE_BASE_A,
  KNOWLEDGE_BASE_B,
  TEST_USER,
} from "../../test/fixtures";
import { jsonResponse, renderApp } from "../../test/testUtils";

function requestUrl(input: RequestInfo | URL): URL {
  return new URL(getRequestUrl(input), "http://localhost");
}

describe("知识库目录", () => {
  it("主内容、侧栏和分页只展示真实知识库字段", async () => {
    const fetchMock = vi.fn(
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
            knowledgeListResponse(
              [KNOWLEDGE_BASE_A, KNOWLEDGE_BASE_B],
              1,
              pageSize,
            ),
          );
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    renderApp("/knowledge");

    await screen.findByRole("heading", { level: 1, name: "知识库" });
    const main = screen.getByRole("main");
    expect(
      await within(main).findByRole("link", { name: /产品研究库/ }),
    ).toHaveAttribute("href", `/knowledge/${KNOWLEDGE_BASE_A.id}`);
    expect(
      within(main).getByText(KNOWLEDGE_BASE_A.description ?? ""),
    ).toBeInTheDocument();
    expect(within(main).queryByText(/共\s*\d+\s*个知识库/)).not.toBeInTheDocument();
    expect(within(main).queryByText(/份文档/)).not.toBeInTheDocument();

    const sidebar = screen.getByRole("complementary", {
      name: "工作区侧栏",
    });
    expect(
      within(sidebar).getByRole("link", { name: "产品研究库" }),
    ).toHaveAttribute("href", `/knowledge/${KNOWLEDGE_BASE_A.id}`);
    expect(
      within(sidebar).getByRole("link", { name: "市场资料库" }),
    ).toHaveAttribute("href", `/knowledge/${KNOWLEDGE_BASE_B.id}`);
    expect(fetchMock).toHaveBeenCalled();
  });

  it("下一页只依据后端 items 和 page_size 判断", async () => {
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (
          url.pathname === "/api/v1/knowledge-bases" &&
          (!init?.method || init.method === "GET")
        ) {
          return jsonResponse(
            knowledgeListResponse(
              [KNOWLEDGE_BASE_A, KNOWLEDGE_BASE_B],
              1,
              2,
            ),
          );
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);

    renderApp("/knowledge");

    await screen.findByRole("heading", { level: 1, name: "知识库" });
    const main = screen.getByRole("main");
    await within(main).findByText("产品研究库");
    expect(
      within(main).getByRole("button", { name: "下一页" }),
    ).toBeEnabled();
    expect(
      within(main).queryByText(/共\s*\d+\s*个知识库/),
    ).not.toBeInTheDocument();
  });

  it("创建知识库提交真实名称和说明并进入详情", async () => {
    let created = false;
    const createdBase = {
      ...KNOWLEDGE_BASE_A,
      id: "knowledge-created",
      name: "新建研究库",
      description: "用于整理访谈",
    };
    const fetchMock = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = requestUrl(input);
        if (url.pathname.endsWith("/auth/me")) {
          return jsonResponse(TEST_USER);
        }
        if (
          url.pathname === "/api/v1/knowledge-bases" &&
          init?.method === "POST"
        ) {
          created = true;
          return jsonResponse(createdBase, 201);
        }
        if (
          url.pathname === "/api/v1/knowledge-bases" &&
          (!init?.method || init.method === "GET")
        ) {
          const pageSize = Number(
            url.searchParams.get("page_size") ?? 20,
          );
          return jsonResponse(
            knowledgeListResponse(
              created ? [createdBase] : [],
              1,
              pageSize,
            ),
          );
        }
        if (url.pathname === `/api/v1/knowledge-bases/${createdBase.id}`) {
          return jsonResponse(createdBase);
        }
        if (
          url.pathname ===
          `/api/v1/knowledge-bases/${createdBase.id}/documents`
        ) {
          return jsonResponse(
            knowledgeListResponse([], 1, 10),
          );
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/knowledge");
    await screen.findByRole("heading", { level: 1, name: "知识库" });
    const main = screen.getByRole("main");
    const createButtons = await within(main).findAllByRole("button", {
      name: "新建知识库",
    });
    await user.click(createButtons[0]);
    const dialog = screen.getByRole("dialog", { name: "新建知识库" });
    await user.type(
      within(dialog).getByLabelText("知识库名称"),
      "新建研究库",
    );
    await user.type(
      within(dialog).getByLabelText("说明"),
      "用于整理访谈",
    );
    await user.click(
      within(dialog).getByRole("button", { name: "创建知识库" }),
    );

    expect(
      await screen.findByRole("heading", {
        level: 1,
        name: "新建研究库",
      }),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { name: "还没有文档" }),
    ).toBeInTheDocument();
    const postCall = fetchMock.mock.calls.find(
      ([input, init]) =>
        requestUrl(input).pathname === "/api/v1/knowledge-bases" &&
        init?.method === "POST",
    );
    expect(JSON.parse(String(postCall?.[1]?.body))).toEqual({
      description: "用于整理访谈",
      name: "新建研究库",
    });
  });

  it("改名和删除成功后刷新真实目录", async () => {
    let bases = [KNOWLEDGE_BASE_A];
    const fetchMock = vi.fn(
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
            knowledgeListResponse(bases, 1, pageSize),
          );
        }
        if (
          url.pathname === `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}`
        ) {
          if (init?.method === "PATCH") {
            const body = JSON.parse(String(init.body)) as {
              name?: string;
            };
            const updated = {
              ...KNOWLEDGE_BASE_A,
              name: body.name ?? KNOWLEDGE_BASE_A.name,
            };
            bases = [updated];
            return jsonResponse(updated);
          }
          if (init?.method === "DELETE") {
            bases = [];
            return new Response(null, { status: 204 });
          }
        }
        throw new Error(
          `Unexpected request: ${init?.method ?? "GET"} ${url}`,
        );
      },
    );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderApp("/knowledge");
    await screen.findByRole("heading", { level: 1, name: "知识库" });
    const main = screen.getByRole("main");
    await within(main).findByText("产品研究库");
    await user.click(
      within(main).getByRole("button", {
        name: "编辑知识库：产品研究库",
      }),
    );
    const editDialog = screen.getByRole("dialog", {
      name: "编辑知识库",
    });
    const nameInput = within(editDialog).getByLabelText("知识库名称");
    await user.clear(nameInput);
    await user.type(nameInput, "更新后的研究库");
    await user.click(
      within(editDialog).getByRole("button", {
        name: "保存知识库",
      }),
    );

    expect(
      await within(main).findByText("更新后的研究库"),
    ).toBeInTheDocument();
    await user.click(
      within(main).getByRole("button", {
        name: "删除知识库：更新后的研究库",
      }),
    );
    const deleteDialog = screen.getByRole("dialog", {
      name: "删除知识库",
    });
    await user.click(
      within(deleteDialog).getByLabelText(
        "我已确认要删除这个知识库",
      ),
    );
    await user.click(
      within(deleteDialog).getByRole("button", {
        name: "删除知识库",
      }),
    );

    expect(
      await within(main).findByRole("heading", {
        name: "还没有知识库",
      }),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(
          ([input, init]) =>
            requestUrl(input).pathname ===
              `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}` &&
            init?.method === "DELETE",
        ),
      ).toBe(true);
    });
  });
});
