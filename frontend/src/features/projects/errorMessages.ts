import { ApiError } from "../../shared/api/client";

const PROJECT_ERROR_COPY: Record<string, string> = {
  http_error: "服务器拒绝了本次请求，请稍后重试。",
  invalid_response: "服务器返回了无法识别的响应，请稍后重试。",
  network_error: "无法连接服务器，请检查网络后重试。",
  project_default_skill_ids_invalid:
    "默认挂载技能数量不能超过 20 个，请检查所选技能。",
  project_description_invalid:
    "项目简介最多 500 个字符，且不能包含换行或控制字符。",
  project_has_sessions:
    "该项目仍有会话，请勾选「保留历史对话」转为普通对话，或先删除全部会话后再删除项目。",
  project_instructions_invalid:
    "专属背景指令最多 8,000 个字符，且不能包含不支持的控制字符。",
  project_name_invalid: "项目名称需为 1–120 个字符，且不能包含控制字符。",
  project_not_found: "项目不存在或已被删除。",
  session_has_active_run:
    "项目内存在正在执行的对话任务，请等待任务完成或停止后再删除项目。",
};

function lookupProjectErrorCopy(code: string): string | undefined {
  // 使用 Object.hasOwn 防止原型属性键污染错误文案查表。
  return Object.hasOwn(PROJECT_ERROR_COPY, code)
    ? PROJECT_ERROR_COPY[code]
    : undefined;
}

export function projectErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return (
      lookupProjectErrorCopy(error.detail) ?? "项目操作未完成，请稍后重试。"
    );
  }
  return "项目操作未完成，请稍后重试。";
}

export function isProjectNotFound(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    (error.status === 404 || error.detail === "project_not_found")
  );
}
