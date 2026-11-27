import type { VisibilityGrantInput } from "../knowledge/model";

export function grantKey(
  grant: Pick<VisibilityGrantInput, "target_id" | "target_type">,
): string {
  return `${grant.target_type}:${grant.target_id.trim()}`;
}

/** 构造归属部门默认授权行（默认包含下级子部门由角色策略决定，不可删除）。 */
export function defaultOwnerGrant(
  ownerDepartmentId: string,
  includeSubdepartments = true,
): VisibilityGrantInput {
  return {
    include_subdepartments: includeSubdepartments,
    target_id: ownerDepartmentId.trim(),
    target_type: "department",
  };
}

/**
 * 规范化并去重可见性授权列表：
 * 1. 始终锁定归属部门为首项（若入参已包含归属部门则保留其含子部门值，缺失时采用 defaultIncludeSubdepartments）；
 * 2. 按 (target_type, target_id) 自动去重，避免提交触发 422 visibility_grant_duplicate；
 * 3. role 类型授权强制 include_subdepartments=false，与后端不变式一致。
 */
export function normalizeVisibilityGrants(
  ownerDepartmentId: string,
  grants?: readonly VisibilityGrantInput[] | null,
  defaultIncludeSubdepartments = true,
): VisibilityGrantInput[] {
  const trimmedOwnerId = ownerDepartmentId.trim();
  if (!trimmedOwnerId) {
    return [];
  }

  let ownerIncludeSub = defaultIncludeSubdepartments;
  const extras: VisibilityGrantInput[] = [];
  const seen = new Set<string>([`department:${trimmedOwnerId}`]);

  for (const raw of grants ?? []) {
    const targetId = raw.target_id.trim();
    if (!targetId) {
      continue;
    }
    if (raw.target_type === "department" && targetId === trimmedOwnerId) {
      ownerIncludeSub = Boolean(raw.include_subdepartments);
      continue;
    }
    const key = `${raw.target_type}:${targetId}`;
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    extras.push({
      include_subdepartments:
        raw.target_type === "role" ? false : Boolean(raw.include_subdepartments),
      target_id: targetId,
      target_type: raw.target_type,
    });
  }

  return [defaultOwnerGrant(trimmedOwnerId, ownerIncludeSub), ...extras];
}

/** 比较两份授权集合是否语义等价（忽略顺序差异），供详情页精确判断表单脏状态。 */
export function visibilityGrantsEqual(
  left: readonly VisibilityGrantInput[],
  right: readonly VisibilityGrantInput[],
): boolean {
  if (left.length !== right.length) {
    return false;
  }
  const sortGrants = (items: readonly VisibilityGrantInput[]) =>
    [...items].sort((a, b) => grantKey(a).localeCompare(grantKey(b)));
  const sortedLeft = sortGrants(left);
  const sortedRight = sortGrants(right);
  return sortedLeft.every(
    (item, index) =>
      item.target_type === sortedRight[index]?.target_type &&
      item.target_id.trim() === sortedRight[index]?.target_id.trim() &&
      item.include_subdepartments ===
        sortedRight[index]?.include_subdepartments,
  );
}

/** 判断当前授权是否仍与角色默认策略完全一致，用于新建提交时安全省略 visibility_grants。 */
export function isDefaultVisibilityGrants(
  ownerDepartmentId: string,
  grants: readonly VisibilityGrantInput[],
  defaultIncludeSubdepartments = true,
): boolean {
  const trimmedOwnerId = ownerDepartmentId.trim();
  if (!trimmedOwnerId) {
    return true;
  }
  const normalized = normalizeVisibilityGrants(
    trimmedOwnerId,
    grants,
    defaultIncludeSubdepartments,
  );
  return (
    normalized.length === 1 &&
    normalized[0].target_type === "department" &&
    normalized[0].target_id === trimmedOwnerId &&
    normalized[0].include_subdepartments === defaultIncludeSubdepartments
  );
}
