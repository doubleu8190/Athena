import { ApiError } from "../../shared/api/client";

const ADMIN_ERROR_COPY: Record<string, string> = {
  admin_required: "管理员权限已失效，正在重新核验账号。",
  not_authenticated: "登录状态已失效，请重新登录。",
  llm_model_profile_invalid:
    "模型配置不符合后端约束，请检查地址、API Key 和容量参数。",
  llm_model_profile_conflict:
    "该模型配置与已有版本冲突，请检查名称或唯一配置字段。",
  llm_model_profile_not_found:
    "该模型版本不存在、已停用，或不再满足默认模型条件。",
  llm_model_credential_unavailable:
    "模型 API Key 未配置，无法创建版本。",
  llm_model_credential_invalid:
    "API Key 无效或超过 16 KiB 安全限制，请检查后重试。",
  llm_compression_policy_invalid:
    "上下文压缩策略不符合后端约束，请检查比例与模型状态。",
  mcp_request_invalid:
    "MCP 配置不合法，请检查 transport、地址、命令、参数和秘密名称。",
  mcp_conflict:
    "MCP Server 名称或配置版本发生冲突，请刷新后重新提交。",
  mcp_discovery_failed:
    "服务端连接 MCP Server 或发现工具失败，配置未保存。",
  mcp_discovery_timeout:
    "服务端连接或工具发现超时，配置未保存。",
  mcp_unavailable:
    "MCP 目录存储或秘密服务暂时不可用，请稍后重试。",
  mcp_query_invalid: "分页参数无效。",
  mcp_tool_policy_invalid: "工具审批策略参数无效。",
  resource_not_found: "目标资源不存在或已被删除。",
  network_error: "无法连接服务器，请检查网络后重试。",
  http_error: "服务器拒绝了本次管理请求，请稍后重试。",
  invalid_response: "服务器返回了无法识别的响应，请稍后重试。",
};

export function adminErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return (
      ADMIN_ERROR_COPY[error.detail] ??
      (error.status === 403
        ? ADMIN_ERROR_COPY.admin_required
        : "管理操作未完成，请稍后重试。")
    );
  }
  return "管理操作未完成，请稍后重试。";
}

export function isAdminForbidden(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    error.status === 403 &&
    error.detail === "admin_required"
  );
}

export function isMcpConflict(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    error.status === 409 &&
    error.detail === "mcp_conflict"
  );
}

