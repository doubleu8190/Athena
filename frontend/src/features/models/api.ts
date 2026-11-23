import { apiRequest } from "../../shared/api/client";
import type { UserModelListResponse } from "./model";

export function listUserModels(): Promise<UserModelListResponse> {
  return apiRequest<UserModelListResponse>("/models", { method: "GET" });
}
