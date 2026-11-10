import { useEffect, useId, useState } from "react";
import { useNavigate } from "react-router-dom";

import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Modal } from "../../shared/ui/Modal";
import { projectErrorMessage } from "./errorMessages";
import {
  useCreateProjectMutation,
  useRenameProjectMutation,
} from "./hooks";
import {
  normalizeProjectName,
  type Project,
  validateProjectName,
} from "./model";

interface NewProjectDialogProps {
  onClose: () => void;
  open: boolean;
}

export function NewProjectDialog({
  onClose,
  open,
}: NewProjectDialogProps) {
  const navigate = useNavigate();
  const formId = useId();
  const errorId = useId();
  const [name, setName] = useState("");
  const [validationError, setValidationError] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const mutation = useCreateProjectMutation();

  useEffect(() => {
    if (open) {
      setName("");
      setValidationError(null);
      setRequestError(null);
      mutation.reset();
    }
    // 每次打开都从干净表单开始，不复用上一次的失败状态。
  }, [open]);

  const close = () => {
    if (!mutation.isPending) {
      onClose();
    }
  };

  const handleSubmit = async () => {
    const error = validateProjectName(name);
    if (error) {
      setValidationError(error);
      return;
    }

    setValidationError(null);
    setRequestError(null);
    try {
      const project = await mutation.mutateAsync({
        name: normalizeProjectName(name),
      });
      onClose();
      navigate(`/projects/${encodeURIComponent(project.id)}/overview`);
    } catch (requestFailure) {
      setRequestError(projectErrorMessage(requestFailure));
    }
  };

  return (
    <Modal
      closeDisabled={mutation.isPending}
      description="项目用于组织同一主题下的对话。名称可在创建后修改。"
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
            disabled={mutation.isPending}
            form={formId}
            type="submit"
          >
            {mutation.isPending ? "正在创建…" : "创建项目"}
          </button>
        </>
      }
      onClose={close}
      open={open}
      title="新建项目"
    >
      <form
        id={formId}
        onSubmit={(event) => {
          event.preventDefault();
          void handleSubmit();
        }}
      >
        <label className="workspace-field" htmlFor={`${formId}-name`}>
          <span>项目名称</span>
          <input
            aria-label="项目名称"
            aria-describedby={validationError ? errorId : undefined}
            aria-invalid={Boolean(validationError)}
            autoComplete="off"
            data-autofocus
            id={`${formId}-name`}
            onChange={(event) => {
              setName(event.target.value);
              if (validationError) {
                setValidationError(null);
              }
              if (requestError) {
                setRequestError(null);
              }
            }}
            placeholder="例如：产品研究"
            value={name}
          />
          <small>1–120 个 Unicode 码点，不能包含控制字符。</small>
        </label>
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

interface RenameProjectDialogProps {
  onClose: () => void;
  open: boolean;
  project: Project;
}

export function RenameProjectDialog({
  onClose,
  open,
  project,
}: RenameProjectDialogProps) {
  const formId = useId();
  const errorId = useId();
  const [name, setName] = useState(project.name);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const mutation = useRenameProjectMutation(project.id);

  useEffect(() => {
    if (open) {
      setName(project.name);
      setValidationError(null);
      setRequestError(null);
      mutation.reset();
    }
  }, [open, project.name]);

  const close = () => {
    if (!mutation.isPending) {
      onClose();
    }
  };

  const handleSubmit = async () => {
    const error = validateProjectName(name);
    if (error) {
      setValidationError(error);
      return;
    }

    setValidationError(null);
    setRequestError(null);
    try {
      await mutation.mutateAsync({
        name: normalizeProjectName(name),
      });
      onClose();
    } catch (requestFailure) {
      setRequestError(projectErrorMessage(requestFailure));
    }
  };

  return (
    <Modal
      closeDisabled={mutation.isPending}
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
            disabled={mutation.isPending}
            form={formId}
            type="submit"
          >
            {mutation.isPending ? "正在保存…" : "保存名称"}
          </button>
        </>
      }
      onClose={close}
      open={open}
      title="重命名项目"
    >
      <form
        id={formId}
        onSubmit={(event) => {
          event.preventDefault();
          void handleSubmit();
        }}
      >
        <label className="workspace-field" htmlFor={`${formId}-name`}>
          <span>项目名称</span>
          <input
            aria-label="项目名称"
            aria-describedby={validationError ? errorId : undefined}
            aria-invalid={Boolean(validationError)}
            autoComplete="off"
            data-autofocus
            id={`${formId}-name`}
            onChange={(event) => {
              setName(event.target.value);
              setValidationError(null);
              setRequestError(null);
            }}
            value={name}
          />
          <small>1–120 个 Unicode 码点，不能包含控制字符。</small>
        </label>
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

