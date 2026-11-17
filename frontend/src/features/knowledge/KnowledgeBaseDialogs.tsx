import { useEffect, useId, useState } from "react";
import { useNavigate } from "react-router-dom";

import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Modal } from "../../shared/ui/Modal";
import {
  knowledgeErrorMessage,
} from "./errorMessages";
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
} from "./model";

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
  const navigate = useNavigate();
  const formId = useId();
  const nameErrorId = useId();
  const descriptionErrorId = useId();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);
  const [descriptionError, setDescriptionError] = useState<string | null>(
    null,
  );
  const [requestError, setRequestError] = useState<string | null>(null);
  const mutation = useCreateKnowledgeBaseMutation();

  useEffect(() => {
    if (open) {
      setName("");
      setDescription("");
      setNameError(null);
      setDescriptionError(null);
      setRequestError(null);
      mutation.reset();
    }
    // 每次打开都从干净表单开始，避免把上一次失败内容误带入新知识库。
  }, [open]);

  const close = () => {
    if (!mutation.isPending) {
      onClose();
    }
  };

  const handleSubmit = async () => {
    const nextNameError = validateKnowledgeName(name);
    const nextDescriptionError = validateKnowledgeDescription(description);
    setNameError(nextNameError);
    setDescriptionError(nextDescriptionError);
    if (nextNameError || nextDescriptionError) {
      return;
    }

    setRequestError(null);
    const normalizedDescription =
      normalizeKnowledgeDescription(description);
    try {
      const created = await mutation.mutateAsync({
        name: normalizeKnowledgeName(name),
        ...(normalizedDescription
          ? { description: normalizedDescription }
          : {}),
      });
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
            disabled={mutation.isPending}
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
            aria-invalid={Boolean(nameError)}
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
            aria-invalid={Boolean(descriptionError)}
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
  const formId = useId();
  const nameErrorId = useId();
  const descriptionErrorId = useId();
  const [name, setName] = useState(base.name);
  const [description, setDescription] = useState(base.description ?? "");
  const [nameError, setNameError] = useState<string | null>(null);
  const [descriptionError, setDescriptionError] = useState<string | null>(
    null,
  );
  const [requestError, setRequestError] = useState<string | null>(null);
  const mutation = useUpdateKnowledgeBaseMutation(base.id);

  useEffect(() => {
    if (open) {
      setName(base.name);
      setDescription(base.description ?? "");
      setNameError(null);
      setDescriptionError(null);
      setRequestError(null);
      mutation.reset();
    }
  }, [base.description, base.name, open]);

  const close = () => {
    if (!mutation.isPending) {
      onClose();
    }
  };

  const handleSubmit = async () => {
    const nextNameError = validateKnowledgeName(name);
    const nextDescriptionError = validateKnowledgeDescription(
      description,
      base.description,
    );
    setNameError(nextNameError);
    setDescriptionError(nextDescriptionError);
    if (nextNameError || nextDescriptionError) {
      return;
    }

    const normalizedName = normalizeKnowledgeName(name);
    const normalizedDescription =
      normalizeKnowledgeDescription(description);
    const patch: { name?: string; description?: string } = {};
    if (normalizedName !== base.name) {
      patch.name = normalizedName;
    }
    if (
      normalizedDescription &&
      normalizedDescription !== base.description
    ) {
      patch.description = normalizedDescription;
    }
    if (!patch.name && !patch.description) {
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
      description="名称和说明会同步显示在知识库目录中。"
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
            aria-invalid={Boolean(nameError)}
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
            aria-invalid={Boolean(descriptionError)}
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

  useEffect(() => {
    if (open) {
      setConfirmed(false);
      setRequestError(null);
      mutation.reset();
    }
  }, [open]);

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
