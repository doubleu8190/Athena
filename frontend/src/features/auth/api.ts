import {
  apiGet,
  apiPatch,
  apiPost,
  type ApiSchemas,
} from "../../shared/api/client";
import type {
  AuthProvidersResponse,
  LoginInput,
  MockUser,
  MockUsersResponse,
  User,
} from "./model";
import { sanitizeReturnTo } from "./returnTo";

function toUser(raw: ApiSchemas["UserResponse"]): User {
  // 对齐 Pydantic 含默认值的可选响应字段，确保业务层始终拿到完整结构。
  return {
    ...raw,
    auth_provider: raw.auth_provider ?? "local",
    avatar_url: raw.avatar_url ?? null,
    departments: raw.departments ?? [],
    employee_no: raw.employee_no ?? null,
    job_title: raw.job_title ?? null,
    managed_department_ids: raw.managed_department_ids ?? [],
  };
}

function toMockUser(raw: ApiSchemas["MockUserResponse"]): MockUser {
  return {
    ...raw,
    avatar_url: raw.avatar_url ?? null,
    departments: raw.departments ?? [],
    employee_no: raw.employee_no ?? null,
    job_title: raw.job_title ?? null,
    managed_department_ids: raw.managed_department_ids ?? [],
  };
}

export async function getAuthProviders(): Promise<AuthProvidersResponse> {
  const response = await apiGet("/api/v1/auth/providers", {
    cache: "no-store",
  });
  return {
    ...response,
    local_login_enabled: response.local_login_enabled ?? true,
  };
}

export async function listMockUsers(): Promise<MockUsersResponse> {
  const response = await apiGet("/api/v1/auth/mock/users", {
    cache: "no-store",
  });
  return {
    items: response.items.map(toMockUser),
  };
}

export async function mockLogin(userId: string): Promise<User> {
  return toUser(
    await apiPost("/api/v1/auth/mock/login", {
      body: { user_id: userId },
    }),
  );
}

/**
 * 构造飞书 OAuth 授权入口 URL；return_to 必须经过站内安全路径校验后再编码，
 * 拦截外链、协议相对路径及 /login、/register 跳转死循环。
 */
export function buildFeishuAuthorizeUrl(returnTo: string): string {
  const safeReturnTo = sanitizeReturnTo(returnTo);
  return `/api/v1/auth/feishu/authorize?return_to=${encodeURIComponent(safeReturnTo)}`;
}

export async function getCurrentUser(): Promise<User> {
  return toUser(
    await apiGet("/api/v1/auth/me", {
      cache: "no-store",
    }),
  );
}

export async function login(input: LoginInput): Promise<User> {
  return toUser(
    await apiPost("/api/v1/auth/login", {
      body: input,
    }),
  );
}

export function logout(): Promise<void> {
  return apiPost("/api/v1/auth/logout");
}

export async function updateCurrentUser(displayName: string): Promise<User> {
  return toUser(
    await apiPatch("/api/v1/auth/me", {
      body: { display_name: displayName },
    }),
  );
}

export function changePassword(input: {
  current_password: string;
  new_password: string;
}): Promise<void> {
  return apiPost("/api/v1/auth/password", {
    body: input,
  });
}
