import type { ApiSchemas } from "../../shared/api/client";

// 直连 OpenAPI 用户侧模型目录契约。
export type UserModelItem = ApiSchemas["UserModelCatalogItemResponse"];
export type UserModelListResponse = ApiSchemas["UserModelCatalogResponse"];

/**
 * 计算面向普通用户的场景化模型展示名：
 * 优先展示管理员配置的场景标签，未配置时回退为模型展示名称。
 */
export function userModelDisplayLabel(
  model: Pick<UserModelItem, "name" | "scenario_label">,
): string {
  return model.scenario_label?.trim() || model.name;
}

/**
 * 解析当前轮次实际生效的模型对象：
 * 显式选择 > 后端声明的 default_model_id > 标记 is_default 的模型 > 首项。
 */
export function resolveEffectiveUserModel(
  catalog: UserModelListResponse | undefined,
  selectedModelProfileId: string | null,
): UserModelItem | null {
  if (!catalog || catalog.items.length === 0) {
    return null;
  }
  if (selectedModelProfileId) {
    const selected = catalog.items.find(
      (item) => item.id === selectedModelProfileId,
    );
    if (selected) {
      return selected;
    }
  }
  if (catalog.default_model_id) {
    const byDefaultId = catalog.items.find(
      (item) => item.id === catalog.default_model_id,
    );
    if (byDefaultId) {
      return byDefaultId;
    }
  }
  return (
    catalog.items.find((item) => item.is_default) ?? catalog.items[0] ?? null
  );
}
