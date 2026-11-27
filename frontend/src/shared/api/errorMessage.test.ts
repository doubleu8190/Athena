import { describe, expect, it } from "vitest";

import { ApiError } from "./client";
import {
  COMMON_API_ERROR_COPY,
  createApiErrorMessage,
  lookupErrorCopy,
} from "./errorMessage";

describe("shared/api/errorMessage", () => {
  const resolveMessage = createApiErrorMessage(
    {
      custom_code: "业务专属提示。",
      http_error: "业务覆写的 HTTP 提示。",
    },
    "默认兜底提示。",
  );

  it("优先命中业务错误码表并允许覆写公共错误码", () => {
    expect(resolveMessage(new ApiError(422, "custom_code"))).toBe(
      "业务专属提示。",
    );
    expect(resolveMessage(new ApiError(500, "http_error"))).toBe(
      "业务覆写的 HTTP 提示。",
    );
  });

  it("未命中业务表时仅对无歧义的公共码表与 0/401 状态码回退", () => {
    expect(resolveMessage(new ApiError(0, "network_error"))).toBe(
      COMMON_API_ERROR_COPY.network_error,
    );
    expect(resolveMessage(new ApiError(0, "unknown_network"))).toBe(
      COMMON_API_ERROR_COPY.network_error,
    );
    expect(resolveMessage(new ApiError(401, "unknown_auth"))).toBe(
      COMMON_API_ERROR_COPY.not_authenticated,
    );
    expect(resolveMessage(new ApiError(403, "admin_required"))).toBe(
      COMMON_API_ERROR_COPY.admin_required,
    );
    expect(resolveMessage(new ApiError(503, "database_unavailable"))).toBe(
      COMMON_API_ERROR_COPY.database_unavailable,
    );
    expect(resolveMessage(new ApiError(200, "invalid_response"))).toBe(
      COMMON_API_ERROR_COPY.invalid_response,
    );
  });

  it("普通模块遇到未枚举的 403/503 错误码时回退业务兜底文案，不误归因为管理权限或数据库故障", () => {
    expect(resolveMessage(new ApiError(403, "origin_not_allowed"))).toBe(
      "默认兜底提示。",
    );
    expect(resolveMessage(new ApiError(403, "unknown_forbidden"))).toBe(
      "默认兜底提示。",
    );
    expect(resolveMessage(new ApiError(503, "unknown_unavailable"))).toBe(
      "默认兜底提示。",
    );

    const resolveWithStatusFallback = createApiErrorMessage(
      { admin_required: "管理员权限已失效。" },
      "管理兜底提示。",
      { 403: "管理员权限已失效。" },
    );
    expect(
      resolveWithStatusFallback(new ApiError(403, "unknown_forbidden")),
    ).toBe("管理员权限已失效。");
  });

  it("使用 Object.hasOwn 防止原型链污染，并在未知错误或非 ApiError 时返回兜底文案", () => {
    for (const protoKey of ["__proto__", "constructor", "toString"]) {
      expect(lookupErrorCopy(COMMON_API_ERROR_COPY, protoKey)).toBeUndefined();
      expect(resolveMessage(new ApiError(500, protoKey))).toBe("默认兜底提示。");
    }
    expect(resolveMessage(new Error("raw error"))).toBe("默认兜底提示。");
    expect(resolveMessage("string error")).toBe("默认兜底提示。");
  });
});
