import type { WorkerRegistryInstance } from "../types"

export type RegistryFilter = "all" | "available" | "paused" | "full" | "not_ready" | "disabled"
export type RegistryState = Exclude<RegistryFilter, "all">
export type RegistrySidebarState = "loading" | "load_failed" | "empty" | "no_matches" | "list"

/** 依据首次加载结果区分加载失败、真正空注册表和筛选无结果。 */
export function registrySidebarState(
  loading: boolean,
  loaded: boolean,
  registeredCount: number,
  visibleCount: number,
): RegistrySidebarState {
  if (!loaded) return loading ? "loading" : "load_failed"
  if (registeredCount === 0) return "empty"
  return visibleCount === 0 ? "no_matches" : "list"
}

/** 表示实例身份探测的现有证据，不把默认值解释成探测成功。 */
export function registryIdentityMessage(identityHasNoMismatch: boolean): string {
  return identityHasNoMismatch ? "未发现身份不匹配" : "身份不匹配"
}

/** 将健康、接单与容量字段投影为页面筛选使用的单一状态。 */
export function registryState(instance: WorkerRegistryInstance): RegistryState {
  if (instance.status === "disabled") return "disabled"
  if (instance.status !== "ready" || !instance.health_identity_valid) return "not_ready"
  if (!instance.accepting_tasks) return "paused"
  if (instance.current_concurrency >= instance.max_concurrency) return "full"
  return "available"
}

export function registryStateLabel(state: RegistryState): string {
  return {
    available: "可接单",
    paused: "暂停接单",
    full: "容量已满",
    not_ready: "不可用",
    disabled: "已禁用",
  }[state]
}

export function registryStateTone(state: RegistryState): "success" | "warning" | "danger" | "default" {
  if (state === "available") return "success"
  if (state === "paused" || state === "full") return "warning"
  if (state === "disabled") return "default"
  return "danger"
}
