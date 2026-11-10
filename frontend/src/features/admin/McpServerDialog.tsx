import { useEffect, useId, useRef, useState } from "react";

import { ErrorBanner } from "../../shared/ui/ErrorBanner";
import { Icon } from "../../shared/ui/Icons";
import { Modal } from "../../shared/ui/Modal";
import { adminErrorMessage, isMcpConflict } from "./errorMessages";
import {
  useCreateMcpServerMutation,
  useMcpServer,
  useUpdateMcpServerMutation,
} from "./hooks";
import {
  mcpServerForm,
  validateMcpServerForm,
  type FieldErrors,
  type McpSecretRow,
  type McpServerField,
  type McpServerFormValues,
} from "./model";

let secretRowSequence = 0;

function newSecretRow(): McpSecretRow {
  secretRowSequence += 1;
  return {
    id: `new-secret-${secretRowSequence}`,
    name: "",
    value: "",
    existing: false,
  };
}

function FieldError({
  error,
  id,
}: {
  error?: string;
  id: string;
}) {
  return error ? (
    <p className="field-error" id={id} role="alert">
      {error}
    </p>
  ) : null;
}

interface McpServerDialogProps {
  onClose: () => void;
  open: boolean;
  serverId?: string;
}

export function McpServerDialog({
  onClose,
  open,
  serverId,
}: McpServerDialogProps) {
  const formId = useId();
  const editing = Boolean(serverId);
  const detail = useMcpServer(serverId, open && editing);
  const createMutation = useCreateMcpServerMutation();
  const updateMutation = useUpdateMcpServerMutation(serverId ?? "");
  const [values, setValues] = useState<McpServerFormValues>(
    mcpServerForm,
  );
  const [errors, setErrors] = useState<FieldErrors<McpServerField>>(
    {},
  );
  const [requestError, setRequestError] = useState<string | null>(null);
  const [configVersion, setConfigVersion] = useState<number | null>(
    null,
  );
  const [loadedIdentity, setLoadedIdentity] = useState<string | null>(
    null,
  );
  const submitLock = useRef(false);
  const pending =
    createMutation.isPending || updateMutation.isPending;

  useEffect(() => {
    if (!open) {
      setLoadedIdentity(null);
      submitLock.current = false;
      return;
    }
    setErrors({});
    setRequestError(null);
    createMutation.reset();
    updateMutation.reset();
    submitLock.current = false;
    if (!serverId) {
      setValues(mcpServerForm());
      setConfigVersion(null);
      setLoadedIdentity("create");
    }
  }, [open, serverId]);

  useEffect(() => {
    if (
      !open ||
      !serverId ||
      !detail.data ||
      !detail.isSuccess ||
      !detail.isFetchedAfterMount ||
      loadedIdentity === serverId
    ) {
      return;
    }
    // 首次打开编辑时以最新详情初始化；409 刷新只更新版本号，不覆盖用户已输入的秘密。
    setValues(mcpServerForm(detail.data));
    setConfigVersion(detail.data.config_version);
    setLoadedIdentity(serverId);
  }, [
    detail.data,
    detail.isFetchedAfterMount,
    detail.isSuccess,
    loadedIdentity,
    open,
    serverId,
  ]);

  const close = () => {
    if (!pending) {
      onClose();
    }
  };

  const update = <Field extends McpServerField>(
    field: Field,
    value: McpServerFormValues[Field],
  ) => {
    setValues((current) => ({ ...current, [field]: value }));
    setErrors((current) => ({ ...current, [field]: undefined }));
    setRequestError(null);
  };

  const updateSecret = (
    rowId: string,
    patch: Partial<Pick<McpSecretRow, "name" | "value">>,
  ) => {
    setValues((current) => ({
      ...current,
      secrets: current.secrets.map((row) =>
        row.id === rowId ? { ...row, ...patch } : row,
      ),
    }));
    setErrors((current) => ({ ...current, secrets: undefined }));
    setRequestError(null);
  };

  const removeSecret = (rowId: string) => {
    setValues((current) => ({
      ...current,
      secrets: current.secrets.filter((row) => row.id !== rowId),
    }));
    setErrors((current) => ({ ...current, secrets: undefined }));
    setRequestError(null);
  };

  const handleSubmit = async () => {
    if (submitLock.current || pending) {
      return;
    }
    const result = validateMcpServerForm(values);
    setErrors(result.errors);
    if (!result.input) {
      return;
    }
    if (editing && configVersion === null) {
      setRequestError(
        "尚未读取到最新 config_version，不能提交完整替换。",
      );
      return;
    }

    submitLock.current = true;
    setRequestError(null);
    try {
      if (editing) {
        await updateMutation.mutateAsync({
          expected_config_version: configVersion as number,
          server: result.input,
        });
      } else {
        await createMutation.mutateAsync(result.input);
      }
      onClose();
    } catch (failure) {
      if (editing && isMcpConflict(failure)) {
        // 409 表示其他管理员已保存新版本；旧草稿不能与新版本号组合提交。
        setConfigVersion(null);
        setRequestError(
          "配置已被并发修改，正在读取最新详情；读取完成前不能提交。",
        );
        try {
          const refreshed = await detail.refetch();
          if (refreshed.isSuccess && refreshed.data) {
            // 以服务端最新详情替换整份草稿；秘密只保留脱敏名称，值全部清空。
            setValues(mcpServerForm(refreshed.data));
            setConfigVersion(refreshed.data.config_version);
            setErrors({});
            setRequestError(
              `配置已被并发修改。已载入最新配置，旧草稿未保存，请重新输入秘密后提交。（${refreshed.data.name} · config_version ${refreshed.data.config_version}）`,
            );
          } else {
            setConfigVersion(null);
            setRequestError(
              "配置已被并发修改，且暂时无法读取最新详情。请关闭后重新打开编辑。",
            );
          }
        } catch {
          setConfigVersion(null);
          setRequestError(
            "配置已被并发修改，且暂时无法读取最新详情。请关闭后重新打开编辑。",
          );
        }
      } else {
        setRequestError(adminErrorMessage(failure));
      }
    } finally {
      submitLock.current = false;
    }
  };

  const fieldId = (name: string) => `${formId}-${name}`;
  const editFormAvailable =
    !editing ||
    loadedIdentity === serverId ||
    (detail.isSuccess && detail.isFetchedAfterMount);
  const versionReady = !editing || configVersion !== null;
  const editReady =
    (!editing || loadedIdentity === serverId) && versionReady;

  return (
    <Modal
      closeDisabled={pending}
      description={
        editing
          ? "保存会以当前 config_version 完整替换配置，并重新连接、发现工具。"
          : "创建时后端会真实连接 MCP Server 并发现工具，连接成功后才写入目录。"
      }
      footer={
        <>
          <button
            className="button-quiet"
            disabled={pending}
            onClick={close}
            type="button"
          >
            取消
          </button>
          <button
            className="button-primary"
            disabled={pending || !editReady}
            form={formId}
            type="submit"
          >
            {pending
              ? "正在连接并发现工具…"
              : editing
                ? "完整替换并发现工具"
                : "创建并发现工具"}
          </button>
        </>
      }
      onClose={close}
      open={open}
      panelClassName="admin-modal admin-mcp-modal"
      title={editing ? "编辑 MCP Server" : "新建 MCP Server"}
    >
      {editing && !editFormAvailable ? (
        detail.isError ? (
          <ErrorBanner
            message={adminErrorMessage(detail.error)}
            onRetry={() => void detail.refetch()}
          />
        ) : (
          <div
            className="page-loading admin-dialog-loading"
            role="status"
          >
            正在读取 Server 最新详情…
          </div>
        )
      ) : (
        <form
          className="admin-form"
          id={formId}
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            void handleSubmit();
          }}
        >
          <fieldset disabled={pending || !editReady}>
            {editing ? (
              <p className="admin-inline-warning">
                编辑是完整替换：必须重新输入全部秘密。未重新输入或显式移除的旧秘密会被删除。
              </p>
            ) : null}

            <label className="workspace-field" htmlFor={fieldId("name")}>
              <span>Server 名称</span>
              <input
                aria-describedby={
                  errors.name ? fieldId("name-error") : undefined
                }
                aria-invalid={Boolean(errors.name)}
                autoComplete="off"
                data-autofocus
                disabled={pending}
                id={fieldId("name")}
                onChange={(event) => update("name", event.target.value)}
                placeholder="例如：内部检索服务"
                value={values.name}
              />
            </label>
            <FieldError
              error={errors.name}
              id={fieldId("name-error")}
            />

            <fieldset className="admin-transport-fieldset">
              <legend>Transport</legend>
              <div className="admin-segmented">
                <label>
                  <input
                    checked={values.transport === "streamable_http"}
                    disabled={pending}
                    name={`${formId}-transport`}
                    onChange={() => update("transport", "streamable_http")}
                    type="radio"
                  />
                  <span>Streamable HTTP</span>
                </label>
                <label>
                  <input
                    checked={values.transport === "stdio"}
                    disabled={pending}
                    name={`${formId}-transport`}
                    onChange={() => update("transport", "stdio")}
                    type="radio"
                  />
                  <span>STDIO</span>
                </label>
              </div>
            </fieldset>

            {values.transport === "streamable_http" ? (
              <>
                <label className="workspace-field" htmlFor={fieldId("url")}>
                  <span>HTTPS MCP URL</span>
                  <input
                    aria-describedby={
                      errors.url
                        ? fieldId("url-error")
                        : fieldId("url-help")
                    }
                    aria-invalid={Boolean(errors.url)}
                    autoComplete="url"
                    disabled={pending}
                    id={fieldId("url")}
                    onChange={(event) => update("url", event.target.value)}
                    placeholder="https://mcp.example/mcp"
                    value={values.url}
                  />
                  <small id={fieldId("url-help")}>
                    只允许 HTTPS，且不能包含账号、查询参数或 URL 片段。
                  </small>
                </label>
                <FieldError
                  error={errors.url}
                  id={fieldId("url-error")}
                />
              </>
            ) : (
              <>
                <label
                  className="workspace-field"
                  htmlFor={fieldId("command")}
                >
                  <span>绝对命令路径</span>
                  <input
                    aria-invalid={Boolean(errors.command)}
                    autoComplete="off"
                    disabled={pending}
                    id={fieldId("command")}
                    onChange={(event) =>
                      update("command", event.target.value)
                    }
                    placeholder="/usr/local/bin/mcp-server"
                    value={values.command}
                  />
                </label>
                <FieldError
                  error={errors.command}
                  id={fieldId("command-error")}
                />
                <label
                  className="workspace-field"
                  htmlFor={fieldId("cwd")}
                >
                  <span>工作目录（可选，必须是绝对路径）</span>
                  <input
                    aria-invalid={Boolean(errors.cwd)}
                    autoComplete="off"
                    disabled={pending}
                    id={fieldId("cwd")}
                    onChange={(event) => update("cwd", event.target.value)}
                    placeholder="/srv/mcp"
                    value={values.cwd}
                  />
                </label>
                <FieldError
                  error={errors.cwd}
                  id={fieldId("cwd-error")}
                />
                <label
                  className="workspace-field"
                  htmlFor={fieldId("args")}
                >
                  <span>参数（每行一个，不使用 shell 引号）</span>
                  <textarea
                    aria-invalid={Boolean(errors.argsText)}
                    disabled={pending}
                    id={fieldId("args")}
                    onChange={(event) =>
                      update("argsText", event.target.value)
                    }
                    rows={4}
                    value={values.argsText}
                  />
                </label>
                <FieldError
                  error={errors.argsText}
                  id={fieldId("args-error")}
                />
              </>
            )}

            <section
              aria-labelledby={fieldId("secrets-title")}
              className="admin-secrets"
            >
              <header className="admin-secrets-heading">
                <div>
                  <h3 id={fieldId("secrets-title")}>连接秘密</h3>
                  <p>
                    {values.transport === "streamable_http"
                      ? "名称作为 HTTPS header，值只提交给后端。"
                      : "名称作为子进程环境变量，值只提交给后端。"}
                  </p>
                </div>
                <button
                  className="button-quiet admin-secret-add"
                  disabled={pending || !editReady}
                  onClick={() =>
                    setValues((current) => ({
                      ...current,
                      secrets: [...current.secrets, newSecretRow()],
                    }))
                  }
                  type="button"
                >
                  <Icon name="plus" size={13} />
                  添加秘密
                </button>
              </header>

              {values.secrets.length === 0 ? (
                <p className="admin-empty-inline">
                  当前没有秘密。需要认证的 Server 请添加对应名称和值。
                </p>
              ) : (
                <div className="admin-secret-list">
                  {values.secrets.map((row, index) => (
                    <div className="admin-secret-row" key={row.id}>
                      <label
                        className="workspace-field"
                        htmlFor={`${row.id}-name`}
                      >
                        <span>秘密名称 {index + 1}</span>
                        <input
                          autoComplete="off"
                          disabled={pending}
                          id={`${row.id}-name`}
                          onChange={(event) =>
                            updateSecret(row.id, {
                              name: event.target.value,
                            })
                          }
                          spellCheck={false}
                          value={row.name}
                        />
                      </label>
                      <label
                        className="workspace-field"
                        htmlFor={`${row.id}-value`}
                      >
                        <span>
                          {row.existing ? "重新输入秘密值" : "秘密值"}
                        </span>
                        <input
                          autoComplete="new-password"
                          disabled={pending}
                          id={`${row.id}-value`}
                          onChange={(event) =>
                            updateSecret(row.id, {
                              value: event.target.value,
                            })
                          }
                          type="password"
                          value={row.value}
                        />
                      </label>
                      <button
                        aria-label={`移除秘密 ${row.name || index + 1}`}
                        className="icon-button icon-button--danger admin-secret-remove"
                        disabled={pending}
                        onClick={() => removeSecret(row.id)}
                        title="移除秘密"
                        type="button"
                      >
                        <Icon name="trash" size={15} />
                      </button>
                    </div>
                  ))}
                </div>
              )}
              <FieldError
                error={errors.secrets}
                id={fieldId("secrets-error")}
              />
            </section>
          </fieldset>

          {editing && configVersion !== null ? (
            <p className="admin-form-help">
              本次提交的 expected_config_version：{configVersion}
            </p>
          ) : null}
          {pending ? (
            <p className="admin-connection-status" role="status">
              正在连接 MCP Server 并发现工具。后端完成前不会显示连接成功；配置结果以服务端响应为准。
            </p>
          ) : null}
          {requestError ? <ErrorBanner message={requestError} /> : null}
        </form>
      )}
    </Modal>
  );
}
