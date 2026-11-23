import { useQuery } from "@tanstack/react-query";

import { listUserModels } from "./api";
import { modelKeys } from "./queryKeys";

/**
 * 读取面向普通用户的场景化模型目录（GET /api/v1/models）。
 * 目录失败时不做重试以免拖慢输入器，界面侧优雅降级为“默认模型”。
 */
export function useUserModels(enabled = true) {
  return useQuery({
    enabled,
    queryFn: listUserModels,
    queryKey: modelKeys.catalog(),
    retry: false,
    staleTime: 60_000,
  });
}
