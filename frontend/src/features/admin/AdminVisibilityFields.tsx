import { useMemo } from "react";

import {
  FIELD_CLASS,
  FIELD_INPUT_CLASS,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../../shared/ui";
import { FieldError } from "../../shared/ui/FieldError";
import { VisibilityGrantsEditor } from "../access/VisibilityGrantsEditor";
import { useAuth } from "../auth/AuthProvider";
import { isSuperAdmin, resolveUserDepartmentName, type User } from "../auth/model";
import type { VisibilityGrantInput } from "../knowledge/model";
import { useAdminOrgDepartments } from "./hooks";
import type { OrgDepartment } from "./model";

export type AdminResourceVisibilityScope = "company" | "department";

/** 优先从活跃企业部门目录解析部门中文名称，缺失时回退用户部门或原始 ID。 */
export function resolveAdminDepartmentName(
  user: User | null,
  departments: readonly OrgDepartment[] | undefined,
  departmentId: string | null | undefined,
): string | null {
  const trimmed = departmentId?.trim();
  if (!trimmed) {
    return null;
  }
  const matched = departments?.find((dept) => dept.id === trimmed);
  if (matched) {
    return matched.name;
  }
  return resolveUserDepartmentName(user, trimmed) ?? trimmed;
}

export interface AdminVisibilityFieldsProps {
  formId: string;
  open: boolean;
  disabled?: boolean;
  resourceLabel: string;
  visibilityScope: AdminResourceVisibilityScope;
  ownerDepartmentId: string;
  visibilityGrants: readonly VisibilityGrantInput[];
  ownerDepartmentError?: string;
  grantsEditorResetKey?: number;
  onScopeChange: (nextScope: AdminResourceVisibilityScope) => void;
  onDepartmentChange: (nextDepartmentId: string) => void;
  onGrantsChange: (nextGrants: VisibilityGrantInput[]) => void;
}

/**
 * 超管资源通用可见范围配置块（供 MCP Server 与子智能体弹窗共用）：
 * - 可见范围：全公司（company）/ 部门共享（department）；
 * - 选择部门共享时加载活跃部门目录，并复用 VisibilityGrantsEditor 管理跨部门与角色只读授权。
 */
export function AdminVisibilityFields({
  disabled = false,
  formId,
  grantsEditorResetKey = 0,
  onDepartmentChange,
  onGrantsChange,
  onScopeChange,
  open,
  ownerDepartmentError,
  ownerDepartmentId,
  resourceLabel,
  visibilityGrants,
  visibilityScope,
}: AdminVisibilityFieldsProps) {
  const { user } = useAuth();
  const superAdmin = isSuperAdmin(user);
  const scopeSelectId = `${formId}-visibility-scope`;
  const deptSelectId = `${formId}-owner-department`;
  const deptErrorId = `${formId}-owner-department-error`;

  // 仅当弹窗打开且选择了部门共享时按需加载活跃部门目录，避免公司级资源产生多余请求。
  const orgDepartmentsQuery = useAdminOrgDepartments(
    open && superAdmin && visibilityScope === "department",
    false,
  );
  const activeDepartments = useMemo(
    () =>
      (orgDepartmentsQuery.data?.items ?? []).filter(
        (dept) => dept.status === "active",
      ),
    [orgDepartmentsQuery.data?.items],
  );
  const ownerDepartmentName = useMemo(
    () =>
      resolveAdminDepartmentName(user, activeDepartments, ownerDepartmentId),
    [activeDepartments, ownerDepartmentId, user],
  );

  return (
    <div className="flex flex-col gap-3 rounded-[10px] border border-border-default bg-subtle p-[13px]">
      <div className={FIELD_CLASS}>
        <label htmlFor={scopeSelectId}>可见范围</label>
        <Select
          disabled={disabled}
          onValueChange={(value) =>
            onScopeChange(value as AdminResourceVisibilityScope)
          }
          value={visibilityScope}
        >
          <SelectTrigger aria-label="可见范围" id={scopeSelectId}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="company">全公司</SelectItem>
            <SelectItem value="department">部门共享</SelectItem>
          </SelectContent>
        </Select>
        <small>
          {visibilityScope === "company"
            ? `全公司活跃员工均可在可用范围中使用该${resourceLabel}。`
            : `仅归属部门及附加授权范围成员可使用该${resourceLabel}。`}
        </small>
      </div>

      {visibilityScope === "department" ? (
        <>
          <div className={FIELD_CLASS}>
            {orgDepartmentsQuery.isPending ? (
              <span>归属部门</span>
            ) : (
              <label htmlFor={deptSelectId}>归属部门</label>
            )}
            {orgDepartmentsQuery.isPending ? (
              <small role="status">正在加载企业活跃部门列表…</small>
            ) : orgDepartmentsQuery.isError ? (
              <>
                <input
                  aria-describedby={ownerDepartmentError ? deptErrorId : undefined}
                  aria-invalid={Boolean(ownerDepartmentError)}
                  aria-label="归属部门"
                  autoComplete="off"
                  className={FIELD_INPUT_CLASS}
                  disabled={disabled}
                  id={deptSelectId}
                  onChange={(event) => onDepartmentChange(event.target.value)}
                  placeholder="请输入归属部门 ID（如 dept-rd）"
                  value={ownerDepartmentId}
                />
                <small>部门目录暂不可用，已降级为手动输入部门 ID。</small>
              </>
            ) : (
              <Select
                disabled={disabled}
                onValueChange={onDepartmentChange}
                value={ownerDepartmentId}
              >
                <SelectTrigger
                  aria-describedby={ownerDepartmentError ? deptErrorId : undefined}
                  aria-label="归属部门"
                  error={Boolean(ownerDepartmentError)}
                  id={deptSelectId}
                >
                  <SelectValue placeholder="请选择归属部门" />
                </SelectTrigger>
                <SelectContent>
                  {activeDepartments.map((dept) => (
                    <SelectItem key={dept.id} value={dept.id}>
                      {dept.name}
                    </SelectItem>
                  ))}
                  {ownerDepartmentId &&
                  !activeDepartments.some(
                    (dept) => dept.id === ownerDepartmentId,
                  ) ? (
                    <SelectItem value={ownerDepartmentId}>
                      {ownerDepartmentId}
                    </SelectItem>
                  ) : null}
                </SelectContent>
              </Select>
            )}
            <FieldError error={ownerDepartmentError} id={deptErrorId} />
          </div>

          {ownerDepartmentId.trim() ? (
            <VisibilityGrantsEditor
              departments={activeDepartments}
              departmentsError={orgDepartmentsQuery.isError}
              departmentsLoading={orgDepartmentsQuery.isPending}
              disabled={disabled}
              enabled={open}
              key={grantsEditorResetKey}
              onChange={onGrantsChange}
              ownerDepartmentId={ownerDepartmentId}
              ownerDepartmentName={ownerDepartmentName}
              resourceLabel={resourceLabel}
              value={visibilityGrants}
            />
          ) : null}
        </>
      ) : null}
    </div>
  );
}
