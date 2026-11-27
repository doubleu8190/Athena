import { useCallback, useEffect, useId, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

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
} from "../access/VisibilityGrantsEditor";
import { useAdminOrgDepartments } from "../admin/hooks";
import { useAuth } from "../auth/AuthProvider";
import { isDeptAdmin, isSuperAdmin } from "../auth/model";
import {
  KnowledgeVisibilityFields,
  resolveOwnerDepartmentName,
} from "./KnowledgeVisibilityFields";
import { knowledgeErrorMessage } from "./errorMessages";
import { useCreateKnowledgeBaseMutation } from "./hooks";
import {
  normalizeKnowledgeDescription,
  normalizeKnowledgeName,
  validateKnowledgeDescription,
  validateKnowledgeName,
  type KnowledgeBase,
  type KnowledgeBaseInput,
  type VisibilityGrantInput,
} from "./model";

export interface NewKnowledgeBaseDialogProps {
  onClose: () => void;
  onCreated?: (base: KnowledgeBase) => void;
  open: boolean;
}

/** 新建知识库弹窗（支持个人、部门共享与全公司三层可见范围及跨部门授权） */
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
  const submitLock = useSubmitLock();
  const mutation = useCreateKnowledgeBaseMutation();
  const { reset: resetMutation } = mutation;
  const pending = mutation.isPending || submitLock.isLocked;

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
    if (!pending) {
      onClose();
    }
  };

  const handleSubmit = async () => {
    if (deptAdminMissingManagedDepts || pending) {
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

    // 同步提交锁防止表单 Enter 与点击按钮同 tick 重复创建知识库。
    await submitLock.runLocked(async () => {
      setRequestError(null);
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
    });
  };

  return (
    <Modal
      closeDisabled={pending}
      description="知识库用于保存可检索的文档与文本资料。"
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
            {pending ? "正在创建…" : "创建知识库"}
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
      title="新建知识库"
    >
      <form
        className={FORM_STACK_CLASS}
        id={formId}
        onSubmit={(event) => {
          event.preventDefault();
          void handleSubmit();
        }}
      >
        <label className={FIELD_CLASS} htmlFor={`${formId}-name`}>
          <span>知识库名称</span>
          <input
            aria-label="知识库名称"
            aria-describedby={nameError ? nameErrorId : undefined}
            aria-invalid={nameError ? true : undefined}
            autoComplete="off"
            className={FIELD_INPUT_CLASS}
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

        <label className={FIELD_CLASS} htmlFor={`${formId}-description`}>
          <span>说明（可选）</span>
          <input
            aria-label="说明"
            aria-describedby={
              descriptionError ? descriptionErrorId : undefined
            }
            aria-invalid={descriptionError ? true : undefined}
            autoComplete="off"
            className={FIELD_INPUT_CLASS}
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
