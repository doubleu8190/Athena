import { afterEach, describe, expect, it, vi } from "vitest";

import { getCurrentUser } from "../../features/auth/api";
import { submitRun } from "../../features/chat/api";
import { makeRun } from "../../test/fixtures";
import {
  ADMIN_FORBIDDEN_EVENT,
  AUTH_UNAUTHORIZED_EVENT,
  ApiError,
  advanceAuthIdentityEpoch,
  apiDelete,
  apiDownload,
  apiGet,
  apiMultipart,
  apiPatch,
  apiPost,
  stripApiPrefix,
} from "./client";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
    status,
  });
}

function deferredBodyResponse(
  body: string,
  status = 200,
  contentType = "application/json",
) {
  const encoder = new TextEncoder();
  let controller: ReadableStreamDefaultController<Uint8Array> | null = null;
  const stream = new ReadableStream<Uint8Array>({
    start(nextController) {
      controller = nextController;
    },
  });
  return {
    response: new Response(stream, {
      headers: {
        "Content-Disposition": "attachment; filename=private.txt",
        "Content-Type": contentType,
      },
      status,
    }),
    complete() {
      if (!controller) {
        throw new Error("Response stream is not ready");
      }
      controller.enqueue(encoder.encode(body));
      controller.close();
    },
  };
}

function flushTasks(): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, 0);
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("OpenAPI 客户端与底层请求管道", () => {
  it("把 401 转成 ApiError 并发布应用级未认证事件", async () => {
    const listener = vi.fn();
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, listener);
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ detail: "not_authenticated" }, 401),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(getCurrentUser()).rejects.toMatchObject({
      detail: "not_authenticated",
      name: "ApiError",
      status: 401,
    });

    expect(listener).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/auth/me",
      expect.objectContaining({
        cache: "no-store",
        credentials: "same-origin",
        method: "GET",
      }),
    );
    window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, listener);
  });

  it("非 JSON 错误降级为稳定的 http_error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("upstream unavailable", {
          headers: { "Content-Type": "text/plain" },
          status: 502,
        }),
      ),
    );

    const error = await getCurrentUser().catch((reason: unknown) => reason);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      detail: "http_error",
      status: 502,
    });
  });

  it("OpenAPI 请求在 2xx 非 JSON 或空体时返回 invalid_response，204 返回 undefined", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response("plain text", {
          headers: { "Content-Type": "text/plain" },
          status: 200,
        }),
      )
      .mockResolvedValueOnce(
        new Response("", {
          headers: { "Content-Type": "application/json" },
          status: 200,
        }),
      )
      .mockResolvedValueOnce(
        new Response("{broken-json", {
          headers: { "Content-Type": "application/json" },
          status: 200,
        }),
      )
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(apiGet("/api/v1/models")).rejects.toMatchObject({
      detail: "invalid_response",
      status: 200,
    });
    await expect(apiGet("/api/v1/models")).rejects.toMatchObject({
      detail: "invalid_response",
      status: 200,
    });
    await expect(apiGet("/api/v1/models")).rejects.toMatchObject({
      detail: "invalid_response",
      status: 200,
    });
    await expect(
      apiDelete("/api/v1/sessions/{session_id}", {
        params: { path: { session_id: "session-1" } },
      }),
    ).resolves.toBeUndefined();
  });

  it("OpenAPI 请求正确编码路径参数、查询参数与结构化错误（含 field 与 member_path）", async () => {
    const memberPath = "references/中文\\x01<b>说明</b>.txt";
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse(
          {
            detail: {
              code: "skill_markdown_field_invalid",
              field: "guide",
              member_path: memberPath,
            },
          },
          422,
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          created_at: "2026-11-10T00:00:00Z",
          id: "session/a",
          project_id: null,
          title: "重命名",
          updated_at: "2026-11-10T00:00:00Z",
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const error = await apiPost("/api/v1/admin/skills/markdown", {
      body: {
        description: "desc",
        enabled: true,
        featured: false,
        guide: "body",
        name: "name",
        scope: "company",
      },
      params: { header: { "Idempotency-Key": "idem-1" } },
    }).catch((reason: unknown) => reason);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      detail: "skill_markdown_field_invalid",
      field: "guide",
      memberPath,
      status: 422,
    });
    expect(
      new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get("Idempotency-Key"),
    ).toBe("idem-1");

    await apiPatch("/api/v1/sessions/{session_id}", {
      body: { title: "重命名" },
      params: { path: { session_id: "session/a" } },
    });
    expect(fetchMock.mock.calls[1]?.[0]).toBe("/api/v1/sessions/session%2Fa");
  });

  it("submitRun 通过 OpenAPI 客户端完整保留 knowledge_base_ids 三态语义（省略 / null / [] / string[]）", async () => {
    const run = makeRun({
      id: "run-kb-tristate",
      session_id: "session-1",
      status: "queued",
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(run, 201))
      .mockResolvedValueOnce(jsonResponse(run, 201))
      .mockResolvedValueOnce(jsonResponse(run, 201))
      .mockResolvedValueOnce(jsonResponse(run, 201));
    vi.stubGlobal("fetch", fetchMock);

    await submitRun("session-1", "req-1", "未指定范围");
    await submitRun("session-1", "req-2", "企业默认范围", [], [], {
      knowledgeBaseIds: null,
    });
    await submitRun("session-1", "req-3", "显式关闭知识库", [], [], {
      knowledgeBaseIds: [],
    });
    await submitRun("session-1", "req-4", "指定知识库", [], [], {
      knowledgeBaseIds: ["kb-1", "kb-2"],
    });

    const bodies = fetchMock.mock.calls.map(
      ([, init]) => JSON.parse(String(init?.body)) as Record<string, unknown>,
    );
    expect("knowledge_base_ids" in bodies[0]).toBe(false);
    expect("knowledge_base_ids" in bodies[1]).toBe(true);
    expect(bodies[1].knowledge_base_ids).toBeNull();
    expect(bodies[2].knowledge_base_ids).toEqual([]);
    expect(bodies[3].knowledge_base_ids).toEqual(["kb-1", "kb-2"]);
  });

  it("OpenAPI 请求收到 403 admin_required 时发布去掉 /api/v1 前缀的路径事件", async () => {
    const adminListener = vi.fn();
    const authListener = vi.fn();
    window.addEventListener(ADMIN_FORBIDDEN_EVENT, adminListener);
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, authListener);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({ detail: "admin_required" }, 403),
      ),
    );

    await expect(
      apiGet("/api/v1/admin/skills/{skill_id}", {
        params: { path: { skill_id: "skill-42" } },
      }),
    ).rejects.toMatchObject({
      detail: "admin_required",
      status: 403,
    });

    expect(adminListener).toHaveBeenCalledTimes(1);
    expect(
      (adminListener.mock.calls[0]?.[0] as CustomEvent<{ path: string }>)
        .detail,
    ).toEqual({
      path: "/admin/skills/skill-42",
    });
    expect(authListener).not.toHaveBeenCalled();
    expect(stripApiPrefix("/api/v1/admin/tools?page=1")).toBe(
      "/admin/tools?page=1",
    );
    window.removeEventListener(ADMIN_FORBIDDEN_EVENT, adminListener);
    window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, authListener);
  });

  it("结构化技能包错误保留稳定 code 和中文控制转义成员路径", async () => {
    const memberPath = "references/中文\\x01<b>说明</b>.txt";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse(
          {
            detail: {
              code: "skill_zip_path_invalid",
              member_path: memberPath,
            },
          },
          422,
        ),
      ),
    );

    const error = await apiMultipart(
      "/admin/skills/import",
      new FormData(),
    ).catch((reason: unknown) => reason);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      detail: "skill_zip_path_invalid",
      memberPath,
      status: 422,
    });
    expect((error as Error).message).toBe(
      "API 422: skill_zip_path_invalid",
    );
  });

  it("原有 string detail 仍保持原错误码且没有成员路径", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({ detail: "skill_package_invalid" }, 422),
      ),
    );

    await expect(
      apiMultipart("/admin/skills/import", new FormData()),
    ).rejects.toMatchObject({
      detail: "skill_package_invalid",
      memberPath: undefined,
      status: 422,
    });
  });

  it("ApiError 继续兼容原有 cause ErrorOptions", () => {
    const cause = new TypeError("upstream failed");
    const error = new ApiError(0, "network_error", { cause });

    expect(error.cause).toBe(cause);
    expect(error.memberPath).toBeUndefined();
  });

  it("网络失败安全转换为 network_error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new TypeError("Failed to fetch")),
    );

    await expect(getCurrentUser()).rejects.toMatchObject({
      detail: "network_error",
      status: 0,
    });
  });

  it("multipart 和下载失败沿用统一 401 事件与错误码", async () => {
    const listener = vi.fn();
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, listener);
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({ detail: "not_authenticated" }, 401),
      )
      .mockResolvedValueOnce(
        jsonResponse({ detail: "source_not_found" }, 404),
      );
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      apiMultipart("/knowledge-bases/a/documents/upload", new FormData()),
    ).rejects.toMatchObject({
      detail: "not_authenticated",
      status: 401,
    });
    await expect(apiDownload("/source")).rejects.toMatchObject({
      detail: "source_not_found",
      status: 404,
    });

    expect(listener).toHaveBeenCalledTimes(1);
    window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, listener);
  });

  it("403 admin_required 仅发布管理员权限失效事件并携带触发路径", async () => {
    const adminListener = vi.fn();
    const authListener = vi.fn();
    window.addEventListener(ADMIN_FORBIDDEN_EVENT, adminListener);
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, authListener);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({ detail: "admin_required" }, 403),
      ),
    );

    await expect(
      apiMultipart("/admin/skills/import", new FormData()),
    ).rejects.toMatchObject({
      detail: "admin_required",
      status: 403,
    });

    expect(adminListener).toHaveBeenCalledTimes(1);
    const event = adminListener.mock.calls[0]?.[0];
    expect(event).toBeInstanceOf(CustomEvent);
    expect((event as CustomEvent<{ path: string }>).detail).toEqual({
      path: "/admin/skills/import",
    });
    expect(authListener).not.toHaveBeenCalled();
    window.removeEventListener(ADMIN_FORBIDDEN_EVENT, adminListener);
    window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, authListener);
  });

  it("身份在 OpenAPI 成功 JSON 响应体读取期间切换时丢弃旧数据", async () => {
    const delayed = deferredBodyResponse(
      JSON.stringify({ items: [], default_model_id: null }),
    );
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(delayed.response),
    );

    const request = apiGet("/api/v1/models");
    await flushTasks();
    expect(delayed.response.bodyUsed).toBe(true);

    advanceAuthIdentityEpoch();
    delayed.complete();

    await expect(request).rejects.toMatchObject({
      name: "AbortError",
    });
  });

  it("身份在 OpenAPI 错误 JSON 读取期间切换时不发布旧 401 或 403 事件", async () => {
    const authListener = vi.fn();
    const adminListener = vi.fn();
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, authListener);
    window.addEventListener(ADMIN_FORBIDDEN_EVENT, adminListener);

    const delayed401 = deferredBodyResponse(
      JSON.stringify({ detail: "not_authenticated" }),
      401,
    );
    const delayed403 = deferredBodyResponse(
      JSON.stringify({ detail: "admin_required" }),
      403,
    );
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(delayed401.response)
        .mockResolvedValueOnce(delayed403.response),
    );

    const req401 = apiGet("/api/v1/models");
    const req403 = apiGet("/api/v1/admin/tools");
    await flushTasks();
    expect(delayed401.response.bodyUsed).toBe(true);
    expect(delayed403.response.bodyUsed).toBe(true);

    advanceAuthIdentityEpoch();
    delayed401.complete();
    delayed403.complete();

    await expect(req401).rejects.toMatchObject({ name: "AbortError" });
    await expect(req403).rejects.toMatchObject({ name: "AbortError" });
    expect(authListener).not.toHaveBeenCalled();
    expect(adminListener).not.toHaveBeenCalled();
    window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, authListener);
    window.removeEventListener(ADMIN_FORBIDDEN_EVENT, adminListener);
  });

  it("身份在 OpenAPI POST 成功 JSON 响应体读取期间切换时丢弃旧数据", async () => {
    const delayed = deferredBodyResponse(
      JSON.stringify({ id: "private-project-a" }),
    );
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(delayed.response),
    );

    const request = apiPost("/api/v1/projects", {
      body: { name: "private-project-a" },
    });
    await flushTasks();
    expect(delayed.response.bodyUsed).toBe(true);

    advanceAuthIdentityEpoch();
    delayed.complete();

    await expect(request).rejects.toMatchObject({
      name: "AbortError",
    });
  });

  it("身份在 multipart 成功响应体读取期间切换时丢弃旧数据", async () => {
    const delayed = deferredBodyResponse(
      JSON.stringify({ id: "private-upload-a" }),
    );
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(delayed.response),
    );

    const request = apiMultipart<{ id: string }>(
      "/knowledge-bases/a/documents/upload",
      new FormData(),
    );
    await flushTasks();
    expect(delayed.response.bodyUsed).toBe(true);

    advanceAuthIdentityEpoch();
    delayed.complete();

    await expect(request).rejects.toMatchObject({
      name: "AbortError",
    });
  });

  it("身份在 multipart 错误 JSON 读取期间切换时不发布旧 403 管理事件", async () => {
    const adminListener = vi.fn();
    window.addEventListener(ADMIN_FORBIDDEN_EVENT, adminListener);
    const delayed = deferredBodyResponse(
      JSON.stringify({ detail: "admin_required" }),
      403,
    );
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(delayed.response),
    );

    const request = apiMultipart("/admin/skills/import", new FormData());
    await flushTasks();
    expect(delayed.response.bodyUsed).toBe(true);

    advanceAuthIdentityEpoch();
    delayed.complete();

    await expect(request).rejects.toMatchObject({
      name: "AbortError",
    });
    expect(adminListener).not.toHaveBeenCalled();
    window.removeEventListener(ADMIN_FORBIDDEN_EVENT, adminListener);
  });

  it("身份在 multipart 错误 JSON 读取期间切换时不发布旧 401 事件", async () => {
    const authListener = vi.fn();
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, authListener);
    const delayed = deferredBodyResponse(
      JSON.stringify({ detail: "not_authenticated" }),
      401,
    );
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(delayed.response),
    );

    const request = apiMultipart(
      "/knowledge-bases/a/documents/upload",
      new FormData(),
    );
    await flushTasks();
    expect(delayed.response.bodyUsed).toBe(true);

    advanceAuthIdentityEpoch();
    delayed.complete();

    await expect(request).rejects.toMatchObject({
      name: "AbortError",
    });
    expect(authListener).not.toHaveBeenCalled();
    window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, authListener);
  });

  it("身份在 Blob 读取期间切换时不返回旧文件", async () => {
    const delayed = deferredBodyResponse(
      "private file from user A",
      200,
      "application/octet-stream",
    );
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(delayed.response),
    );

    const request = apiDownload("/knowledge-bases/a/source");
    await flushTasks();
    expect(delayed.response.bodyUsed).toBe(true);

    advanceAuthIdentityEpoch();
    delayed.complete();

    await expect(request).rejects.toMatchObject({
      name: "AbortError",
    });
  });
});
