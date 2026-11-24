import { useEffect, useId, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { VisibilityBadge, type VisibilityScope } from "../../shared/ui";
import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Modal } from "../../shared/ui/Modal";
import { useAdminOrgDepartments } from "../admin/hooks";
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
  knowledgeBaseScope,
  normalizeKnowledgeDescription,
  normalizeKnowledgeName,
  validateKnowledgeDescription,
  validateKnowledgeName,
  type KnowledgeBase,
  type KnowledgeBaseInput,
  type KnowledgeBasePatch,
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

interface VisibilityFieldsProps {
  departmentError: string | null;
  departmentId: string;
  disabled: boolean;
  formId: string;
  onDepartmentChange: (value: string) => void;
  onScopeChange: (value: VisibilityScope) => void;
  open: boolean;
  scope: VisibilityScope;
}

/**
 * 知识库可见范围与归属部门字段组：
 * - member：固定为个人知识库（只读展示）；
 * - dept_admin：可选个人 / 部门共享，部门列表仅限本人 managed_department_ids；
 * - super_admin：可选个人 / 部门共享 / 全公司，选部门时按需请求组织部门树，失败安全降级为手工输入部门 ID。
 */
function KnowledgeVisibilityFields({
  departmentError,
  departmentId,
  disabled,
  formId,
  onDepartmentChange,
  onScopeChange,
  open,
  scope,
}: VisibilityFieldsProps) {
  const { user } = useAuth();
  const scopeSelectId = `${formId}-visibility-scope`;
  const deptSelectId = `${formId}-owner-department`;
  const deptErrorId = `${formId}-owner-department-error`;
  const superAdmin = isSuperAdmin(user);
  const deptAdmin = isDeptAdmin(user);
  const allowedScopes = useMemo(() => allowedScopesForUser(user), [user]);

  const orgDepartments = useAdminOrgDepartments(
    open && superAdmin && scope === "department",
    false,
  );

  const managedDeptOptions = useMemo(() => {
    if (!user) {
      return [];
    }
    return user.managed_department_ids.map((id) => ({
      id,
      name: resolveUserDepartmentName(user, id) ?? id,
    }));
  }, [user]);

  const activeOrgDeptOptions = useMemo(
    () =>
      (orgDepartments.data?.items ?? []).filter(
        (dept) => dept.status === "active",
      ),
    [orgDepartments.data?.items],
  );

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
    !noManagedDepartments && !(superAdmin && orgDepartments.isPending);

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
              ? "归属部门及子部门成员均可检索，对应部门管理员与超管可维护。"
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
          ) : orgDepartments.isPending ? (
            <small role="status">正在加载企业活跃部门列表…</small>
          ) : orgDepartments.isError ? (
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
              {activeOrgDeptOptions.map((dept) => (
                <option key={dept.id} value={dept.id}>
                  {dept.name}
                </option>
              ))}
              {departmentId &&
              !activeOrgDeptOptions.some((dept) => dept.id === departmentId) ? (
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
  const navigate = useNavigate();
  const formId = useId();
  const nameErrorId = useId();
  const descriptionErrorId = useId();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [visibilityScope, setVisibilityScope] =
    useState<VisibilityScope>("personal");
  const [ownerDepartmentId, setOwnerDepartmentId] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);
  const [descriptionError, setDescriptionError] = useState<string | null>(
    null,
  );
  const [departmentError, setDepartmentError] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const mutation = useCreateKnowledgeBaseMutation();
  const { reset: resetMutation } = mutation;
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
      setNameError(null);
      setDescriptionError(null);
      setDepartmentError(null);
      setRequestError(null);
      resetMutation();
    }
    // 每次打开都从干净表单开始，避免把上一次失败内容误带入新知识库。
  }, [open, resetMutation]);

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
          onDepartmentChange={(value) => {
            setOwnerDepartmentId(value);
            setDepartmentError(null);
            setRequestError(null);
          }}
          onScopeChange={(value) => {
            setVisibilityScope(value);
            setDepartmentError(null);
            setRequestError(null);
          }}
          open={open}
          scope={visibilityScope}
        />

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
  const formId = useId();
  const nameErrorId = useId();
  const descriptionErrorId = useId();
  const initialScope = knowledgeBaseScope(base);
  const initialOwnerDeptId = base.owner_department_id ?? "";
  const [name, setName] = useState(base.name);
  const [description, setDescription] = useState(base.description ?? "");
  const [visibilityScope, setVisibilityScope] =
    useState<VisibilityScope>(initialScope);
  const [ownerDepartmentId, setOwnerDepartmentId] =
    useState(initialOwnerDeptId);
  const [nameError, setNameError] = useState<string | null>(null);
  const [descriptionError, setDescriptionError] = useState<string | null>(
    null,
  );
  const [departmentError, setDepartmentError] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const mutation = useUpdateKnowledgeBaseMutation(base.id);
  const { reset: resetMutation } = mutation;
  const deptAdminMissingManagedDepts =
    isDeptAdmin(user) &&
    visibilityScope === "department" &&
    (user?.managed_department_ids.length ?? 0) === 0;

  useEffect(() => {
    if (open) {
      setName(base.name);
      setDescription(base.description ?? "");
      setVisibilityScope(initialScope);
      setOwnerDepartmentId(initialOwnerDeptId);
      setNameError(null);
      setDescriptionError(null);
      setDepartmentError(null);
      setRequestError(null);
      resetMutation();
    }
  }, [
    base.description,
    base.name,
    initialOwnerDeptId,
    initialScope,
    open,
    resetMutation,
  ]);

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
      visibilityChanged && visibilityScope === "department" && !trimmedDeptId
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
      // 仅切换/修改为 department 时透传归属部门；切换至 company/personal 时不传非法 owner_department_id。
      if (visibilityScope === "department") {
        patch.owner_department_id = trimmedDeptId;
      }
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
          onDepartmentChange={(value) => {
            setOwnerDepartmentId(value);
            setDepartmentError(null);
            setRequestError(null);
          }}
          onScopeChange={(value) => {
            setVisibilityScope(value);
            setDepartmentError(null);
            setRequestError(null);
          }}
          open={open}
          scope={visibilityScope}
        />

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
