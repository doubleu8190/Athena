import { useEffect, useMemo } from "react";

import {
  FIELD_CLASS,
  FIELD_INPUT_CLASS,
  FIELD_SELECT_CLASS,
  VisibilityBadge,
  type VisibilityScope,
} from "../../shared/ui";
import type { OrgDepartment } from "../admin/model";
import { useAuth } from "../auth/AuthProvider";
import {
  isDeptAdmin,
  isSuperAdmin,
  resolveUserDepartmentName,
  type User,
} from "../auth/model";

function allowedScopesForUser(user: User | null): VisibilityScope[] {
  if (isSuperAdmin(user)) {
    return ["personal", "department", "company"];
  }
  if (isDeptAdmin(user)) {
    return ["personal", "department"];
  }
  return ["personal"];
}

function scopeOptionLabel(scope: VisibilityScope): string {
  switch (scope) {
    case "company":
      return "全公司";
    case "department":
      return "部门共享";
    case "personal":
      return "个人";
  }
}

/** 优先从活跃组织部门目录解析归属部门名称，缺失时回退当前用户的部门信息 */
export function resolveOwnerDepartmentName(
  user: User | null,
  activeOrgDepartments: readonly OrgDepartment[],
  ownerDepartmentId: string,
): string | null {
  const trimmed = ownerDepartmentId.trim();
  if (!trimmed) {
    return null;
  }
  const fromOrg = activeOrgDepartments.find((dept) => dept.id === trimmed);
  if (fromOrg) {
    return fromOrg.name;
  }
  return resolveUserDepartmentName(user, trimmed) ?? trimmed;
}

interface VisibilityFieldsProps {
  departmentError: string | null;
  departmentId: string;
  disabled: boolean;
  formId: string;
  onDepartmentChange: (value: string) => void;
  onScopeChange: (value: VisibilityScope) => void;
  open: boolean;
  orgDepartments: readonly OrgDepartment[];
  orgDepartmentsError: boolean;
  orgDepartmentsLoading: boolean;
  scope: VisibilityScope;
}

/**
 * 知识库可见范围与归属部门字段组：
 * - member：固定为个人知识库（只读展示）；
 * - dept_admin：可选个人 / 部门共享，部门列表仅限本人 managed_department_ids；
 * - super_admin：可选个人 / 部门共享 / 全公司，与外层弹窗及 VisibilityGrantsEditor 共享同一次组织部门查询结果，失败安全降级为手工输入部门 ID。
 */
export function KnowledgeVisibilityFields({
  departmentError,
  departmentId,
  disabled,
  formId,
  onDepartmentChange,
  onScopeChange,
  open,
  orgDepartments,
  orgDepartmentsError,
  orgDepartmentsLoading,
  scope,
}: VisibilityFieldsProps) {
  const { user } = useAuth();
  const scopeSelectId = `${formId}-visibility-scope`;
  const deptSelectId = `${formId}-owner-department`;
  const deptErrorId = `${formId}-owner-department-error`;
  const superAdmin = isSuperAdmin(user);
  const deptAdmin = isDeptAdmin(user);
  const allowedScopes = useMemo(() => allowedScopesForUser(user), [user]);

  const managedDeptOptions = useMemo(() => {
    if (!user) {
      return [];
    }
    return user.managed_department_ids.map((id) => ({
      id,
      name: resolveUserDepartmentName(user, id) ?? id,
    }));
  }, [user]);

  // dept_admin 仅管辖 1 个部门时自动预选，减少重复操作。
  useEffect(() => {
    if (
      open &&
      deptAdmin &&
      scope === "department" &&
      !departmentId &&
      managedDeptOptions.length === 1
    ) {
      onDepartmentChange(managedDeptOptions[0].id);
    }
  }, [
    deptAdmin,
    departmentId,
    managedDeptOptions,
    onDepartmentChange,
    open,
    scope,
  ]);

  if (allowedScopes.length === 1) {
    return (
      <div className={FIELD_CLASS}>
        <span>可见范围</span>
        <div className="flex items-center gap-2 pt-0.5">
          <VisibilityBadge scope="personal" />
          <small className="text-xs text-text-tertiary">
            当前角色仅支持创建与维护个人知识库。
          </small>
        </div>
      </div>
    );
  }

  const noManagedDepartments = deptAdmin && managedDeptOptions.length === 0;
  const hasDepartmentControl =
    !noManagedDepartments && !(superAdmin && orgDepartmentsLoading);

  return (
    <>
      <label className={FIELD_CLASS} htmlFor={scopeSelectId}>
        <span>可见范围</span>
        <select
          aria-label="可见范围"
          className={FIELD_SELECT_CLASS}
          disabled={disabled}
          id={scopeSelectId}
          onChange={(event) =>
            onScopeChange(event.target.value as VisibilityScope)
          }
          value={scope}
        >
          {allowedScopes.map((option) => (
            <option key={option} value={option}>
              {scopeOptionLabel(option)}
            </option>
          ))}
        </select>
        <small>
          {scope === "company"
            ? "全公司活跃员工均可检索，仅超级管理员可维护。"
            : scope === "department"
              ? "默认向归属部门成员开放，支持按需追加跨部门或按角色只读共享；归属部门管理员与超管可维护。"
              : "仅本人可见、检索与维护。"}
        </small>
      </label>

      {scope === "department" ? (
        <div className={FIELD_CLASS}>
          {/* 仅在渲染实际表单控件时绑定 htmlFor，避免无控件状态出现悬空 label。 */}
          {hasDepartmentControl ? (
            <label htmlFor={deptSelectId}>归属部门</label>
          ) : (
            <span>归属部门</span>
          )}
          {noManagedDepartments ? (
            <p
              className="field-error"
              id={`${formId}-no-managed-department`}
              role="alert"
            >
              当前账号未分配可管辖部门，无法创建或修改部门共享知识库。
            </p>
          ) : deptAdmin ? (
            <select
              aria-describedby={departmentError ? deptErrorId : undefined}
              aria-invalid={departmentError ? true : undefined}
              aria-label="归属部门"
              className={FIELD_SELECT_CLASS}
              disabled={disabled}
              id={deptSelectId}
              onChange={(event) => onDepartmentChange(event.target.value)}
              value={departmentId}
            >
              <option value="">请选择归属部门</option>
              {managedDeptOptions.map((dept) => (
                <option key={dept.id} value={dept.id}>
                  {dept.name}
                </option>
              ))}
            </select>
          ) : orgDepartmentsLoading ? (
            <small role="status">正在加载企业活跃部门列表…</small>
          ) : orgDepartmentsError ? (
            <>
              <input
                aria-describedby={departmentError ? deptErrorId : undefined}
                aria-invalid={departmentError ? true : undefined}
                aria-label="归属部门"
                autoComplete="off"
                className={FIELD_INPUT_CLASS}
                disabled={disabled}
                id={deptSelectId}
                onChange={(event) => onDepartmentChange(event.target.value)}
                placeholder="请输入归属部门 ID（如 dept_mock_fin）"
                value={departmentId}
              />
              <small>部门目录暂不可用，已降级为手动输入部门 ID。</small>
            </>
          ) : (
            <select
              aria-describedby={departmentError ? deptErrorId : undefined}
              aria-invalid={departmentError ? true : undefined}
              aria-label="归属部门"
              className={FIELD_SELECT_CLASS}
              disabled={disabled}
              id={deptSelectId}
              onChange={(event) => onDepartmentChange(event.target.value)}
              value={departmentId}
            >
              <option value="">请选择归属部门</option>
              {orgDepartments.map((dept) => (
                <option key={dept.id} value={dept.id}>
                  {dept.name}
                </option>
              ))}
              {departmentId &&
              !orgDepartments.some((dept) => dept.id === departmentId) ? (
                <option value={departmentId}>{departmentId}</option>
              ) : null}
            </select>
          )}

          {!noManagedDepartments && departmentError ? (
            <p className="field-error" id={deptErrorId} role="alert">
              {departmentError}
            </p>
          ) : null}
        </div>
      ) : null}
    </>
  );
}
