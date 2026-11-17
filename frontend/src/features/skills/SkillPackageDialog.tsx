import { useId, useRef, useState } from "react";

import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Modal } from "../../shared/ui/Modal";
import { WorkspaceFilePicker } from "../../shared/ui/WorkspaceFilePicker";
import { newRequestId } from "../chat/requestId";
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
  const formId = useId();
  const inputId = useId();
  const errorId = useId();
  const hintId = useId();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [validationError, setValidationError] = useState<string | null>(
    null,
  );
  const [requestError, setRequestError] = useState<string | null>(null);
  const requestIdRef = useRef<string | null>(null);
  const mutation = useImportSkillMutation();

  const resetTransientState = () => {
    setValidationError(null);
    setRequestError(null);
    mutation.reset();
  };

  const close = () => {
    if (!mutation.isPending) {
      onClose();
    }
  };

  const handleSubmit = async () => {
    const validation = validateSkillZip(file);
    setValidationError(validation);
    if (validation || !file || mutation.isPending) {
      return;
    }
    const requestId = requestIdRef.current ?? newRequestId();
    requestIdRef.current = requestId;
    setRequestError(null);
    try {
      const details = await mutation.mutateAsync({ file, requestId });
      requestIdRef.current = null;
      setFile(null);
      setValidationError(null);
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
            disabled={mutation.isPending || !file}
            form={formId}
            type="submit"
          >
            {mutation.isPending ? "正在检查并导入…" : "导入并查看检查结果"}
          </button>
        </>
      }
      onClose={close}
      open={open}
      panelClassName="admin-modal admin-skill-import-modal"
      title="导入技能包"
    >
      <form
        className="admin-form"
        id={formId}
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          void handleSubmit();
        }}
      >
        <div className="admin-inline-notice">
          ZIP 根目录必须包含 <code>SKILL.md</code>，可选目录为{" "}
          <code>scripts/</code>、<code>templates/</code> 和{" "}
          <code>references/</code>。后端会校验路径安全、解压边界、依赖与脚本
          Schema。
        </div>
        <div className="workspace-field">
          <span>技能 ZIP</span>
          <WorkspaceFilePicker
            accept=".zip,application/zip"
            describedBy={
              validationError ? `${hintId} ${errorId}` : hintId
            }
            disabled={mutation.isPending}
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
        {requestError ? <ErrorBanner message={requestError} /> : null}
      </form>
    </Modal>
  );
}
