import { apiRequest } from "../../shared/api/client";
import type { LoginInput, RegisterInput, User } from "./model";

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

export function register(input: RegisterInput): Promise<User> {
  return apiRequest<User>("/auth/register", {
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
