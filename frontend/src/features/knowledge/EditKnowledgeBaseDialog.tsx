import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";

import { useSubmitLock } from "../../shared/lib/useSubmitLock";
import {
  Button,
  FIELD_CLASS,
  FIELD_INPUT_CLASS,
  FORM_STACK_CLASS,
  InlineAlert,
  type VisibilityScope,
} from "../../shared/ui";
import { Modal } from "../../shared/ui/Modal";
import {
  VisibilityGrantsEditor,
  defaultOwnerGrant,
  normalizeVisibilityGrants,
  visibilityGrantsEqual,
} from "../access/VisibilityGrantsEditor";
import { useAdminOrgDepartments } from "../admin/hooks";
import { useAuth } from "../auth/AuthProvider";
import { isDeptAdmin, isSuperAdmin } from "../auth/model";
import {
  KnowledgeVisibilityFields,
  resolveOwnerDepartmentName,
} from "./KnowledgeVisibilityFields";
import { knowledgeErrorMessage } from "./errorMessages";
import { useUpdateKnowledgeBaseMutation } from "./hooks";
import {
  normalizeKnowledgeDescription,
  normalizeKnowledgeName,
  validateKnowledgeDescription,
  validateKnowledgeName,
  type KnowledgeBase,
  type KnowledgeBasePatch,
  type VisibilityGrantInput,
} from "./model";

export interface EditKnowledgeBaseDialogProps {
  base: KnowledgeBase;
  onClose: () => void;
  open: boolean;
}

/** 编辑知识库弹窗（保护未保存草稿不被后台刷新覆盖，精准对比可见性与附加授权差异） */
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
  const submitLock = useSubmitLock();
  const mutation = useUpdateKnowledgeBaseMutation(base.id);
  const { reset: resetMutation } = mutation;
  const pending = mutation.isPending || submitLock.isLocked;

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
    if (!pending) {
      onClose();
    }
  };

  const handleSubmit = async () => {
    if (deptAdminMissingManagedDepts || pending) {
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

    // 同步提交锁防止同 tick 重复发起 PATCH 请求。
    await submitLock.runLocked(async () => {
      setRequestError(null);
      try {
        await mutation.mutateAsync(patch);
        onClose();
      } catch (failure) {
        setRequestError(knowledgeErrorMessage(failure));
      }
    });
  };

  return (
    <Modal
      closeDisabled={pending}
      description="名称、说明与可见范围会同步更新到知识库目录中。"
      footer={
        <>
          <Button
            disabled={pending}
            onClick={close}
            type="button"
            variant="outline"
          >
            取消
          </Button>
          <Button
            disabled={pending || deptAdminMissingManagedDepts}
            form={formId}
            type="submit"
            variant="primary"
          >
            {pending ? "正在保存…" : "保存知识库"}
          </Button>
        </>
      }
      onClose={close}
      open={open}
      panelClassName={
        visibilityScope === "department"
          ? "w-full max-w-[min(42rem,calc(100vw-1.5rem))]"
          : undefined
      }
      title="编辑知识库"
    >
      <form
        className={FORM_STACK_CLASS}
        id={formId}
        onSubmit={(event) => {
          event.preventDefault();
          void handleSubmit();
        }}
      >
        <label className={FIELD_CLASS} htmlFor={`${formId}-edit-name`}>
          <span>知识库名称</span>
          <input
            aria-label="知识库名称"
            aria-describedby={nameError ? nameErrorId : undefined}
            aria-invalid={nameError ? true : undefined}
            autoComplete="off"
            className={FIELD_INPUT_CLASS}
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

        <label className={FIELD_CLASS} htmlFor={`${formId}-edit-description`}>
          <span>说明</span>
          <input
            aria-label="说明"
            aria-describedby={
              descriptionError ? descriptionErrorId : "description-backend-note"
            }
            aria-invalid={descriptionError ? true : undefined}
            autoComplete="off"
            className={FIELD_INPUT_CLASS}
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
          disabled={pending}
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
            disabled={pending}
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

        {requestError ? (
          <InlineAlert size="compact" variant="danger">
            {requestError}
          </InlineAlert>
        ) : null}
      </form>
    </Modal>
  );
}
