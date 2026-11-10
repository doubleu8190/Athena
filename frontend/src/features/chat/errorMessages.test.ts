import { describe, expect, it } from "vitest";

import { ApiError } from "../../shared/api/client";
import {
  chatErrorMessage,
  requestIdCanBeReused,
} from "./errorMessages";

const ERROR_CODES = [
  "run_already_active",
  "run_request_id_conflict",
  "run_text_invalid",
  "run_input_request_not_pending",
  "run_not_found",
  "session_not_found",
  "run_not_retryable",
  "run_not_latest",
  "run_tool_approval_not_pending",
  "run_tool_approval_already_decided",
  "tool_retry_confirmation_required",
  "network_error",
] as const;

describe("Run 错误映射", () => {
  it.each(ERROR_CODES)("%s 有明确中文提示", (code) => {
    const message = chatErrorMessage(new ApiError(409, code));
    expect(message).not.toBe("运行操作未完成，请稍后重试。");
    expect(message.length).toBeGreaterThan(4);
  });

  it("只对结果不确定的失败复用 request_id", () => {
    expect(requestIdCanBeReused(new ApiError(0, "network_error"))).toBe(
      true,
    );
    expect(requestIdCanBeReused(new ApiError(503, "http_error"))).toBe(
      true,
    );
    expect(
      requestIdCanBeReused(new ApiError(201, "invalid_response")),
    ).toBe(true);
    expect(
      requestIdCanBeReused(new ApiError(409, "run_already_active")),
    ).toBe(false);
    expect(
      requestIdCanBeReused(new ApiError(422, "run_text_invalid")),
    ).toBe(false);
  });
});

