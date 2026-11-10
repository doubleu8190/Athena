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
