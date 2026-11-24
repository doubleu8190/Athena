import { apiRequest } from "../../shared/api/client";
import type {
  CreateUserMemoryInput,
  UpdateUserMemoryInput,
  UpdateUserWorkProfileInput,
  UserMemory,
  UserMemoryListResponse,
  UserWorkProfile,
} from "./model";

export function getUserPreferences(): Promise<UserWorkProfile> {
  return apiRequest<UserWorkProfile>("/me/preferences", { method: "GET" });
}

export function updateUserPreferences(
  input: UpdateUserWorkProfileInput,
): Promise<UserWorkProfile> {
  return apiRequest<UserWorkProfile>("/me/preferences", {
    body: input,
    method: "PUT",
  });
}

export function listUserMemories(): Promise<UserMemoryListResponse> {
  return apiRequest<UserMemoryListResponse>("/me/memories", {
    method: "GET",
  });
}

export function createUserMemory(
  input: CreateUserMemoryInput,
): Promise<UserMemory> {
  return apiRequest<UserMemory>("/me/memories", {
    body: input,
    method: "POST",
  });
}

export function updateUserMemory(
  memoryId: string,
  input: UpdateUserMemoryInput,
): Promise<UserMemory> {
  return apiRequest<UserMemory>(
    `/me/memories/${encodeURIComponent(memoryId)}`,
    {
      body: input,
      method: "PATCH",
    },
  );
}

export function deleteUserMemory(memoryId: string): Promise<void> {
  return apiRequest<void>(`/me/memories/${encodeURIComponent(memoryId)}`, {
    method: "DELETE",
  });
}
