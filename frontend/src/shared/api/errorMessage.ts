import { ApiError } from "./client";

/** 全站通用的 API 稳定错误码文案表；各业务模块可通过专属码表覆盖任意条目。 */
export const COMMON_API_ERROR_COPY: Readonly<Record<string, string>> = {
  admin_required: "当前操作需要管理员权限。",
  database_unavailable: "数据服务暂时不可用，请稍后重试。",
  http_error: "服务器拒绝了本次请求，请稍后重试。",
  invalid_response: "服务器返回了无法识别的响应，请稍后重试。",
  network_error: "无法连接服务器，请检查网络后重试。",
  not_authenticated: "登录状态已失效，请重新登录。",
};

/**
 * 仅在 detail 未命中业务码表与公共码表时生效的无歧义 HTTP 状态码兜底映射。
 * 403 / 503 存在多种非管理/非数据库业务语义，须按精确 detail 或由模块通过 statusFallbacks 显式声明。
 */
const COMMON_API_STATUS_COPY: Readonly<Record<number, string>> = {
  0: COMMON_API_ERROR_COPY.network_error,
  401: COMMON_API_ERROR_COPY.not_authenticated,
};

export interface ResolveApiErrorMessageOptions {
  /** 业务专属错误码文案表（优先级高于公共 API 错误码表）。 */
  messages: Readonly<Record<string, string>>;
  /** 非 ApiError 或未命中任何错误码与状态码时的兜底中文提示。 */
  fallback: string;
  /** 可选的业务专属 HTTP 状态码兜底映射（优先级高于公共状态码映射）。 */
  statusFallbacks?: Readonly<Record<number, string>>;
}

/**
 * 严格使用 Object.hasOwn 查表，防止 __proto__ / constructor / toString 等原型链属性
 * 被误当作错误文案返回或引发 React 渲染异常。
 */
export function lookupErrorCopy(
  table: Readonly<Record<string, string>>,
  code: string,
): string | undefined {
  return Object.hasOwn(table, code) ? table[code] : undefined;
}

function lookupStatusCopy(
  table: Readonly<Record<number, string>> | undefined,
  status: number,
): string | undefined {
  return table && Object.hasOwn(table, status) ? table[status] : undefined;
}

/**
 * 将未知异常统一映射为面向用户的受控中文提示：
 * 查找优先级：业务错误码表 -> 公共错误码表 -> 业务状态码表 -> 公共状态码表 -> 业务兜底文案。
 */
export function resolveApiErrorMessage(
  error: unknown,
  { fallback, messages, statusFallbacks }: ResolveApiErrorMessageOptions,
): string {
  if (!(error instanceof ApiError)) {
    return fallback;
  }
  return (
    lookupErrorCopy(messages, error.detail) ??
    lookupErrorCopy(COMMON_API_ERROR_COPY, error.detail) ??
    lookupStatusCopy(statusFallbacks, error.status) ??
    lookupStatusCopy(COMMON_API_STATUS_COPY, error.status) ??
    fallback
  );
}

/** 创建绑定特定业务码表与兜底文案的错误消息解析函数。 */
export function createApiErrorMessage(
  messages: Readonly<Record<string, string>>,
  fallback: string,
  statusFallbacks?: Readonly<Record<number, string>>,
): (error: unknown) => string {
  return (error: unknown) =>
    resolveApiErrorMessage(error, { fallback, messages, statusFallbacks });
}
