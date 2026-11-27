import { ApiError } from "../../shared/api/client";
import { createApiErrorMessage } from "../../shared/api/errorMessage";

const PROJECT_ERROR_COPY: Record<string, string> = {
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

export const projectErrorMessage = createApiErrorMessage(
  PROJECT_ERROR_COPY,
  "项目操作未完成，请稍后重试。",
);

export function isProjectNotFound(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    (error.status === 404 || error.detail === "project_not_found")
  );
}
