import { apiRequest } from "../../shared/api/client";
import type {
  AuthProvidersResponse,
  LoginInput,
  MockUsersResponse,
  User,
} from "./model";
import { sanitizeReturnTo } from "./returnTo";

export function getAuthProviders(): Promise<AuthProvidersResponse> {
  return apiRequest<AuthProvidersResponse>("/auth/providers", {
    method: "GET",
    cache: "no-store",
  });
}

export function listMockUsers(): Promise<MockUsersResponse> {
  return apiRequest<MockUsersResponse>("/auth/mock/users", {
    method: "GET",
    cache: "no-store",
  });
}

export function mockLogin(userId: string): Promise<User> {
  return apiRequest<User>("/auth/mock/login", {
    method: "POST",
    body: { user_id: userId },
  });
}

/**
 * 构造飞书 OAuth 授权入口 URL；return_to 必须经过站内安全路径校验后再编码，
 * 拦截外链、协议相对路径及 /login、/register 跳转死循环。
 */
export function buildFeishuAuthorizeUrl(returnTo: string): string {
  const safeReturnTo = sanitizeReturnTo(returnTo);
  return `/api/v1/auth/feishu/authorize?return_to=${encodeURIComponent(safeReturnTo)}`;
}

export function getCurrentUser(): Promise<User> {
  return apiRequest<User>("/auth/me", {
    method: "GET",
    cache: "no-store",
  });
}

export function login(input: LoginInput): Promise<User> {
  return apiRequest<User>("/auth/login", {
    method: "POST",
    body: input,
  });
}

export function logout(): Promise<void> {
  return apiRequest<void>("/auth/logout", {
    method: "POST",
  });
}

export function updateCurrentUser(displayName: string): Promise<User> {
  return apiRequest<User>("/auth/me", {
    method: "PATCH",
    body: { display_name: displayName },
  });
}

export function changePassword(input: {
  current_password: string;
  new_password: string;
}): Promise<void> {
  return apiRequest<void>("/auth/password", {
    method: "POST",
    body: input,
  });
}
