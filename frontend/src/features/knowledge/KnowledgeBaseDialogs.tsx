import { useEffect, useId, useState } from "react";
import { useNavigate } from "react-router-dom";

import { useSubmitLock } from "../../shared/lib/useSubmitLock";
import {
  Button,
  CHECKBOX_INPUT_DANGER_CLASS,
  CHECKBOX_LABEL_CLASS,
  CONFIRM_COPY_CLASS,
  InlineAlert,
} from "../../shared/ui";
import { Modal } from "../../shared/ui/Modal";
import { knowledgeErrorMessage } from "./errorMessages";
import { useDeleteKnowledgeBaseMutation } from "./hooks";
import type { KnowledgeBase } from "./model";

export { EditKnowledgeBaseDialog } from "./EditKnowledgeBaseDialog";
export { NewKnowledgeBaseDialog } from "./NewKnowledgeBaseDialog";

interface DeleteKnowledgeBaseDialogProps {
  base: KnowledgeBase;
  onClose: () => void;
  onDeleted?: () => void;
  open: boolean;
}

/** 删除知识库确认弹窗（需显式勾选确认，并通过 useSubmitLock 防止连点） */
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
  const submitLock = useSubmitLock();
  const mutation = useDeleteKnowledgeBaseMutation(base.id);
  const { reset: resetMutation } = mutation;
  const pending = mutation.isPending || submitLock.isLocked;

  useEffect(() => {
    if (open) {
      setConfirmed(false);
      setRequestError(null);
      resetMutation();
    }
  }, [open, resetMutation]);

  const close = () => {
    if (!pending) {
      onClose();
    }
  };

  const handleDelete = async () => {
    if (!confirmed || pending) {
      return;
    }
    await submitLock.runLocked(async () => {
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
    });
  };

  return (
    <Modal
      closeDisabled={pending}
      description="知识库删除后，其项目关联也会移除。"
      footer={
        <>
          <Button
            data-autofocus
            disabled={pending}
            onClick={close}
            type="button"
            variant="outline"
          >
            取消
          </Button>
          <Button
            disabled={!confirmed || pending}
            onClick={() => void handleDelete()}
            type="button"
            variant="danger"
          >
            {pending ? "正在删除…" : "删除知识库"}
          </Button>
        </>
      }
      onClose={close}
      open={open}
      title="删除知识库"
    >
      <p className={CONFIRM_COPY_CLASS}>
        确定删除“{base.name}”吗？知识库中的文档、版本和项目关联将无法继续访问。
      </p>
      <label className={CHECKBOX_LABEL_CLASS} htmlFor={checkboxId}>
        <input
          checked={confirmed}
          className={CHECKBOX_INPUT_DANGER_CLASS}
          id={checkboxId}
          onChange={(event) => setConfirmed(event.target.checked)}
          type="checkbox"
        />
        <span>我已确认要删除这个知识库</span>
      </label>
      {requestError ? (
        <InlineAlert size="compact" variant="danger">
          {requestError}
        </InlineAlert>
      ) : null}
    </Modal>
  );
}
