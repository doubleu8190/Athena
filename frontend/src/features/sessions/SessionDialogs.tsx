import { useEffect, useId, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Modal } from "../../shared/ui/Modal";
import { sessionErrorMessage } from "./errorMessages";
import {
  useDeleteSessionMutation,
  useRenameSessionMutation,
} from "./hooks";
import {
  normalizeSessionTitle,
  sessionDisplayTitle,
  type Session,
  validateSessionTitle,
} from "./model";

interface SessionRenameDialogProps {
  onClose: () => void;
  open: boolean;
  session: Session;
}

export function SessionRenameDialog({
  onClose,
  open,
  session,
}: SessionRenameDialogProps) {
  const formId = useId();
  const inputId = useId();
  const errorId = useId();
  const [title, setTitle] = useState(session.title ?? "");
  const [validationError, setValidationError] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const mutation = useRenameSessionMutation(session.id);

  useEffect(() => {
    if (open) {
      setTitle(session.title ?? "");
      setValidationError(null);
      setRequestError(null);
      mutation.reset();
    }
  }, [open, session.id, session.title]);

  const close = () => {
    if (!mutation.isPending) {
      onClose();
    }
  };

  const handleSubmit = async () => {
    const error = validateSessionTitle(title);
    if (error) {
      setValidationError(error);
      return;
    }

    setValidationError(null);
    setRequestError(null);
    try {
      await mutation.mutateAsync({
        title: normalizeSessionTitle(title),
      });
      onClose();
    } catch (requestFailure) {
      setRequestError(sessionErrorMessage(requestFailure));
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
            {mutation.isPending ? "正在保存…" : "保存标题"}
          </button>
        </>
      }
      onClose={close}
      open={open}
      title="重命名会话"
    >
      <form
        id={formId}
        onSubmit={(event) => {
          event.preventDefault();
          void handleSubmit();
        }}
      >
        <label className="workspace-field" htmlFor={inputId}>
          <span>会话标题</span>
          <input
            aria-label="会话标题"
            aria-describedby={validationError ? errorId : undefined}
            aria-invalid={Boolean(validationError)}
            autoComplete="off"
            data-autofocus
            id={inputId}
            onChange={(event) => {
              setTitle(event.target.value);
              setValidationError(null);
              setRequestError(null);
            }}
            placeholder="给这段对话一个标题"
            value={title}
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

interface SessionDeleteDialogProps {
  onClose: () => void;
  onDeleted?: (session: Session) => void;
  open: boolean;
  session: Session;
}

export function SessionDeleteDialog({
  onClose,
  onDeleted,
  open,
  session,
}: SessionDeleteDialogProps) {
  const location = useLocation();
  const navigate = useNavigate();
  const [requestError, setRequestError] = useState<string | null>(null);
  const mutation = useDeleteSessionMutation(session.id);

  useEffect(() => {
    if (open) {
      setRequestError(null);
      mutation.reset();
    }
  }, [open, session.id]);

  const close = () => {
    if (!mutation.isPending) {
      onClose();
    }
  };

  const handleDelete = async () => {
    setRequestError(null);
    try {
      await mutation.mutateAsync();
      onClose();
      onDeleted?.(session);
      if (
        location.pathname ===
        `/chat/${encodeURIComponent(session.id)}`
      ) {
        navigate("/chat", { replace: true });
      }
    } catch (requestFailure) {
      setRequestError(sessionErrorMessage(requestFailure));
    }
  };

  return (
    <Modal
      closeDisabled={mutation.isPending}
      description="删除后，该会话会从当前用户的目录中移除。"
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
            disabled={mutation.isPending}
            onClick={() => void handleDelete()}
            type="button"
          >
            {mutation.isPending ? "正在删除…" : "确认删除"}
          </button>
        </>
      }
      onClose={close}
      open={open}
      title="删除会话"
    >
      <p className="confirm-copy">
        确定删除“{sessionDisplayTitle(session.title)}”吗？
      </p>
      {requestError ? <ErrorBanner message={requestError} /> : null}
    </Modal>
  );
}

