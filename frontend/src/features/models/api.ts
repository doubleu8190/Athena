import { apiGet } from "../../shared/api/client";
import type { UserModelListResponse } from "./model";

export function listUserModels(): Promise<UserModelListResponse> {
  return apiGet("/api/v1/models");
}
