import { useEffect, useId, useMemo, useRef, useState } from "react";

import { newRequestId } from "../../shared/lib/requestId";
import {
  Button,
  FIELD_CLASS,
  FIELD_INPUT_CLASS,
  FIELD_SELECT_CLASS,
  FORM_STACK_CLASS,
  InlineAlert,
  MODAL_WIDTH_LG,
  NOTICE_WARNING_CLASS,
  VisibilityBadge,
} from "../../shared/ui";
import { Modal } from "../../shared/ui/Modal";
import { WorkspaceFilePicker } from "../../shared/ui/WorkspaceFilePicker";
import { useAdminOrgDepartments } from "../admin/hooks";
import { useAuth } from "../auth/AuthProvider";
import {
  isDeptAdmin,
  isSuperAdmin,
  resolveUserDepartmentName,
} from "../auth/model";
import { skillErrorMessage } from "./errorMessages";
import { useImportSkillMutation } from "./hooks";
import { validateSkillZip } from "./model";

interface SkillPackageDialogProps {
  onClose: () => void;
  onImported: (skillId: string) => void;
  open: boolean;
}

export function SkillPackageDialog({
  onClose,
  onImported,
  open,
}: SkillPackageDialogProps) {
  const { user } = useAuth();
  const formId = useId();
  const inputId = useId();
  const scopeId = useId();
  const departmentIdInput = useId();
  const errorId = useId();
  const deptErrorId = useId();
  const hintId = useId();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const superAdmin = isSuperAdmin(user);
  const deptAdmin = isDeptAdmin(user);
  const managedDeptOptions = useMemo(() => {
    if (!user) {
      return [];
    }
    return user.managed_department_ids.map((id) => ({
      id,
      name: resolveUserDepartmentName(user, id) ?? id,
    }));
  }, [user]);

  const [file, setFile] = useState<File | null>(null);
  const [scope, setScope] = useState<"company" | "department">(
    deptAdmin ? "department" : "company",
  );
  const [ownerDepartmentId, setOwnerDepartmentId] = useState(
    deptAdmin && managedDeptOptions.length === 1
      ? managedDeptOptions[0].id
      : "",
  );
  const [validationError, setValidationError] = useState<string | null>(null);
  const [departmentError, setDepartmentError] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const requestIdRef = useRef<string | null>(null);
  const mutation = useImportSkillMutation();
  const { reset: resetMutation } = mutation;

  const orgDepartments = useAdminOrgDepartments(
    open && superAdmin && scope === "department",
    false,
  );
  const activeOrgDepartments = useMemo(
    () =>
      (orgDepartments.data?.items ?? []).filter(
        (dept) => dept.status === "active",
      ),
    [orgDepartments.data?.items],
  );

  const wasOpenRef = useRef(false);
  // 仅在弹窗从关闭切为打开时初始化默认发布范围与单部门自动预选，
  // 避免打开期间 /auth/me 后台刷新引起 managedDeptOptions 引用变更从而误重置用户已填草稿。
  useEffect(() => {
    const justOpened = open && !wasOpenRef.current;
    wasOpenRef.current = open;
    if (!justOpened) {
      return;
    }
    setScope(deptAdmin ? "department" : "company");
    setOwnerDepartmentId(
      deptAdmin && managedDeptOptions.length === 1
        ? managedDeptOptions[0].id
        : "",
    );
    setValidationError(null);
    setDepartmentError(null);
    setRequestError(null);
    resetMutation();
  }, [deptAdmin, managedDeptOptions, open, resetMutation]);

  const noManagedDepartments = deptAdmin && managedDeptOptions.length === 0;
  const hasDepartmentControl =
    !noManagedDepartments && !(superAdmin && orgDepartments.isPending);

  const resetTransientState = () => {
    setValidationError(null);
    setDepartmentError(null);
    setRequestError(null);
    mutation.reset();
  };

  const close = () => {
    if (!mutation.isPending) {
      onClose();
    }
  };

  const handleSubmit = async () => {
    if (noManagedDepartments) {
      return;
    }
    const validation = validateSkillZip(file);
    const trimmedDeptId = ownerDepartmentId.trim();
    const nextDepartmentError =
      scope === "department" && !trimmedDeptId
        ? "请选择或填写部门技能的归属部门。"
        : null;
    setValidationError(validation);
    setDepartmentError(nextDepartmentError);
    if (validation || nextDepartmentError || !file || mutation.isPending) {
      return;
    }
    const requestId = requestIdRef.current ?? newRequestId();
    requestIdRef.current = requestId;
    setRequestError(null);
    try {
      const details = await mutation.mutateAsync({
        file,
        ownerDepartmentId: scope === "department" ? trimmedDeptId : undefined,
        requestId,
        scope,
      });
      requestIdRef.current = null;
      setFile(null);
      setValidationError(null);
      setDepartmentError(null);
      onImported(details.id);
    } catch (error) {
      // 同一文件重试沿用请求标识，避免网络结果不明时创建第二份技能。
      setRequestError(skillErrorMessage(error));
    }
  };

  return (
    <Modal
      closeDisabled={mutation.isPending}
      description="仅首发导入会创建技能身份；重复 slug 由后端拒绝，不会覆盖已有技能。"
      footer={
        <>
          <Button
            disabled={mutation.isPending}
            onClick={close}
            type="button"
            variant="outline"
          >
            取消
          </Button>
          <Button
            disabled={mutation.isPending || !file || noManagedDepartments}
            form={formId}
            type="submit"
            variant="primary"
          >
            {mutation.isPending ? "正在检查并导入…" : "导入并查看检查结果"}
          </Button>
        </>
      }
      onClose={close}
      open={open}
      panelClassName={MODAL_WIDTH_LG}
      title="导入技能包"
    >
      <form
        className={FORM_STACK_CLASS}
        id={formId}
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          void handleSubmit();
        }}
      >
        <div className={NOTICE_WARNING_CLASS}>
          ZIP 根目录必须包含 <code>SKILL.md</code>，可选目录为{" "}
          <code>scripts/</code>、<code>templates/</code> 和{" "}
          <code>references/</code>。后端会校验路径安全、解压边界、依赖与脚本
          Schema。
        </div>

        {/* 可见范围与归属部门选择：部门管理员固定部门级，超级管理员可选全公司或部门共享 */}
        {deptAdmin ? (
          <div className={FIELD_CLASS}>
            <span>发布范围</span>
            <div className="flex items-center gap-2 pt-0.5">
              <VisibilityBadge scope="department" />
              <small className="text-xs text-text-tertiary">
                部门管理员仅可导入所属管辖部门的部门共享技能。
              </small>
            </div>
          </div>
        ) : (
          <label className={FIELD_CLASS} htmlFor={scopeId}>
            <span>发布范围</span>
            <select
              aria-label="发布范围"
              className={FIELD_SELECT_CLASS}
              disabled={mutation.isPending}
              id={scopeId}
              onChange={(event) => {
                const nextScope = event.target.value as
                  | "company"
                  | "department";
                setScope(nextScope);
                setDepartmentError(null);
                requestIdRef.current = null;
              }}
              value={scope}
            >
              <option value="company">全公司</option>
              <option value="department">部门共享</option>
            </select>
            <small>
              {scope === "company"
                ? "全公司活跃员工可用，仅支持启停不可删除。"
                : "归属部门及子部门成员可用，支持按需软删除。"}
            </small>
          </label>
        )}

        {scope === "department" ? (
          <div className={FIELD_CLASS}>
            {hasDepartmentControl ? (
              <label htmlFor={departmentIdInput}>归属部门</label>
            ) : (
              <span>归属部门</span>
            )}
            {noManagedDepartments ? (
              <InlineAlert
                description="当前账号尚未配置可管辖部门，无法导入部门技能，请联系企业超级管理员配置管辖范围。"
                title="暂无可管辖部门"
                variant="warning"
              />
            ) : deptAdmin ? (
              <select
                aria-describedby={departmentError ? deptErrorId : undefined}
                aria-invalid={departmentError ? true : undefined}
                aria-label="归属部门"
                className={FIELD_SELECT_CLASS}
                disabled={mutation.isPending}
                id={departmentIdInput}
                onChange={(event) => {
                  setOwnerDepartmentId(event.target.value);
                  setDepartmentError(null);
                  requestIdRef.current = null;
                }}
                value={ownerDepartmentId}
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
                  className={FIELD_INPUT_CLASS}
                  disabled={mutation.isPending}
                  id={departmentIdInput}
                  onChange={(event) => {
                    setOwnerDepartmentId(event.target.value);
                    setDepartmentError(null);
                    requestIdRef.current = null;
                  }}
                  placeholder="请输入归属部门 ID（如 dept_mock_fin）"
                  value={ownerDepartmentId}
                />
                <small>部门目录暂不可用，已降级为手动输入部门 ID。</small>
              </>
            ) : (
              <select
                aria-describedby={departmentError ? deptErrorId : undefined}
                aria-invalid={departmentError ? true : undefined}
                aria-label="归属部门"
                className={FIELD_SELECT_CLASS}
                disabled={mutation.isPending}
                id={departmentIdInput}
                onChange={(event) => {
                  setOwnerDepartmentId(event.target.value);
                  setDepartmentError(null);
                  requestIdRef.current = null;
                }}
                value={ownerDepartmentId}
              >
                <option value="">请选择归属部门</option>
                {activeOrgDepartments.map((dept) => (
                  <option key={dept.id} value={dept.id}>
                    {dept.name}
                  </option>
                ))}
              </select>
            )}
            {departmentError ? (
              <p className="field-error" id={deptErrorId} role="alert">
                {departmentError}
              </p>
            ) : null}
          </div>
        ) : null}

        <div className={FIELD_CLASS}>
          <span>技能 ZIP</span>
          <WorkspaceFilePicker
            accept=".zip,application/zip"
            describedBy={
              validationError ? `${hintId} ${errorId}` : hintId
            }
            disabled={mutation.isPending || noManagedDepartments}
            file={file}
            focusOnOpen
            id={inputId}
            inputRef={fileInputRef}
            invalid={Boolean(validationError)}
            label="技能 ZIP"
            onChange={(selected) => {
              setFile(selected);
              requestIdRef.current = null;
              resetTransientState();
            }}
            placeholder="未选择技能 ZIP"
            triggerLabel="选择 ZIP 文件"
          />
          <small id={hintId}>
            ZIP 不超过 25 MiB；首次导入成功后版本保持未启用。
          </small>
        </div>
        {validationError ? (
          <p className="field-error" id={errorId} role="alert">
            {validationError}
          </p>
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
