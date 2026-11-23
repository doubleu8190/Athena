export interface UserModelItem {
  id: string;
  name: string;
  scenario_label: string | null;
  scenario_description: string | null;
  is_default: boolean;
  supports_vision: boolean;
}

export interface UserModelListResponse {
  items: UserModelItem[];
  default_model_id: string | null;
}

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
