import { afterEach, describe, expect, it, vi } from "vitest";

import { getCurrentUser } from "../../features/auth/api";
import {
  advanceAuthIdentityEpoch,
  apiDownload,
  apiMultipart,
  apiRequest,
  ADMIN_FORBIDDEN_EVENT,
  AUTH_UNAUTHORIZED_EVENT,
  ApiError,
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

describe("apiRequest", () => {
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

    const error = await apiRequest("/admin/skills/import").catch(
      (reason: unknown) => reason,
    );

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
      apiRequest("/admin/skills/import"),
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

    await expect(apiRequest("/admin/models")).rejects.toMatchObject({
      detail: "admin_required",
      status: 403,
    });

    expect(adminListener).toHaveBeenCalledTimes(1);
    const event = adminListener.mock.calls[0]?.[0];
    expect(event).toBeInstanceOf(CustomEvent);
    expect((event as CustomEvent<{ path: string }>).detail).toEqual({
      path: "/admin/models",
    });
    expect(authListener).not.toHaveBeenCalled();
    window.removeEventListener(ADMIN_FORBIDDEN_EVENT, adminListener);
    window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, authListener);
  });

  it("身份在成功 JSON 响应体读取期间切换时丢弃旧数据", async () => {
    const delayed = deferredBodyResponse(
      JSON.stringify({ id: "private-project-a" }),
    );
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(delayed.response),
    );

    const request = apiRequest<{ id: string }>("/projects/private");
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

  it("身份在错误 JSON 读取期间切换时不发布旧 403 管理事件", async () => {
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

    const request = apiRequest("/admin/tools");
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

  it("身份在错误 JSON 读取期间切换时不发布旧 401 事件", async () => {
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

    const request = apiRequest("/projects");
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
