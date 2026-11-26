import {
  apiGet,
  apiPatch,
  apiPost,
} from "../../shared/api/client";
import type {
  AuthProvidersResponse,
  LoginInput,
  MockUsersResponse,
  User,
} from "./model";
import { sanitizeReturnTo } from "./returnTo";

export function getAuthProviders(): Promise<AuthProvidersResponse> {
  return apiGet("/api/v1/auth/providers", {
    cache: "no-store",
  });
}

export function listMockUsers(): Promise<MockUsersResponse> {
  return apiGet("/api/v1/auth/mock/users", {
    cache: "no-store",
  });
}

export function mockLogin(userId: string): Promise<User> {
  return apiPost("/api/v1/auth/mock/login", {
    body: { user_id: userId },
  });
}

/**
 * 构造飞书 OAuth 授权入口 URL；return_to 必须经过站内安全路径校验后再编码，
 * 拦截外链、协议相对路径及 /login 跳转死循环。
 */
export function buildFeishuAuthorizeUrl(returnTo: string): string {
  const safeReturnTo = sanitizeReturnTo(returnTo);
  return `/api/v1/auth/feishu/authorize?return_to=${encodeURIComponent(safeReturnTo)}`;
}

export function getCurrentUser(): Promise<User> {
  return apiGet("/api/v1/auth/me", {
    cache: "no-store",
  });
}

export function login(input: LoginInput): Promise<User> {
  return apiPost("/api/v1/auth/login", {
    body: input,
  });
}

export function logout(): Promise<void> {
  return apiPost("/api/v1/auth/logout");
}

export function updateCurrentUser(displayName: string): Promise<User> {
  return apiPatch("/api/v1/auth/me", {
    body: { display_name: displayName },
  });
}

export function changePassword(input: {
  current_password: string;
  new_password: string;
}): Promise<void> {
  return apiPost("/api/v1/auth/password", {
    body: input,
  });
}
