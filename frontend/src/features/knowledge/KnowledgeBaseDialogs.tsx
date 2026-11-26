import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { VisibilityBadge, type VisibilityScope } from "../../shared/ui";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Modal } from "../../shared/ui/Modal";
import {
  VisibilityGrantsEditor,
  defaultOwnerGrant,
  normalizeVisibilityGrants,
  visibilityGrantsEqual,
} from "../access/VisibilityGrantsEditor";
import { useAdminOrgDepartments } from "../admin/hooks";
import type { OrgDepartment } from "../admin/model";
import { useAuth } from "../auth/AuthProvider";
import {
  isDeptAdmin,
  isSuperAdmin,
  resolveUserDepartmentName,
  type User,
} from "../auth/model";
import { knowledgeErrorMessage } from "./errorMessages";
import {
  useCreateKnowledgeBaseMutation,
  useDeleteKnowledgeBaseMutation,
  useUpdateKnowledgeBaseMutation,
} from "./hooks";
import {
  normalizeKnowledgeDescription,
  normalizeKnowledgeName,
  validateKnowledgeDescription,
  validateKnowledgeName,
  type KnowledgeBase,
  type KnowledgeBaseInput,
  type KnowledgeBasePatch,
  type VisibilityGrantInput,
} from "./model";

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

function resolveOwnerDepartmentName(
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
function KnowledgeVisibilityFields({
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
      <div className="workspace-field">
        <span>可见范围</span>
        <div className="flex items-center gap-2 pt-0.5">
          <VisibilityBadge scope="personal" />
          <small>当前角色仅支持创建与维护个人知识库。</small>
        </div>
      </div>
    );
  }

  const noManagedDepartments = deptAdmin && managedDeptOptions.length === 0;
  const hasDepartmentControl =
    !noManagedDepartments && !(superAdmin && orgDepartmentsLoading);

  return (
    <>
      <label className="workspace-field" htmlFor={scopeSelectId}>
        <span>可见范围</span>
        <select
          aria-label="可见范围"
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
        <div className="workspace-field">
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

interface NewKnowledgeBaseDialogProps {
  onClose: () => void;
  onCreated?: (base: KnowledgeBase) => void;
  open: boolean;
}

export function NewKnowledgeBaseDialog({
  onClose,
  onCreated,
  open,
}: NewKnowledgeBaseDialogProps) {
  const { user } = useAuth();
  const superAdmin = isSuperAdmin(user);
  const navigate = useNavigate();
  const formId = useId();
  const nameErrorId = useId();
  const descriptionErrorId = useId();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [visibilityScope, setVisibilityScope] =
    useState<VisibilityScope>("personal");
  const [ownerDepartmentId, setOwnerDepartmentId] = useState("");
  const [visibilityGrants, setVisibilityGrants] = useState<
    VisibilityGrantInput[]
  >([]);
  // 追踪用户是否主动改动过附加授权区：未改动时必须省略 visibility_grants，交由后端按实时管辖子树派生默认授权，防止直属 dept_admin 触发 403。
  const [visibilityGrantsTouched, setVisibilityGrantsTouched] = useState(false);
  const [nameError, setNameError] = useState<string | null>(null);
  const [descriptionError, setDescriptionError] = useState<string | null>(
    null,
  );
  const [departmentError, setDepartmentError] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const mutation = useCreateKnowledgeBaseMutation();
  const { reset: resetMutation } = mutation;

  // 在弹窗顶层统一拉取一次企业活跃部门目录，透传给归属部门选择器与 VisibilityGrantsEditor，避免重复请求与状态失步。
  const orgDepartmentsQuery = useAdminOrgDepartments(
    open && superAdmin && visibilityScope === "department",
    false,
  );
  const activeOrgDepartments = useMemo(
    () =>
      (orgDepartmentsQuery.data?.items ?? []).filter(
        (dept) => dept.status === "active",
      ),
    [orgDepartmentsQuery.data?.items],
  );
  const ownerDepartmentName = useMemo(
    () =>
      resolveOwnerDepartmentName(user, activeOrgDepartments, ownerDepartmentId),
    [activeOrgDepartments, ownerDepartmentId, user],
  );

  const deptAdminMissingManagedDepts =
    isDeptAdmin(user) &&
    visibilityScope === "department" &&
    (user?.managed_department_ids.length ?? 0) === 0;

  useEffect(() => {
    if (open) {
      setName("");
      setDescription("");
      setVisibilityScope("personal");
      setOwnerDepartmentId("");
      setVisibilityGrants([]);
      setVisibilityGrantsTouched(false);
      setNameError(null);
      setDescriptionError(null);
      setDepartmentError(null);
      setRequestError(null);
      resetMutation();
    }
    // 每次打开都从干净表单开始，避免把上一次失败内容误带入新知识库。
  }, [open, resetMutation]);

  const handleScopeChange = useCallback(
    (nextScope: VisibilityScope) => {
      setVisibilityScope(nextScope);
      const trimmed = ownerDepartmentId.trim();
      setVisibilityGrants(
        nextScope === "department" && trimmed
          ? [defaultOwnerGrant(trimmed, superAdmin)]
          : [],
      );
      setVisibilityGrantsTouched(false);
      setDepartmentError(null);
      setRequestError(null);
    },
    [ownerDepartmentId, superAdmin],
  );

  const handleDepartmentChange = useCallback(
    (nextDeptId: string) => {
      setOwnerDepartmentId(nextDeptId);
      const trimmed = nextDeptId.trim();
      // 同步按新归属部门重置默认授权并清除 touched 标记，避免子编辑器触发冗余同步将未改动状态误判为已改动。
      setVisibilityGrants(
        trimmed ? [defaultOwnerGrant(trimmed, superAdmin)] : [],
      );
      setVisibilityGrantsTouched(false);
      setDepartmentError(null);
      setRequestError(null);
    },
    [superAdmin],
  );

  const close = () => {
    if (!mutation.isPending) {
      onClose();
    }
  };

  const handleSubmit = async () => {
    if (deptAdminMissingManagedDepts) {
      return;
    }
    const nextNameError = validateKnowledgeName(name);
    const nextDescriptionError = validateKnowledgeDescription(description);
    const trimmedDeptId = ownerDepartmentId.trim();
    const nextDepartmentError =
      visibilityScope === "department" && !trimmedDeptId
        ? "请选择或填写部门共享知识库的归属部门。"
        : null;
    setNameError(nextNameError);
    setDescriptionError(nextDescriptionError);
    setDepartmentError(nextDepartmentError);
    if (nextNameError || nextDescriptionError || nextDepartmentError) {
      return;
    }

    setRequestError(null);
    const normalizedDescription =
      normalizeKnowledgeDescription(description);
    const payload: KnowledgeBaseInput = {
      name: normalizeKnowledgeName(name),
      ...(normalizedDescription ? { description: normalizedDescription } : {}),
    };
    if (visibilityScope === "company") {
      payload.visibility_scope = "company";
    } else if (visibilityScope === "department") {
      payload.visibility_scope = "department";
      payload.owner_department_id = trimmedDeptId;
      // 仅在用户主动改动过附加授权区时携带规范化后的 visibility_grants；未改动时省略，交由后端派生默认归属授权。
      if (visibilityGrantsTouched) {
        payload.visibility_grants = normalizeVisibilityGrants(
          trimmedDeptId,
          visibilityGrants,
          superAdmin,
        );
      }
    }
    try {
      const created = await mutation.mutateAsync(payload);
      onClose();
      if (onCreated) {
        onCreated(created);
      } else {
        navigate(`/knowledge/${encodeURIComponent(created.id)}`);
      }
    } catch (failure) {
      setRequestError(knowledgeErrorMessage(failure));
    }
  };

  return (
    <Modal
      closeDisabled={mutation.isPending}
      description="知识库用于保存可检索的文档与文本资料。"
      footer={
        <>
          <button
            className="button-quiet"
            disabled={mutation.isPending}
            onClick={close}
            type="button"
          >
            取消
          </button>
          <button
            className="button-primary"
            disabled={mutation.isPending || deptAdminMissingManagedDepts}
            form={formId}
            type="submit"
          >
            {mutation.isPending ? "正在创建…" : "创建知识库"}
          </button>
        </>
      }
      onClose={close}
      open={open}
      panelClassName={
        visibilityScope === "department" ? "w-full max-w-2xl" : undefined
      }
      title="新建知识库"
    >
      <form
        className="knowledge-form"
        id={formId}
        onSubmit={(event) => {
          event.preventDefault();
          void handleSubmit();
        }}
      >
        <label className="workspace-field" htmlFor={`${formId}-name`}>
          <span>知识库名称</span>
          <input
            aria-label="知识库名称"
            aria-describedby={nameError ? nameErrorId : undefined}
            aria-invalid={nameError ? true : undefined}
            autoComplete="off"
            data-autofocus
            id={`${formId}-name`}
            onChange={(event) => {
              setName(event.target.value);
              setNameError(null);
              setRequestError(null);
            }}
            placeholder="例如：产品研究资料"
            value={name}
          />
          <small>最多 120 个 Unicode 码点，不能包含控制字符。</small>
        </label>
        {nameError ? (
          <p className="field-error" id={nameErrorId} role="alert">
            {nameError}
          </p>
        ) : null}

        <label
          className="workspace-field"
          htmlFor={`${formId}-description`}
        >
          <span>说明（可选）</span>
          <input
            aria-label="说明"
            aria-describedby={
              descriptionError ? descriptionErrorId : undefined
            }
            aria-invalid={descriptionError ? true : undefined}
            autoComplete="off"
            id={`${formId}-description`}
            onChange={(event) => {
              setDescription(event.target.value);
              setDescriptionError(null);
              setRequestError(null);
            }}
            placeholder="简要说明资料用途"
            value={description}
          />
          <small>最多 500 个字符；当前后端不支持清空已保存说明。</small>
        </label>
        {descriptionError ? (
          <p className="field-error" id={descriptionErrorId} role="alert">
            {descriptionError}
          </p>
        ) : null}

        <KnowledgeVisibilityFields
          departmentError={departmentError}
          departmentId={ownerDepartmentId}
          disabled={mutation.isPending}
          formId={formId}
          onDepartmentChange={handleDepartmentChange}
          onScopeChange={handleScopeChange}
          open={open}
          orgDepartments={activeOrgDepartments}
          orgDepartmentsError={orgDepartmentsQuery.isError}
          orgDepartmentsLoading={orgDepartmentsQuery.isPending}
          scope={visibilityScope}
        />

        {visibilityScope === "department" && ownerDepartmentId.trim() ? (
          <VisibilityGrantsEditor
            defaultIncludeSubdepartments={superAdmin}
            departments={superAdmin ? activeOrgDepartments : undefined}
            departmentsError={superAdmin ? orgDepartmentsQuery.isError : false}
            departmentsLoading={
              superAdmin ? orgDepartmentsQuery.isPending : false
            }
            disabled={mutation.isPending}
            enabled={open}
            onChange={(nextGrants) => {
              setVisibilityGrants(nextGrants);
              setVisibilityGrantsTouched(true);
              setRequestError(null);
            }}
            ownerDepartmentId={ownerDepartmentId}
            ownerDepartmentName={ownerDepartmentName}
            resourceLabel="知识库"
            value={visibilityGrants}
          />
        ) : null}

        {requestError ? <ErrorBanner message={requestError} /> : null}
      </form>
    </Modal>
  );
}

interface EditKnowledgeBaseDialogProps {
  base: KnowledgeBase;
  onClose: () => void;
  open: boolean;
}

export function EditKnowledgeBaseDialog({
  base,
  onClose,
  open,
}: EditKnowledgeBaseDialogProps) {
  const { user } = useAuth();
  const superAdmin = isSuperAdmin(user);
  const formId = useId();
  const nameErrorId = useId();
  const descriptionErrorId = useId();
  const initialScope = base.visibility_scope;
  const initialOwnerDeptId = base.owner_department_id ?? "";
  // 基线授权与后端当前持久化授权对齐，用于精准比较「同部门仅改动附加授权」是否产生真实差异。
  const initialGrants = useMemo(
    () =>
      initialScope === "department" && initialOwnerDeptId
        ? normalizeVisibilityGrants(
            initialOwnerDeptId,
            base.visibility_grants,
            superAdmin,
          )
        : [],
    [base.visibility_grants, initialOwnerDeptId, initialScope, superAdmin],
  );
  const [name, setName] = useState(base.name);
  const [description, setDescription] = useState(base.description ?? "");
  const [visibilityScope, setVisibilityScope] =
    useState<VisibilityScope>(initialScope);
  const [ownerDepartmentId, setOwnerDepartmentId] =
    useState(initialOwnerDeptId);
  const [visibilityGrants, setVisibilityGrants] =
    useState<VisibilityGrantInput[]>(initialGrants);
  // 切入部门范围或更换归属部门后，仅当用户显式改动附加授权时才携带 visibility_grants，防止直属 dept_admin 触发 403。
  const [visibilityGrantsTouched, setVisibilityGrantsTouched] = useState(false);
  // 归属部门临时切到既有附加部门再切回原归属部门时，通过递增 key 重建 VisibilityGrantsEditor 实例与 prevOwnerIdRef，
  // 避免其内部归属切换 effect 按旧 prevOwnerId 误滤除原有合法附加授权并反向触发 onChange 标脏表单。
  const [grantsEditorResetKey, setGrantsEditorResetKey] = useState(0);
  const [nameError, setNameError] = useState<string | null>(null);
  const [descriptionError, setDescriptionError] = useState<string | null>(
    null,
  );
  const [departmentError, setDepartmentError] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const lastInitKeyRef = useRef<string | null>(null);
  const mutation = useUpdateKnowledgeBaseMutation(base.id);
  const { reset: resetMutation } = mutation;

  const orgDepartmentsQuery = useAdminOrgDepartments(
    open && superAdmin && visibilityScope === "department",
    false,
  );
  const activeOrgDepartments = useMemo(
    () =>
      (orgDepartmentsQuery.data?.items ?? []).filter(
        (dept) => dept.status === "active",
      ),
    [orgDepartmentsQuery.data?.items],
  );
  const ownerDepartmentName = useMemo(
    () =>
      resolveOwnerDepartmentName(user, activeOrgDepartments, ownerDepartmentId),
    [activeOrgDepartments, ownerDepartmentId, user],
  );

  const deptAdminMissingManagedDepts =
    isDeptAdmin(user) &&
    visibilityScope === "department" &&
    (user?.managed_department_ids.length ?? 0) === 0;

  // 仅在弹窗从关闭切为打开或切换编辑目标知识库时初始化草稿，避免编辑期间后台列表刷新冲掉未保存授权修改。
  useEffect(() => {
    if (!open) {
      lastInitKeyRef.current = null;
      return;
    }
    if (lastInitKeyRef.current === base.id) {
      return;
    }
    lastInitKeyRef.current = base.id;
    setName(base.name);
    setDescription(base.description ?? "");
    setVisibilityScope(initialScope);
    setOwnerDepartmentId(initialOwnerDeptId);
    setVisibilityGrants(initialGrants);
    setVisibilityGrantsTouched(false);
    setGrantsEditorResetKey((prev) => prev + 1);
    setNameError(null);
    setDescriptionError(null);
    setDepartmentError(null);
    setRequestError(null);
    resetMutation();
  }, [
    base.description,
    base.id,
    base.name,
    initialGrants,
    initialOwnerDeptId,
    initialScope,
    open,
    resetMutation,
  ]);

  const handleScopeChange = useCallback(
    (nextScope: VisibilityScope) => {
      setVisibilityScope(nextScope);
      if (nextScope === "department") {
        const trimmed = ownerDepartmentId.trim();
        // 切回原归属部门时恢复原持久化基线授权；否则按当前归属部门初始化默认授权。
        if (initialScope === "department" && trimmed === initialOwnerDeptId) {
          setVisibilityGrants(initialGrants);
        } else {
          setVisibilityGrants(
            trimmed ? [defaultOwnerGrant(trimmed, superAdmin)] : [],
          );
        }
      } else {
        setVisibilityGrants([]);
      }
      setVisibilityGrantsTouched(false);
      setDepartmentError(null);
      setRequestError(null);
    },
    [
      initialGrants,
      initialOwnerDeptId,
      initialScope,
      ownerDepartmentId,
      superAdmin,
    ],
  );

  const handleDepartmentChange = useCallback(
    (nextDeptId: string) => {
      setOwnerDepartmentId(nextDeptId);
      const trimmed = nextDeptId.trim();
      if (initialScope === "department" && trimmed === initialOwnerDeptId) {
        // 切回原归属部门恢复基线授权时递增 reset key，确保子编辑器以最新 owner 重建 prevOwnerIdRef，防回写丢授权。
        setVisibilityGrants(initialGrants);
        setGrantsEditorResetKey((prev) => prev + 1);
      } else {
        setVisibilityGrants(
          trimmed ? [defaultOwnerGrant(trimmed, superAdmin)] : [],
        );
      }
      setVisibilityGrantsTouched(false);
      setDepartmentError(null);
      setRequestError(null);
    },
    [initialGrants, initialOwnerDeptId, initialScope, superAdmin],
  );

  const close = () => {
    if (!mutation.isPending) {
      onClose();
    }
  };

  const handleSubmit = async () => {
    if (deptAdminMissingManagedDepts) {
      return;
    }
    const nextNameError = validateKnowledgeName(name);
    const nextDescriptionError = validateKnowledgeDescription(
      description,
      base.description,
    );
    const trimmedDeptId = ownerDepartmentId.trim();
    const visibilityChanged =
      visibilityScope !== initialScope ||
      (visibilityScope === "department" &&
        trimmedDeptId !== initialOwnerDeptId);
    const nextDepartmentError =
      visibilityScope === "department" && !trimmedDeptId
        ? "请选择或填写部门共享知识库的归属部门。"
        : null;
    setNameError(nextNameError);
    setDescriptionError(nextDescriptionError);
    setDepartmentError(nextDepartmentError);
    if (nextNameError || nextDescriptionError || nextDepartmentError) {
      return;
    }

    const normalizedName = normalizeKnowledgeName(name);
    const normalizedDescription =
      normalizeKnowledgeDescription(description);
    const stayingSameDepartment =
      initialScope === "department" &&
      visibilityScope === "department" &&
      trimmedDeptId === initialOwnerDeptId;
    const normalizedGrants =
      visibilityScope === "department" && trimmedDeptId
        ? normalizeVisibilityGrants(
            trimmedDeptId,
            visibilityGrants,
            superAdmin,
          )
        : [];
    const grantsChangedInSameDepartment =
      stayingSameDepartment &&
      !visibilityGrantsEqual(normalizedGrants, initialGrants);

    const patch: KnowledgeBasePatch = {};
    if (normalizedName !== base.name) {
      patch.name = normalizedName;
    }
    if (
      normalizedDescription &&
      normalizedDescription !== base.description
    ) {
      patch.description = normalizedDescription;
    }
    if (visibilityChanged) {
      patch.visibility_scope = visibilityScope;
      // 仅切换/修改为 department 时透传归属部门；切换至 company/personal 时严禁携带 owner_department_id 或 visibility_grants。
      if (visibilityScope === "department") {
        patch.owner_department_id = trimmedDeptId;
        // 切入部门范围或更换归属部门后，仅用户显式调整授权区时才携带 visibility_grants；未改动时省略交由后端重建归属默认授权。
        if (visibilityGrantsTouched) {
          patch.visibility_grants = normalizedGrants;
        }
      }
    } else if (grantsChangedInSameDepartment) {
      // 名称、说明、范围及归属部门均未变化但附加授权实际变化时，单独携带 visibility_grants 发起 PATCH。
      patch.visibility_grants = normalizedGrants;
    }
    if (Object.keys(patch).length === 0) {
      onClose();
      return;
    }

    setRequestError(null);
    try {
      await mutation.mutateAsync(patch);
      onClose();
    } catch (failure) {
      setRequestError(knowledgeErrorMessage(failure));
    }
  };

  return (
    <Modal
      closeDisabled={mutation.isPending}
      description="名称、说明与可见范围会同步更新到知识库目录中。"
      footer={
        <>
          <button
            className="button-quiet"
            disabled={mutation.isPending}
            onClick={close}
            type="button"
          >
            取消
          </button>
          <button
            className="button-primary"
            disabled={mutation.isPending || deptAdminMissingManagedDepts}
            form={formId}
            type="submit"
          >
            {mutation.isPending ? "正在保存…" : "保存知识库"}
          </button>
        </>
      }
      onClose={close}
      open={open}
      panelClassName={
        visibilityScope === "department" ? "w-full max-w-2xl" : undefined
      }
      title="编辑知识库"
    >
      <form
        className="knowledge-form"
        id={formId}
        onSubmit={(event) => {
          event.preventDefault();
          void handleSubmit();
        }}
      >
        <label className="workspace-field" htmlFor={`${formId}-edit-name`}>
          <span>知识库名称</span>
          <input
            aria-label="知识库名称"
            aria-describedby={nameError ? nameErrorId : undefined}
            aria-invalid={nameError ? true : undefined}
            autoComplete="off"
            data-autofocus
            id={`${formId}-edit-name`}
            onChange={(event) => {
              setName(event.target.value);
              setNameError(null);
              setRequestError(null);
            }}
            value={name}
          />
        </label>
        {nameError ? (
          <p className="field-error" id={nameErrorId} role="alert">
            {nameError}
          </p>
        ) : null}

        <label
          className="workspace-field"
          htmlFor={`${formId}-edit-description`}
        >
          <span>说明</span>
          <input
            aria-label="说明"
            aria-describedby={
              descriptionError ? descriptionErrorId : "description-backend-note"
            }
            aria-invalid={descriptionError ? true : undefined}
            autoComplete="off"
            id={`${formId}-edit-description`}
            onChange={(event) => {
              setDescription(event.target.value);
              setDescriptionError(null);
              setRequestError(null);
            }}
            value={description}
          />
          <small id="description-backend-note">
            可设置或替换说明；当前后端接口不支持清空已有说明。
          </small>
        </label>
        {descriptionError ? (
          <p className="field-error" id={descriptionErrorId} role="alert">
            {descriptionError}
          </p>
        ) : null}

        <KnowledgeVisibilityFields
          departmentError={departmentError}
          departmentId={ownerDepartmentId}
          disabled={mutation.isPending}
          formId={formId}
          onDepartmentChange={handleDepartmentChange}
          onScopeChange={handleScopeChange}
          open={open}
          orgDepartments={activeOrgDepartments}
          orgDepartmentsError={orgDepartmentsQuery.isError}
          orgDepartmentsLoading={orgDepartmentsQuery.isPending}
          scope={visibilityScope}
        />

        {visibilityScope === "department" && ownerDepartmentId.trim() ? (
          <VisibilityGrantsEditor
            defaultIncludeSubdepartments={superAdmin}
            departments={superAdmin ? activeOrgDepartments : undefined}
            departmentsError={superAdmin ? orgDepartmentsQuery.isError : false}
            departmentsLoading={
              superAdmin ? orgDepartmentsQuery.isPending : false
            }
            disabled={mutation.isPending}
            enabled={open}
            key={`${base.id}-${grantsEditorResetKey}`}
            onChange={(nextGrants) => {
              setVisibilityGrants(nextGrants);
              setVisibilityGrantsTouched(true);
              setRequestError(null);
            }}
            ownerDepartmentId={ownerDepartmentId}
            ownerDepartmentName={ownerDepartmentName}
            resourceLabel="知识库"
            value={visibilityGrants}
          />
        ) : null}

        {requestError ? <ErrorBanner message={requestError} /> : null}
      </form>
    </Modal>
  );
}

interface DeleteKnowledgeBaseDialogProps {
  base: KnowledgeBase;
  onClose: () => void;
  onDeleted?: () => void;
  open: boolean;
}

export function DeleteKnowledgeBaseDialog({
  base,
  onClose,
  onDeleted,
  open,
}: DeleteKnowledgeBaseDialogProps) {
  const navigate = useNavigate();
  const checkboxId = useId();
  const [confirmed, setConfirmed] = useState(false);
  const [requestError, setRequestError] = useState<string | null>(null);
  const mutation = useDeleteKnowledgeBaseMutation(base.id);
  const { reset: resetMutation } = mutation;

  useEffect(() => {
    if (open) {
      setConfirmed(false);
      setRequestError(null);
      resetMutation();
    }
  }, [open, resetMutation]);

  const close = () => {
    if (!mutation.isPending) {
      onClose();
    }
  };

  const handleDelete = async () => {
    if (!confirmed) {
      return;
    }
    setRequestError(null);
    try {
      await mutation.mutateAsync();
      onClose();
      if (onDeleted) {
        onDeleted();
      } else {
        navigate("/knowledge", { replace: true });
      }
    } catch (failure) {
      setRequestError(knowledgeErrorMessage(failure));
    }
  };

  return (
    <Modal
      closeDisabled={mutation.isPending}
      description="知识库删除后，其项目关联也会移除。"
      footer={
        <>
          <button
            className="button-quiet"
            data-autofocus
            disabled={mutation.isPending}
            onClick={close}
            type="button"
          >
            取消
          </button>
          <button
            className="button-danger"
            disabled={!confirmed || mutation.isPending}
            onClick={() => void handleDelete()}
            type="button"
          >
            {mutation.isPending ? "正在删除…" : "删除知识库"}
          </button>
        </>
      }
      onClose={close}
      open={open}
      title="删除知识库"
    >
      <p className="confirm-copy">
        确定删除“{base.name}”吗？知识库中的文档、版本和项目关联将无法继续访问。
      </p>
      <label
        className="workspace-checkbox workspace-checkbox--danger"
        htmlFor={checkboxId}
      >
        <input
          checked={confirmed}
          id={checkboxId}
          onChange={(event) => setConfirmed(event.target.checked)}
          type="checkbox"
        />
        <span>我已确认要删除这个知识库</span>
      </label>
      {requestError ? <ErrorBanner message={requestError} /> : null}
    </Modal>
  );
}
