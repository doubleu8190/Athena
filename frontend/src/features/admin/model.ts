// 管理端领域模型统一聚合出口：按子域拆分后仍保持对外导出契约 100% 兼容。
export {
  SYSTEM_PROMPT_CONTENT_MAX_LENGTH,
  validateHttpsUrl,
  validateSystemPromptContent,
  type AdminPageQuery,
  type FieldErrors,
  type OrgDepartment,
  type OrgDepartmentListResponse,
  type SystemPromptKind,
  type SystemPromptVersion,
  type SystemPromptVersionListResponse,
  type SystemPromptVersionSummary,
} from "./modelShared";

export * from "./modelLlm";
export * from "./modelMcp";
export * from "./modelSubagent";
