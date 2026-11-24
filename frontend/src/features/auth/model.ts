export type UserRole = "super_admin" | "dept_admin" | "member";

export type AuthProvider = "local" | "feishu" | "mock";

export interface UserDepartment {
  id: string;
  name: string;
  path: string;
  is_primary: boolean;
}

export interface User {
  id: string;
  /** 飞书同步账号可能无企业邮箱，界面展示时必须安全判空。 */
  email: string | null;
  display_name: string | null;
  created_at: string;
  role: UserRole;
  auth_provider: AuthProvider;
  employee_no: string | null;
  job_title: string | null;
  avatar_url: string | null;
  departments: UserDepartment[];
  managed_department_ids: string[];
}

export interface AuthProvidersResponse {
  feishu_enabled: boolean;
  local_login_enabled: boolean;
  mock_enabled: boolean;
}

export interface MockUser {
  id: string;
  display_name: string | null;
  email: string | null;
  role: UserRole;
  employee_no: string | null;
  job_title: string | null;
  avatar_url: string | null;
  departments: UserDepartment[];
  managed_department_ids: string[];
}

export interface MockUsersResponse {
  items: MockUser[];
}

export interface LoginInput {
  email: string;
  password: string;
  remember_me: boolean;
}

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
