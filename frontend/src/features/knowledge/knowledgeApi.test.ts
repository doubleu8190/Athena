import { describe, expect, it, vi } from "vitest";

import {
  filenameFromContentDisposition,
} from "../../shared/api/client";
import {
  getRequestUrl,
  knowledgeListResponse,
  KNOWLEDGE_BASE_A,
  makeKnowledgeDocument,
  makeKnowledgeHit,
} from "../../test/fixtures";
import { jsonResponse } from "../../test/testUtils";
import {
  bindProjectKnowledgeBase,
  createKnowledgeBase,
  deleteKnowledgeBase,
  downloadKnowledgeSource,
  getKnowledgeChunk,
  importKnowledgeText,
  listKnowledgeBases,
  listKnowledgeDocuments,
  replaceKnowledgeFile,
  replaceKnowledgeText,
  searchKnowledgeBase,
  unbindProjectKnowledgeBase,
  updateKnowledgeBase,
  uploadKnowledgeDocument,
} from "./api";

describe("知识库 API 契约", () => {
  it("序列化分页参数并接受没有 total 的列表响应", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(
        knowledgeListResponse([KNOWLEDGE_BASE_A], 2, 12),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await listKnowledgeBases({ page: 2, pageSize: 12 });

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/v1/knowledge-bases?page=2&page_size=12",
    );
    expect(result).toEqual(
      knowledgeListResponse([KNOWLEDGE_BASE_A], 2, 12),
    );
    expect("total" in result).toBe(false);
  });

  it("覆盖知识库 CRUD 和文档分页路径", async () => {
    const document = makeKnowledgeDocument({ id: "document-1" });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(KNOWLEDGE_BASE_A, 201))
      .mockResolvedValueOnce(jsonResponse(KNOWLEDGE_BASE_A))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(
        jsonResponse(knowledgeListResponse([document], 3, 10)),
      );
    vi.stubGlobal("fetch", fetchMock);

    await createKnowledgeBase({
      name: KNOWLEDGE_BASE_A.name,
      description: KNOWLEDGE_BASE_A.description ?? undefined,
    });
    await updateKnowledgeBase(KNOWLEDGE_BASE_A.id, {
      name: "更新后的知识库",
    });
    await expect(
      deleteKnowledgeBase(KNOWLEDGE_BASE_A.id),
    ).resolves.toBeUndefined();
    await listKnowledgeDocuments(KNOWLEDGE_BASE_A.id, {
      page: 3,
      pageSize: 10,
    });

    expect(fetchMock.mock.calls[0]).toEqual([
      "/api/v1/knowledge-bases",
      expect.objectContaining({
        body: JSON.stringify({
          name: KNOWLEDGE_BASE_A.name,
          description: KNOWLEDGE_BASE_A.description,
        }),
        method: "POST",
      }),
    ]);
    expect(getRequestUrl(fetchMock.mock.calls[1][0])).toBe(
      `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}`,
    );
    expect(fetchMock.mock.calls[2][1]).toMatchObject({
      method: "DELETE",
    });
    expect(getRequestUrl(fetchMock.mock.calls[3][0])).toBe(
      `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}/documents?page=3&page_size=10`,
    );
  });

  it("multipart 上传不手动设置 Content-Type，并携带幂等键", async () => {
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse(
        {
          document_id: "document-1",
          state: "queued",
          status_url:
            "/api/v1/knowledge-bases/knowledge-a/documents/document-1",
          version_id: "version-1",
        },
        202,
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const file = new File(["真实内容"], "访谈记录.txt", {
      type: "text/plain",
    });

    await uploadKnowledgeDocument(
      KNOWLEDGE_BASE_A.id,
      "访谈记录",
      file,
      "idem-upload",
    );
    await replaceKnowledgeFile(
      KNOWLEDGE_BASE_A.id,
      "document-1",
      file,
      "idem-replace",
    );

    const uploadInit = fetchMock.mock.calls[0][1] as RequestInit;
    const uploadHeaders = new Headers(uploadInit.headers);
    const uploadBody = uploadInit.body as FormData;
    expect(uploadHeaders.get("Idempotency-Key")).toBe("idem-upload");
    expect(uploadHeaders.has("Content-Type")).toBe(false);
    expect(uploadBody.get("title")).toBe("访谈记录");
    expect((uploadBody.get("file") as File).name).toBe("访谈记录.txt");

    const replaceInit = fetchMock.mock.calls[1][1] as RequestInit;
    const replaceHeaders = new Headers(replaceInit.headers);
    expect(replaceHeaders.get("Idempotency-Key")).toBe("idem-replace");
    expect(replaceHeaders.has("Content-Type")).toBe(false);
    expect((replaceInit.body as FormData).has("title")).toBe(false);
  });

  it("文本导入和替换使用 JSON 与幂等键", async () => {
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse(
        {
          document_id: "document-1",
          state: "queued",
          status_url: "/status",
          version_id: "version-1",
        },
        202,
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await importKnowledgeText(
      KNOWLEDGE_BASE_A.id,
      "文本标题",
      "文本正文",
      "idem-text",
    );
    await replaceKnowledgeText(
      KNOWLEDGE_BASE_A.id,
      "document-1",
      "现有标题",
      "新版本正文",
      "idem-replace-text",
    );

    const firstHeaders = new Headers(fetchMock.mock.calls[0][1]?.headers);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: "POST" });
    expect(
      JSON.parse(String(fetchMock.mock.calls[0][1]?.body)),
    ).toEqual({ text: "文本正文", title: "文本标题" });
    expect(firstHeaders.get("Idempotency-Key")).toBe("idem-text");
    expect(getRequestUrl(fetchMock.mock.calls[1][0])).toBe(
      `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}/documents/document-1/versions/text`,
    );
    const secondHeaders = new Headers(fetchMock.mock.calls[1][1]?.headers);
    expect(
      JSON.parse(String(fetchMock.mock.calls[1][1]?.body)),
    ).toEqual({ text: "新版本正文", title: "现有标题" });
    expect(secondHeaders.get("Idempotency-Key")).toBe(
      "idem-replace-text",
    );
  });

  it("来源下载使用鉴权 fetch 并解析 UTF-8 文件名", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("source", {
        headers: {
          "Content-Disposition":
            "attachment; filename*=UTF-8''%E8%AE%BF%E8%B0%88%E8%AE%B0%E5%BD%95.txt",
          "Content-Type": "text/plain",
        },
        status: 200,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const result = await downloadKnowledgeSource(
      KNOWLEDGE_BASE_A.id,
      "document-1",
      "version-1",
    );

    expect(result.filename).toBe("访谈记录.txt");
    expect(result.blob.size).toBe(6);
    expect(fetchMock).toHaveBeenCalledWith(
      `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}/documents/document-1/versions/version-1/source`,
      expect.objectContaining({
        credentials: "same-origin",
        method: "GET",
      }),
    );
    expect(
      filenameFromContentDisposition(
        'attachment; filename="fallback.txt"',
      ),
    ).toBe("fallback.txt");
  });

  it("检索、片段和项目知识库关联使用真实路径", async () => {
    const hit = makeKnowledgeHit({ chunk_id: "chunk-1" });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          channels: ["keyword"],
          degraded_reasons: [],
          hits: [hit],
        }),
      )
      .mockResolvedValueOnce(jsonResponse(hit))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await searchKnowledgeBase(KNOWLEDGE_BASE_A.id, "访谈 结论", 8);
    await getKnowledgeChunk(KNOWLEDGE_BASE_A.id, hit.chunk_id);
    await bindProjectKnowledgeBase("project-a", KNOWLEDGE_BASE_A.id);
    await unbindProjectKnowledgeBase("project-a", KNOWLEDGE_BASE_A.id);

    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      body: JSON.stringify({ query: "访谈 结论", limit: 8 }),
      method: "POST",
    });
    expect(getRequestUrl(fetchMock.mock.calls[1][0])).toBe(
      `/api/v1/knowledge-bases/${KNOWLEDGE_BASE_A.id}/chunks/chunk-1`,
    );
    expect(getRequestUrl(fetchMock.mock.calls[2][0])).toBe(
      `/api/v1/projects/project-a/knowledge-bases/${KNOWLEDGE_BASE_A.id}`,
    );
    expect(fetchMock.mock.calls[2][1]).toMatchObject({ method: "PUT" });
    expect(fetchMock.mock.calls[3][1]).toMatchObject({ method: "DELETE" });
  });
});
