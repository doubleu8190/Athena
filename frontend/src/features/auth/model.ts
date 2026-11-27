import type { ApiSchemas } from "../../shared/api/client";

// 直连 OpenAPI 契约类型，消除与后端 DTO 重复的手写镜像接口。
export type UserRole = ApiSchemas["UserRole"];
export type AuthProvider = ApiSchemas["AuthProvider"];
export type UserDepartment = ApiSchemas["UserDepartmentItem"];
export type User = ApiSchemas["UserResponse"];
export type AuthProvidersResponse = ApiSchemas["AuthProvidersResponse"];
export type MockUser = ApiSchemas["MockUserResponse"];
export type MockUsersResponse = ApiSchemas["MockUserListResponse"];
export type LoginInput = ApiSchemas["LoginRequest"];

const USER_ROLE_LABELS: Record<UserRole, string> = {
  super_admin: "超级管理员",
  dept_admin: "部门管理员",
  member: "员工",
};

/** 判断是否为企业超级管理员（拥有组织、模型、MCP、提示词、子智能体等全局管理权限）。 */
export function isSuperAdmin(
  user: Pick<User, "role"> | null | undefined,
): boolean {
  return user?.role === "super_admin";
}

/** 判断是否为部门管理员。 */
export function isDeptAdmin(
  user: Pick<User, "role"> | null | undefined,
): boolean {
  return user?.role === "dept_admin";
}

/** 判断是否具备技能管理台访问权限（超管管理全局与部门技能，部门管理员管理管辖部门技能）。 */
export function canManageSkills(
  user: Pick<User, "role"> | null | undefined,
): boolean {
  return user?.role === "super_admin" || user?.role === "dept_admin";
}

export function getUserRoleLabel(role: UserRole): string {
  return USER_ROLE_LABELS[role];
}

/** 优先返回主部门，无主部门标记时回退首个所属部门。 */
export function getPrimaryDepartment(
  user: Pick<User, "departments"> | null | undefined,
): UserDepartment | null {
  if (!user || user.departments.length === 0) {
    return null;
  }
  return (
    user.departments.find((department) => department.is_primary) ??
    user.departments[0] ??
    null
  );
}

/** 按部门 ID 从当前用户的所属部门清单中解析中文部门名，未命中时返回 null。 */
export function resolveUserDepartmentName(
  user: Pick<User, "departments"> | null | undefined,
  departmentId: string | null | undefined,
): string | null {
  if (!user || !departmentId) {
    return null;
  }
  return (
    user.departments.find((department) => department.id === departmentId)
      ?.name ?? null
  );
}

/** 统一计算用户安全展示名称，兼容飞书无邮箱与无显示名账号。 */
export function getUserDisplayName(
  user:
    | Pick<User, "display_name" | "email" | "employee_no">
    | null
    | undefined,
): string {
  const displayName = user?.display_name?.trim();
  if (displayName) {
    return displayName;
  }
  const emailPrefix = user?.email?.split("@")[0]?.trim();
  if (emailPrefix) {
    return emailPrefix;
  }
  const employeeNo = user?.employee_no?.trim();
  if (employeeNo) {
    return employeeNo;
  }
  return "Athena 员工";
}

export const authKeys = {
  currentUser: ["auth", "current-user"] as const,
  providers: ["auth", "providers"] as const,
  mockUsers: ["auth", "mock-users"] as const,
};
