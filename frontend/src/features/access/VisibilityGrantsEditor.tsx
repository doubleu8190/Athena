import { Building2, Lock, Plus, Shield, Trash2 } from "lucide-react";
import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";

import {
  Badge,
  Button,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  cn,
} from "../../shared/ui";
import { useAdminOrgDepartments } from "../admin/hooks";
import type { OrgDepartment } from "../admin/model";
import { useAuth } from "../auth/AuthProvider";
import {
  getUserRoleLabel,
  isDeptAdmin,
  isSuperAdmin,
  resolveUserDepartmentName,
  type UserRole,
} from "../auth/model";
import type {
  VisibilityGrantInput,
  VisibilityGrantTargetType,
} from "../knowledge/model";
import {
  defaultOwnerGrant,
  grantKey,
  isDefaultVisibilityGrants,
  normalizeVisibilityGrants,
  visibilityGrantsEqual,
} from "./visibilityGrants";

export {
  defaultOwnerGrant,
  isDefaultVisibilityGrants,
  normalizeVisibilityGrants,
  visibilityGrantsEqual,
};

const VISIBILITY_GRANT_TARGET_MAX_LENGTH = 64;
// 显式拦截 C0 控制字符与 DEL，与后端 visibility_grant_target_invalid 边界保持一致。
// eslint-disable-next-line no-control-regex
const INVALID_GRANT_TARGET_CONTROL = /[\u0000-\u001f\u007f]/u;

const ROLE_OPTIONS: ReadonlyArray<{ label: string; value: UserRole }> = [
  { label: getUserRoleLabel("member"), value: "member" },
  { label: getUserRoleLabel("dept_admin"), value: "dept_admin" },
  { label: getUserRoleLabel("super_admin"), value: "super_admin" },
];

function roleLabel(roleId: string): string {
  if (
    roleId === "member" ||
    roleId === "dept_admin" ||
    roleId === "super_admin"
  ) {
    return getUserRoleLabel(roleId);
  }
  return roleId;
}

export interface VisibilityGrantsEditorProps {
  /** 仅用于 scope === "department" 且已选定归属部门的资源；为空时不渲染。 */
  ownerDepartmentId: string;
  ownerDepartmentName?: string | null;
  value: readonly VisibilityGrantInput[];
  onChange: (nextGrants: VisibilityGrantInput[]) => void;
  /**
   * 归属部门缺省时的「包含下级子部门」默认策略：
   * super_admin 默认 true，dept_admin 默认 false（避免直属管辖部门管理员误触 403 visibility_grant_forbidden）。
   */
  defaultIncludeSubdepartments?: boolean;
  /** 可选由外层传入已拉取的企业活跃部门列表，避免同一弹窗重复请求 /admin/org/departments。 */
  departments?: readonly OrgDepartment[];
  departmentsLoading?: boolean;
  departmentsError?: boolean;
  disabled?: boolean;
  /** 弹窗/表单处于激活态时允许 super_admin 按需加载部门目录。 */
  enabled?: boolean;
  /** 资源中文名称（如「技能」「知识库」），便于多模块复用。 */
  resourceLabel?: string;
  className?: string;
}

/**
 * 可复用跨部门/角色附加授权选择器（供技能双形态发布与知识库管理共用）：
 * - 默认锁定归属部门授权行（支持切换含子部门、禁止删除）；
 * - super_admin 可追加任意企业活跃部门（含子部门开关）或角色（include_subdepartments 恒为 false）；
 * - dept_admin 严格受控于后端 403 visibility_grant_forbidden 边界，仅允许追加本人管辖内的其他部门，
 *   不渲染角色授权入口且默认不开启「包含下级子部门」（有子树权限者可按需勾选）。
 */
export function VisibilityGrantsEditor({
  className,
  defaultIncludeSubdepartments,
  departments,
  departmentsError = false,
  departmentsLoading = false,
  disabled = false,
  enabled = true,
  onChange,
  ownerDepartmentId,
  ownerDepartmentName,
  resourceLabel = "资源",
  value,
}: VisibilityGrantsEditorProps) {
  const { user } = useAuth();
  const superAdmin = isSuperAdmin(user);
  const deptAdmin = isDeptAdmin(user);
  const resolvedDefaultIncludeSub = defaultIncludeSubdepartments ?? superAdmin;

  const trimmedOwnerId = ownerDepartmentId.trim();
  const shouldFetchDepartments =
    enabled &&
    Boolean(trimmedOwnerId) &&
    superAdmin &&
    departments === undefined;
  const orgDepartmentsQuery = useAdminOrgDepartments(
    shouldFetchDepartments,
    false,
  );

  const resolvedOrgDepartments = useMemo(() => {
    const source = departments ?? orgDepartmentsQuery.data?.items ?? [];
    return source.filter((dept) => dept.status === "active");
  }, [departments, orgDepartmentsQuery.data?.items]);

  const isLoadingDepartments =
    departments !== undefined
      ? departmentsLoading
      : orgDepartmentsQuery.isPending;
  const hasDepartmentLoadError =
    departments !== undefined ? departmentsError : orgDepartmentsQuery.isError;

  const managedDeptOptions = useMemo(() => {
    if (!user) {
      return [];
    }
    return user.managed_department_ids.map((id) => ({
      id,
      name: resolveUserDepartmentName(user, id) ?? id,
    }));
  }, [user]);

  const normalizedGrants = useMemo(
    () =>
      normalizeVisibilityGrants(
        trimmedOwnerId,
        value,
        resolvedDefaultIncludeSub,
      ),
    [resolvedDefaultIncludeSub, trimmedOwnerId, value],
  );
  const ownerGrant = normalizedGrants[0] ?? null;
  const additionalGrants = normalizedGrants.slice(1);
  const existingKeys = useMemo(
    () => new Set(normalizedGrants.map((grant) => grantKey(grant))),
    [normalizedGrants],
  );

  const [newTargetType, setNewTargetType] =
    useState<VisibilityGrantTargetType>("department");
  const [newTargetId, setNewTargetId] = useState("");
  // super_admin 追加部门默认含子部门；dept_admin 默认不含子部门以规避直属管辖账号误触 403。
  const [newIncludeSub, setNewIncludeSub] = useState<boolean>(superAdmin);
  const [duplicateNotice, setDuplicateNotice] = useState<string | null>(null);
  const [inputError, setInputError] = useState<string | null>(null);

  const ownerSubCheckboxId = useId();
  const targetTypeId = useId();
  const targetValueId = useId();
  const newSubCheckboxId = useId();
  const duplicateNoticeId = useId();
  const inputErrorId = useId();

  const prevOwnerIdRef = useRef<string>(trimmedOwnerId);
  const valueRef = useRef(value);
  const onChangeRef = useRef(onChange);
  valueRef.current = value;
  onChangeRef.current = onChange;

  // 角色状态变更时同步默认含子部门开关，并在非 super_admin 下强制锁定仅允许部门授权。
  useEffect(() => {
    setNewIncludeSub(superAdmin);
    if (!superAdmin) {
      setNewTargetType("department");
      setNewTargetId("");
    }
  }, [superAdmin]);

  // 归属部门变化时过滤旧归属部门条目、保留手工追加项并按新归属部门去重规范化同步回父级，防止残留旧归属与父子失步。
  useEffect(() => {
    const prevOwnerId = prevOwnerIdRef.current;
    prevOwnerIdRef.current = trimmedOwnerId;
    setNewTargetId("");
    setDuplicateNotice(null);
    setInputError(null);

    if (!trimmedOwnerId || !prevOwnerId || prevOwnerId === trimmedOwnerId) {
      return;
    }

    const currentValue = valueRef.current;
    const filteredWithoutPrevOwner = currentValue.filter(
      (grant) =>
        !(
          grant.target_type === "department" &&
          grant.target_id.trim() === prevOwnerId
        ),
    );
    const next = normalizeVisibilityGrants(
      trimmedOwnerId,
      filteredWithoutPrevOwner,
      resolvedDefaultIncludeSub,
    );
    if (!visibilityGrantsEqual(next, currentValue)) {
      onChangeRef.current(next);
    }
  }, [resolvedDefaultIncludeSub, trimmedOwnerId]);

  const departmentNameMap = useMemo(() => {
    const map = new Map<string, string>();
    for (const dept of resolvedOrgDepartments) {
      map.set(dept.id, dept.name);
    }
    for (const dept of user?.departments ?? []) {
      if (!map.has(dept.id)) {
        map.set(dept.id, dept.name);
      }
    }
    if (trimmedOwnerId && ownerDepartmentName?.trim()) {
      map.set(trimmedOwnerId, ownerDepartmentName.trim());
    }
    return map;
  }, [
    ownerDepartmentName,
    resolvedOrgDepartments,
    trimmedOwnerId,
    user?.departments,
  ]);

  const resolveDepartmentDisplay = (departmentId: string): string =>
    departmentNameMap.get(departmentId) ??
    resolveUserDepartmentName(user, departmentId) ??
    departmentId;

  const candidateDepartmentOptions = useMemo(() => {
    const source = superAdmin
      ? resolvedOrgDepartments.map((dept) => ({ id: dept.id, name: dept.name }))
      : managedDeptOptions;
    return source.filter((dept) => !existingKeys.has(`department:${dept.id}`));
  }, [existingKeys, managedDeptOptions, resolvedOrgDepartments, superAdmin]);

  const candidateRoleOptions = useMemo(() => {
    if (!superAdmin) {
      return [];
    }
    return ROLE_OPTIONS.filter(
      (role) => !existingKeys.has(`role:${role.value}`),
    );
  }, [existingKeys, superAdmin]);

  if (!trimmedOwnerId || !ownerGrant) {
    return null;
  }

  const handleToggleOwnerSub = (checked: boolean) => {
    if (disabled) {
      return;
    }
    const next = normalizeVisibilityGrants(
      trimmedOwnerId,
      [defaultOwnerGrant(trimmedOwnerId, checked), ...additionalGrants],
      resolvedDefaultIncludeSub,
    );
    onChange(next);
  };

  const handleToggleGrantSub = (targetId: string, checked: boolean) => {
    if (disabled) {
      return;
    }
    const next = normalizeVisibilityGrants(
      trimmedOwnerId,
      normalizedGrants.map((grant) =>
        grant.target_type === "department" && grant.target_id === targetId
          ? { ...grant, include_subdepartments: checked }
          : grant,
      ),
      resolvedDefaultIncludeSub,
    );
    onChange(next);
  };

  const handleRemoveGrant = (grantToRemove: VisibilityGrantInput) => {
    if (disabled) {
      return;
    }
    if (
      grantToRemove.target_type === "department" &&
      grantToRemove.target_id === trimmedOwnerId
    ) {
      return;
    }
    const keyToRemove = grantKey(grantToRemove);
    const next = normalizeVisibilityGrants(
      trimmedOwnerId,
      normalizedGrants.filter((grant) => grantKey(grant) !== keyToRemove),
      resolvedDefaultIncludeSub,
    );
    setDuplicateNotice(null);
    setInputError(null);
    onChange(next);
  };

  const handleAddGrant = () => {
    if (disabled) {
      return;
    }
    const effectiveType: VisibilityGrantTargetType = superAdmin
      ? newTargetType
      : "department";
    const trimmedTarget = newTargetId.trim();
    if (!trimmedTarget) {
      return;
    }
    if (Array.from(trimmedTarget).length > VISIBILITY_GRANT_TARGET_MAX_LENGTH) {
      setInputError(
        `授权目标标识不能超过 ${VISIBILITY_GRANT_TARGET_MAX_LENGTH} 个字符。`,
      );
      setDuplicateNotice(null);
      return;
    }
    if (INVALID_GRANT_TARGET_CONTROL.test(trimmedTarget)) {
      setInputError("授权目标标识不能包含控制字符或换行符。");
      setDuplicateNotice(null);
      return;
    }
    const key = `${effectiveType}:${trimmedTarget}`;
    if (existingKeys.has(key)) {
      setDuplicateNotice("该共享对象已在授权列表中，已自动去重。");
      setInputError(null);
      return;
    }
    setDuplicateNotice(null);
    setInputError(null);
    const next = normalizeVisibilityGrants(
      trimmedOwnerId,
      [
        ...normalizedGrants,
        {
          include_subdepartments:
            effectiveType === "role" ? false : Boolean(newIncludeSub),
          target_id: trimmedTarget,
          target_type: effectiveType,
        },
      ],
      resolvedDefaultIncludeSub,
    );
    setNewTargetId("");
    onChange(next);
  };

  const handleManualInputKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter") {
      // 阻止回车冒泡触发表单整体提交，转为就地添加附加授权条目。
      event.preventDefault();
      handleAddGrant();
    }
  };

  const useManualDepartmentInput =
    superAdmin && newTargetType === "department" && hasDepartmentLoadError;
  const noAvailableDeptAdminTargets =
    deptAdmin && candidateDepartmentOptions.length === 0;
  const noAvailableSuperAdminTargets =
    superAdmin &&
    !useManualDepartmentInput &&
    !isLoadingDepartments &&
    ((newTargetType === "department" &&
      candidateDepartmentOptions.length === 0) ||
      (newTargetType === "role" && candidateRoleOptions.length === 0));

  const targetDescribedBy = [
    inputError ? inputErrorId : undefined,
    duplicateNotice ? duplicateNoticeId : undefined,
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <section
      aria-label="附加共享范围配置"
      className={cn(
        "rounded-xl border border-border-default bg-subtle/45 p-3.5",
        className,
      )}
      data-testid="visibility-grants-editor"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h4 className="text-xs font-semibold text-text-primary">
            可见性与附加共享范围
          </h4>
          <p className="mt-0.5 text-xs text-text-tertiary">
            默认向归属部门开放该{resourceLabel}；可按需追加跨部门
            {superAdmin ? "或按角色" : ""}只读共享。
          </p>
        </div>
        <Badge variant="neutral">
          共 {normalizedGrants.length} 条授权
        </Badge>
      </div>

      {/* 默认锁定行：归属部门不可删除，确保符合后端默认归属可见策略 */}
      <div className="mt-3 space-y-2">
        <div
          className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-brand-border bg-canvas px-3 py-2"
          data-grant-owner="true"
        >
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <Building2
              aria-hidden="true"
              className="h-3.5 w-3.5 shrink-0 text-brand"
            />
            <span className="truncate text-xs font-medium text-text-primary">
              {resolveDepartmentDisplay(trimmedOwnerId)}
            </span>
            <Badge className="px-1.5 py-0 text-[11px]" variant="brand">
              <Lock aria-hidden="true" className="h-3 w-3" />
              归属部门（默认）
            </Badge>
          </div>
          <label
            className="inline-flex cursor-pointer items-center gap-1.5 text-xs text-text-secondary select-none"
            htmlFor={ownerSubCheckboxId}
          >
            <input
              checked={ownerGrant.include_subdepartments}
              className="h-3.5 w-3.5 rounded border-border-strong accent-brand"
              disabled={disabled}
              id={ownerSubCheckboxId}
              onChange={(event) => handleToggleOwnerSub(event.target.checked)}
              type="checkbox"
            />
            <span>包含下级子部门</span>
          </label>
        </div>

        {/* 附加共享授权列表 */}
        {additionalGrants.length > 0 ? (
          <ul
            aria-label="已配置的附加共享范围"
            className="m-0 list-none space-y-2 p-0"
          >
            {additionalGrants.map((grant) => {
              const isRole = grant.target_type === "role";
              const targetDisplay = isRole
                ? roleLabel(grant.target_id)
                : resolveDepartmentDisplay(grant.target_id);
              const subCheckboxId = `${ownerSubCheckboxId}-extra-${grant.target_type}-${grant.target_id}`;
              return (
                <li
                  className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border-default bg-canvas px-3 py-2"
                  key={grantKey(grant)}
                >
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    {isRole ? (
                      <Shield
                        aria-hidden="true"
                        className="h-3.5 w-3.5 shrink-0 text-info"
                      />
                    ) : (
                      <Building2
                        aria-hidden="true"
                        className="h-3.5 w-3.5 shrink-0 text-text-secondary"
                      />
                    )}
                    <Badge
                      className="px-1.5 py-0 text-[11px]"
                      variant={isRole ? "info" : "neutral"}
                    >
                      {isRole ? "角色" : "附加部门"}
                    </Badge>
                    <span className="truncate text-xs font-medium text-text-primary">
                      {targetDisplay}
                    </span>
                  </div>

                  <div className="flex items-center gap-3">
                    {isRole ? (
                      <span className="text-[11px] text-text-tertiary">
                        按系统角色生效
                      </span>
                    ) : (
                      <label
                        className="inline-flex cursor-pointer items-center gap-1.5 text-xs text-text-secondary select-none"
                        htmlFor={subCheckboxId}
                      >
                        <input
                          aria-label={`附加部门 ${targetDisplay} 包含下级子部门`}
                          checked={grant.include_subdepartments}
                          className="h-3.5 w-3.5 rounded border-border-strong accent-brand"
                          disabled={disabled}
                          id={subCheckboxId}
                          onChange={(event) =>
                            handleToggleGrantSub(
                              grant.target_id,
                              event.target.checked,
                            )
                          }
                          type="checkbox"
                        />
                        <span>含子部门</span>
                      </label>
                    )}

                    <button
                      aria-label={`移除附加授权：${targetDisplay}`}
                      className={cn(
                        "inline-flex h-7 w-7 items-center justify-center rounded-md text-text-tertiary transition-colors",
                        "hover:bg-danger-bg hover:text-danger",
                        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus",
                        "disabled:pointer-events-none disabled:opacity-45",
                      )}
                      data-compact-control="true"
                      disabled={disabled}
                      onClick={() => handleRemoveGrant(grant)}
                      type="button"
                    >
                      <Trash2 aria-hidden="true" className="h-3.5 w-3.5" />
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        ) : null}
      </div>

      {/* 新增附加授权交互区：按角色动态裁剪权限边界 */}
      <div className="mt-3 border-t border-border-subtle pt-3">
        {noAvailableDeptAdminTargets ? (
          <p className="text-xs text-text-tertiary">
            当前账号仅管辖该归属部门（或管辖内其他部门均已添加），部门管理员不支持跨管辖范围或按角色共享。
          </p>
        ) : (
          <div className="flex flex-wrap items-end gap-2">
            {superAdmin ? (
              <div className="flex min-w-[120px] flex-col gap-1">
                <label
                  className="text-[11px] font-medium text-text-secondary"
                  htmlFor={targetTypeId}
                >
                  授权类型
                </label>
                <Select
                  disabled={disabled}
                  onValueChange={(value) => {
                    const nextType = value as VisibilityGrantTargetType;
                    setNewTargetType(nextType);
                    setNewTargetId("");
                    setDuplicateNotice(null);
                    setInputError(null);
                  }}
                  value={newTargetType}
                >
                  <SelectTrigger
                    aria-label="附加授权类型"
                    className="h-8 px-2.5 text-xs"
                    id={targetTypeId}
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="department">指定部门</SelectItem>
                    <SelectItem value="role">指定角色</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            ) : null}

            <div className="flex min-w-[180px] flex-1 flex-col gap-1">
              {superAdmin &&
              newTargetType === "department" &&
              !useManualDepartmentInput &&
              isLoadingDepartments ? (
                <span className="text-[11px] font-medium text-text-secondary">
                  附加共享部门
                </span>
              ) : (
                <label
                  className="text-[11px] font-medium text-text-secondary"
                  htmlFor={targetValueId}
                >
                  {superAdmin && newTargetType === "role"
                    ? "附加共享角色"
                    : "附加共享部门"}
                </label>
              )}

              {superAdmin && newTargetType === "role" ? (
                <Select
                  disabled={disabled || candidateRoleOptions.length === 0}
                  onValueChange={(value) => {
                    setNewTargetId(value);
                    setDuplicateNotice(null);
                    setInputError(null);
                  }}
                  value={newTargetId}
                >
                  <SelectTrigger
                    aria-describedby={targetDescribedBy || undefined}
                    aria-label="选择附加共享角色"
                    className="h-8 px-2.5 text-xs"
                    id={targetValueId}
                  >
                    <SelectValue
                      placeholder={
                        candidateRoleOptions.length === 0
                          ? "全部角色均已添加"
                          : "请选择角色"
                      }
                    />
                  </SelectTrigger>
                  <SelectContent>
                    {candidateRoleOptions.map((role) => (
                      <SelectItem key={role.value} value={role.value}>
                        {role.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              ) : useManualDepartmentInput ? (
                <input
                  aria-describedby={targetDescribedBy || undefined}
                  aria-invalid={inputError ? true : undefined}
                  aria-label="输入附加共享部门 ID"
                  autoComplete="off"
                  className={cn(
                    "h-8 rounded-lg border bg-canvas px-2.5 text-xs text-text-primary placeholder:text-text-placeholder focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus disabled:cursor-not-allowed disabled:opacity-60",
                    inputError ? "border-danger" : "border-border-default",
                  )}
                  disabled={disabled}
                  id={targetValueId}
                  maxLength={VISIBILITY_GRANT_TARGET_MAX_LENGTH}
                  onChange={(event) => {
                    setNewTargetId(event.target.value);
                    setDuplicateNotice(null);
                    setInputError(null);
                  }}
                  onKeyDown={handleManualInputKeyDown}
                  placeholder="部门列表不可用，请手动输入部门 ID"
                  value={newTargetId}
                />
              ) : superAdmin && isLoadingDepartments ? (
                <div
                  className="flex h-8 items-center rounded-lg border border-border-subtle bg-canvas px-2.5 text-xs text-text-tertiary"
                  role="status"
                >
                  正在加载企业活跃部门…
                </div>
              ) : (
                <Select
                  disabled={disabled || candidateDepartmentOptions.length === 0}
                  onValueChange={(value) => {
                    setNewTargetId(value);
                    setDuplicateNotice(null);
                    setInputError(null);
                  }}
                  value={newTargetId}
                >
                  <SelectTrigger
                    aria-describedby={targetDescribedBy || undefined}
                    aria-label="选择附加共享部门"
                    className="h-8 px-2.5 text-xs"
                    id={targetValueId}
                  >
                    <SelectValue
                      placeholder={
                        candidateDepartmentOptions.length === 0
                          ? "暂无可追加部门"
                          : "请选择附加部门"
                      }
                    />
                  </SelectTrigger>
                  <SelectContent>
                    {candidateDepartmentOptions.map((dept) => (
                      <SelectItem key={dept.id} value={dept.id}>
                        {dept.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            </div>

            {(!superAdmin || newTargetType === "department") &&
            !noAvailableSuperAdminTargets ? (
              <label
                className="inline-flex h-8 cursor-pointer items-center gap-1.5 px-1 text-xs text-text-secondary select-none"
                htmlFor={newSubCheckboxId}
              >
                <input
                  checked={newIncludeSub}
                  className="h-3.5 w-3.5 rounded border-border-strong accent-brand"
                  disabled={disabled}
                  id={newSubCheckboxId}
                  onChange={(event) => setNewIncludeSub(event.target.checked)}
                  type="checkbox"
                />
                <span>含子部门</span>
              </label>
            ) : null}

            <Button
              disabled={
                disabled ||
                !newTargetId.trim() ||
                (superAdmin &&
                  newTargetType === "department" &&
                  isLoadingDepartments)
              }
              onClick={handleAddGrant}
              size="sm"
              type="button"
              variant="outline"
            >
              <Plus aria-hidden="true" className="h-3.5 w-3.5" />
              添加共享
            </Button>
          </div>
        )}

        {inputError ? (
          <p
            className="mt-1.5 text-xs font-medium text-danger"
            id={inputErrorId}
            role="alert"
          >
            {inputError}
          </p>
        ) : null}

        {duplicateNotice ? (
          <p
            className="mt-1.5 text-xs font-medium text-warning"
            id={duplicateNoticeId}
            role="status"
          >
            {duplicateNotice}
          </p>
        ) : null}
      </div>
    </section>
  );
}
