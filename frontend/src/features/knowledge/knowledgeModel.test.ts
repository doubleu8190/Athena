import { describe, expect, it } from "vitest";

import { ApiError } from "../../shared/api/client";
import {
  idempotencyKeyCanBeReused,
  knowledgeErrorMessage,
} from "./errorMessages";
import {
  formatKnowledgeWarnings,
} from "./display";
import {
  defaultDocumentTitle,
  validateDocumentTitle,
  validateKnowledgeDescription,
  validateKnowledgeFile,
  validateKnowledgeName,
  validateKnowledgeText,
} from "./model";

describe("知识库输入边界", () => {
  it("名称按 Unicode 码点限制为 120", () => {
    expect(validateKnowledgeName("😀".repeat(120))).toBeNull();
    expect(validateKnowledgeName("😀".repeat(121))).toContain("1–120");
    expect(validateKnowledgeName("名称\n第二行")).toContain("控制字符");
  });

  it("已有说明不能被前端假装清空", () => {
    expect(validateKnowledgeDescription("", null)).toBeNull();
    expect(validateKnowledgeDescription("", "原说明")).toContain(
      "不支持清空",
    );
    expect(validateKnowledgeDescription("新说明", "原说明")).toBeNull();
  });

  it("文档标题、文本和文件边界与后端一致", () => {
    expect(validateDocumentTitle("😀".repeat(255))).toBeNull();
    expect(validateDocumentTitle("😀".repeat(256))).toContain("1–255");
    expect(validateKnowledgeText("字".repeat(1_000_000))).toBeNull();
    expect(validateKnowledgeText("字".repeat(1_000_001))).toContain(
      "1,000,000",
    );
    expect(
      validateKnowledgeFile(
        new File(["x"], "旧文件.doc", { type: "application/msword" }),
      ),
    ).toContain("旧版 Office");
    expect(
      validateKnowledgeFile(
        new File(["x"], "资料.exe", {
          type: "application/octet-stream",
        }),
      ),
    ).toContain("支持 TXT");
    expect(
      validateKnowledgeFile(
        new File(["x"], "资料.pdf", { type: "application/pdf" }),
      ),
    ).toBeNull();
  });

  it("文件标题回填按 Unicode 码点截断", () => {
    expect(defaultDocumentTitle("😀".repeat(260) + ".txt")).toBe(
      "😀".repeat(255),
    );
  });

  it("展示真实处理警告而不暴露无说明的原始代码", () => {
    expect(
      formatKnowledgeWarnings(["xlsx_formula_cache_missing"]),
    ).toBe("XLSX 部分公式缺少缓存值，已按空值处理");
  });
});

describe("知识库错误消息", () => {
  it("映射配额、格式、检索和项目上限", () => {
    expect(
      knowledgeErrorMessage(
        new ApiError(429, "knowledge_storage_quota_exceeded"),
      ),
    ).toContain("存储配额");
    expect(
      knowledgeErrorMessage(
        new ApiError(415, "knowledge_legacy_office_unsupported"),
      ),
    ).toContain("旧版 Office");
    expect(
      knowledgeErrorMessage(
        new ApiError(503, "knowledge_search_unavailable"),
      ),
    ).toContain("检索服务");
    expect(
      knowledgeErrorMessage(
        new ApiError(409, "project_knowledge_limit_reached"),
      ),
    ).toContain("20 个知识库");
  });

  it("只对结果不确定的失败复用幂等键", () => {
    expect(idempotencyKeyCanBeReused(new ApiError(0, "network_error"))).toBe(
      true,
    );
    expect(idempotencyKeyCanBeReused(new ApiError(503, "http_error"))).toBe(
      true,
    );
    expect(
      idempotencyKeyCanBeReused(new ApiError(202, "invalid_response")),
    ).toBe(true);
    expect(
      idempotencyKeyCanBeReused(new ApiError(409, "idempotency_conflict")),
    ).toBe(false);
    expect(
      idempotencyKeyCanBeReused(new ApiError(422, "document_title_invalid")),
    ).toBe(false);
  });
});
