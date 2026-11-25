import { apiDelete, apiGet, apiPatch, apiPost, apiPut } from "../../shared/api/client";
import type {
  CreateUserMemoryInput,
  UpdateUserMemoryInput,
  UpdateUserWorkProfileInput,
  UserMemory,
  UserMemoryListResponse,
  UserWorkProfile,
} from "./model";

export function getUserPreferences(): Promise<UserWorkProfile> {
  return apiGet("/api/v1/me/preferences");
}

export function updateUserPreferences(
  input: UpdateUserWorkProfileInput,
): Promise<UserWorkProfile> {
  return apiPut("/api/v1/me/preferences", { body: input });
}

export function listUserMemories(): Promise<UserMemoryListResponse> {
  return apiGet("/api/v1/me/memories");
}

export function createUserMemory(
  input: CreateUserMemoryInput,
): Promise<UserMemory> {
  return apiPost("/api/v1/me/memories", { body: input });
}

export function updateUserMemory(
  memoryId: string,
  input: UpdateUserMemoryInput,
): Promise<UserMemory> {
  return apiPatch("/api/v1/me/memories/{memory_id}", {
    body: input,
    params: { path: { memory_id: memoryId } },
  });
}

export function deleteUserMemory(memoryId: string): Promise<void> {
  return apiDelete("/api/v1/me/memories/{memory_id}", {
    params: { path: { memory_id: memoryId } },
  });
}
