import { ApiError } from "../../shared/api/client";
import { createApiErrorMessage } from "../../shared/api/errorMessage";

const ADMIN_ERROR_COPY: Record<string, string> = {
  admin_required: "管理员权限已失效，正在重新核验账号。",
  llm_model_profile_invalid:
    "模型配置不符合后端约束，请检查地址、API Key 和容量参数。",
  llm_model_profile_conflict:
    "该模型配置与已有版本冲突，请检查名称或唯一配置字段。",
  llm_model_profile_not_found:
    "该模型版本不存在、已停用，或不再满足默认模型条件。",
  model_profile_not_found: "该模型配置不存在或已被删除。",
  model_profile_version_conflict:
    "模型配置已被其他管理员修改，请刷新最新配置后重试。",
  model_profile_in_use_as_default:
    "该模型正在作为系统默认模型使用，请先切换默认模型后再操作。",
  model_profile_in_use_by_compression_policy:
    "该模型正在被上下文压缩策略使用，请先更换压缩模型后再操作。",
  model_profile_in_use_by_subagent:
    "该模型仍被子智能体绑定，请先解除绑定后再删除。",
  llm_model_credential_unavailable:
    "模型 API Key 未配置，无法创建版本。",
  llm_model_credential_invalid:
    "API Key 无效或超过 16 KiB 安全限制，请检查后重试。",
  llm_compression_policy_invalid:
    "上下文压缩策略不符合后端约束，请检查比例与模型状态。",
  mcp_request_invalid:
    "MCP 配置不合法，请检查 transport、地址、命令、参数和秘密名称。",
  mcp_config_invalid:
    "MCP 配置未发生变化或参数不合法，请核对后重试。",
  mcp_conflict:
    "MCP Server 名称或配置版本发生冲突，请刷新后重新提交。",
  mcp_config_version_conflict:
    "MCP Server 配置已被其他管理员修改，请刷新最新配置后重试。",
  mcp_server_not_found: "目标 MCP Server 不存在或已被删除。",
  mcp_tool_not_found: "目标 MCP 工具不存在或已被移除。",
  visibility_scope_invalid: "可见范围无效，请重新选择。",
  visibility_scope_unsupported: "当前资源不支持所选可见范围，请重新选择。",
  owner_department_id_required: "部门共享资源必须指定归属部门。",
  owner_department_required: "部门共享资源必须指定归属部门。",
  owner_department_not_allowed: "全公司可见资源不允许指定归属部门。",
  visibility_grants_invalid: "可见性授权配置无效，请检查部门或角色设置。",
  visibility_grants_not_allowed: "全公司可见资源不允许配置附加授权。",
  mcp_discovery_failed:
    "服务端连接 MCP Server 或发现工具失败，配置未保存。",
  mcp_discovery_timeout:
    "服务端连接或工具发现超时，配置未保存。",
  mcp_unavailable:
    "MCP 目录存储或秘密服务暂时不可用，请稍后重试。",
  mcp_query_invalid: "分页参数无效。",
  mcp_tool_policy_invalid: "工具审批策略参数无效。",
  mcp_tool_metadata_invalid:
    "工具展示元信息不符合规范，请检查名称长度、摘要模板或参数标签。",
  system_prompt_invalid:
    "系统提示词不能全部为空白，也不能超过 100,000 个字符。",
  system_prompt_version_conflict:
    "当前配置已被其他管理员修改。本次输入仍保留，请核对后重新加载。",
  system_prompt_not_found: "该提示词版本不存在或不属于当前类别。",
  system_prompt_unavailable:
    "系统提示词初始化配置不可用，请联系部署管理员检查数据库。",
  system_prompt_page_invalid: "提示词历史分页参数无效。",
  subagent_not_found: "目标 Subagent 不存在或已被删除。",
  subagent_slug_conflict: "该 Slug 已被其他启用或停用中的 Subagent 占用，请更换。",
  subagent_slug_reserved: "该 Slug 属于系统保留字，请更换其他标识。",
  subagent_version_conflict:
    "当前 Subagent 配置已被其他管理员修改（版本冲突），请刷新最新配置后重试。",
  subagent_tool_forbidden:
    "工具白名单包含系统禁止的子智能体嵌套调度工具，无法保存。",
  subagent_model_unavailable:
    "所选模型配置不存在或未启用，请选择其他模型或跟随系统默认。",
  subagent_validation_error:
    "Subagent 配置参数不符合要求，请检查 Slug、长度及预算超时范围。",
  subagent_invalid: "Subagent 请求参数无效，请检查后重试。",
  resource_not_found: "目标资源不存在或已被删除。",
  http_error: "服务器拒绝了本次管理请求，请稍后重试。",
};

export const adminErrorMessage = createApiErrorMessage(
  ADMIN_ERROR_COPY,
  "管理操作未完成，请稍后重试。",
  { 403: ADMIN_ERROR_COPY.admin_required },
);

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
    (error.detail === "mcp_conflict" ||
      error.detail === "mcp_config_version_conflict")
  );
}

export function isModelProfileVersionConflict(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    error.status === 409 &&
    error.detail === "model_profile_version_conflict"
  );
}

export function isSubagentVersionConflict(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    error.status === 409 &&
    error.detail === "subagent_version_conflict"
  );
}

export function isSubagentSlugConflict(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    error.status === 409 &&
    error.detail === "subagent_slug_conflict"
  );
}
