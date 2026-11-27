import { Plus, Trash2 } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";

import {
  CHECKBOX_INPUT_CLASS,
  CHECKBOX_LABEL_CLASS,
  Button,
  EMPTY_INLINE_CLASS,
  FIELD_CLASS,
  FIELD_INPUT_CLASS,
  FIELD_TEXTAREA_CLASS,
  FORM_STACK_CLASS,
  ICON_BUTTON_CLASS,
  ICON_BUTTON_DANGER_CLASS,
  InlineAlert,
  MODAL_WIDTH_XL,
  NOTICE_BRAND_CLASS,
  PAGE_LOADING_CLASS,
  TEXT_BUTTON_CLASS,
  cn,
} from "../../shared/ui";
import { FieldError } from "../../shared/ui/FieldError";
import { Modal } from "../../shared/ui/Modal";
import { defaultOwnerGrant, normalizeVisibilityGrants } from "../access/visibilityGrants";
import { useAuth } from "../auth/AuthProvider";
import { isSuperAdmin } from "../auth/model";
import { AdminVisibilityFields } from "./AdminVisibilityFields";
import { adminErrorMessage, isMcpConflict } from "./errorMessages";
import {
  useCreateMcpServerMutation,
  useMcpServer,
  usePatchMcpServerMutation,
} from "./hooks";
import {
  mcpServerForm,
  validateMcpServerForm,
  validateMcpServerPatchForm,
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
    hasValue: false,
  };
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
  const { user } = useAuth();
  const superAdmin = isSuperAdmin(user);
  const formId = useId();
  const editing = Boolean(serverId);
  const detail = useMcpServer(serverId, open && editing);
  const createMutation = useCreateMcpServerMutation();
  const patchMutation = usePatchMcpServerMutation(serverId ?? "");
  const { reset: resetCreateMutation } = createMutation;
  const { reset: resetPatchMutation } = patchMutation;
  const [values, setValues] = useState<McpServerFormValues>(mcpServerForm);
  const [errors, setErrors] = useState<FieldErrors<McpServerField>>({});
  const [requestError, setRequestError] = useState<string | null>(null);
  const [configVersion, setConfigVersion] = useState<number | null>(null);
  const [loadedIdentity, setLoadedIdentity] = useState<string | null>(null);
  const [grantsEditorResetKey, setGrantsEditorResetKey] = useState(0);
  const rediscoverTouchedRef = useRef(false);
  const submitLock = useRef(false);
  const pending = createMutation.isPending || patchMutation.isPending;

  useEffect(() => {
    if (!open) {
      setLoadedIdentity(null);
      rediscoverTouchedRef.current = false;
      submitLock.current = false;
      return;
    }
    setErrors({});
    setRequestError(null);
    resetCreateMutation();
    resetPatchMutation();
    rediscoverTouchedRef.current = false;
    submitLock.current = false;
    if (!serverId) {
      setValues(mcpServerForm());
      setConfigVersion(null);
      setLoadedIdentity("create");
      setGrantsEditorResetKey((prev) => prev + 1);
    }
  }, [open, resetCreateMutation, resetPatchMutation, serverId]);

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
    // 首次打开编辑时以最新详情初始化；409 刷新仅在冲突处理分支显式覆盖表单与版本号。
    setValues(mcpServerForm(detail.data));
    setConfigVersion(detail.data.config_version);
    setLoadedIdentity(serverId);
    setGrantsEditorResetKey((prev) => prev + 1);
    rediscoverTouchedRef.current = false;
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
    const affectsDiscovery =
      editing &&
      (field === "url" ||
        field === "command" ||
        field === "cwd" ||
        field === "argsText");
    setValues((current) => ({
      ...current,
      [field]: value,
      ...(affectsDiscovery && !rediscoverTouchedRef.current
        ? { rediscover: true }
        : {}),
    }));
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
      ...(editing && !rediscoverTouchedRef.current ? { rediscover: true } : {}),
    }));
    setErrors((current) => ({ ...current, secrets: undefined }));
    setRequestError(null);
  };

  const removeSecret = (rowId: string) => {
    setValues((current) => ({
      ...current,
      secrets: current.secrets.filter((row) => row.id !== rowId),
      ...(editing && !rediscoverTouchedRef.current ? { rediscover: true } : {}),
    }));
    setErrors((current) => ({ ...current, secrets: undefined }));
    setRequestError(null);
  };

  const handleScopeChange = (nextScope: "company" | "department") => {
    setValues((current) => {
      if (nextScope === "company") {
        return {
          ...current,
          visibility_scope: "company",
          visibility_grants: [],
        };
      }
      const trimmedDept = current.owner_department_id.trim();
      const baseServer = detail.data;
      const grants =
        editing &&
        baseServer?.visibility_scope === "department" &&
        trimmedDept &&
        trimmedDept === (baseServer.owner_department_id ?? "")
          ? normalizeVisibilityGrants(
              trimmedDept,
              baseServer.visibility_grants,
              superAdmin,
            )
          : trimmedDept
            ? [defaultOwnerGrant(trimmedDept, superAdmin)]
            : [];
      return {
        ...current,
        visibility_scope: "department",
        visibility_grants: grants,
      };
    });
    setErrors((current) => ({ ...current, owner_department_id: undefined }));
    setRequestError(null);
  };

  const handleDepartmentChange = (nextDeptId: string) => {
    const trimmed = nextDeptId.trim();
    const baseServer = detail.data;
    setValues((current) => {
      const restoredOriginal =
        editing &&
        baseServer?.visibility_scope === "department" &&
        trimmed === (baseServer.owner_department_id ?? "");
      if (restoredOriginal) {
        setGrantsEditorResetKey((prev) => prev + 1);
      }
      return {
        ...current,
        owner_department_id: nextDeptId,
        visibility_grants: restoredOriginal && baseServer
          ? normalizeVisibilityGrants(
              trimmed,
              baseServer.visibility_grants,
              superAdmin,
            )
          : trimmed
            ? [defaultOwnerGrant(trimmed, superAdmin)]
            : [],
      };
    });
    setErrors((current) => ({ ...current, owner_department_id: undefined }));
    setRequestError(null);
  };

  const handleSubmit = async () => {
    if (submitLock.current || pending) {
      return;
    }

    if (editing) {
      if (configVersion === null || !detail.data) {
        setRequestError("尚未读取到最新 config_version，不能提交修改。");
        return;
      }
      const patchResult = validateMcpServerPatchForm(
        values,
        detail.data,
        configVersion,
      );
      setErrors(patchResult.errors);
      if (Object.keys(patchResult.errors).length > 0) {
        return;
      }
      if (!patchResult.hasChanges || !patchResult.input) {
        onClose();
        return;
      }

      submitLock.current = true;
      setRequestError(null);
      try {
        await patchMutation.mutateAsync(patchResult.input);
        onClose();
      } catch (failure) {
        if (isMcpConflict(failure)) {
          // 409 表示其他管理员已保存新版本；禁止沿用旧版本号提交。
          setConfigVersion(null);
          setRequestError(
            "配置已被并发修改，正在读取最新详情；读取完成前不能提交。",
          );
          try {
            const refreshed = await detail.refetch();
            if (refreshed.isSuccess && refreshed.data) {
              setValues(mcpServerForm(refreshed.data));
              setConfigVersion(refreshed.data.config_version);
              setGrantsEditorResetKey((prev) => prev + 1);
              rediscoverTouchedRef.current = false;
              setErrors({});
              setRequestError(
                `配置已被并发修改。已载入最新配置与版本号，旧草稿未保存，请核对后重新提交。（${refreshed.data.name} · config_version ${refreshed.data.config_version}）`,
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
      return;
    }

    const createResult = validateMcpServerForm(values);
    setErrors(createResult.errors);
    if (!createResult.input) {
      return;
    }
    submitLock.current = true;
    setRequestError(null);
    try {
      await createMutation.mutateAsync(createResult.input);
      onClose();
    } catch (failure) {
      setRequestError(adminErrorMessage(failure));
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
  const editReady = (!editing || loadedIdentity === serverId) && versionReady;

  return (
    <Modal
      closeDisabled={pending}
      description={
        editing
          ? "支持保密局部更新：既有加密凭据留空即保留原值，可按需勾选保存后重新发现工具。"
          : "创建时后端会真实连接 MCP Server 并发现工具，连接成功后才写入目录。"
      }
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
            disabled={pending || !editReady}
            form={formId}
            type="submit"
            variant="primary"
          >
            {pending
              ? editing
                ? values.rediscover
                  ? "正在保存并重发现工具…"
                  : "正在保存修改…"
                : "正在连接并发现工具…"
              : editing
                ? "保存修改"
                : "创建并发现工具"}
          </Button>
        </>
      }
      onClose={close}
      open={open}
      panelClassName={MODAL_WIDTH_XL}
      title={editing ? "编辑 MCP Server" : "新建 MCP Server"}
    >
      {editing && !editFormAvailable ? (
        detail.isError ? (
          <InlineAlert
            action={
              <button
                className={TEXT_BUTTON_CLASS}
                onClick={() => void detail.refetch()}
                type="button"
              >
                重试
              </button>
            }
            size="compact"
            variant="danger"
          >
            {adminErrorMessage(detail.error)}
          </InlineAlert>
        ) : (
          <div className={PAGE_LOADING_CLASS} role="status">
            正在读取 Server 最新详情…
          </div>
        )
      ) : (
        <form
          className={FORM_STACK_CLASS}
          id={formId}
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            void handleSubmit();
          }}
        >
          <fieldset
            className={cn(FORM_STACK_CLASS, "m-0 border-0 p-0")}
            disabled={pending || !editReady}
          >
            <label className={FIELD_CLASS} htmlFor={fieldId("name")}>
              <span>Server 名称</span>
              <input
                aria-describedby={
                  errors.name ? fieldId("name-error") : undefined
                }
                aria-invalid={Boolean(errors.name)}
                autoComplete="off"
                className={FIELD_INPUT_CLASS}
                data-autofocus
                disabled={pending}
                id={fieldId("name")}
                onChange={(event) => update("name", event.target.value)}
                placeholder="例如：内部检索服务"
                value={values.name}
              />
            </label>
            <FieldError error={errors.name} id={fieldId("name-error")} />

            <fieldset className="m-0 border-0 p-0">
              <legend className="mb-[7px] text-xs font-semibold text-text-primary">
                Transport
              </legend>
              <div className="grid grid-cols-1 gap-[7px] sm:grid-cols-2">
                <label className="relative min-w-0 cursor-pointer">
                  <input
                    checked={values.transport === "streamable_http"}
                    className="peer sr-only"
                    disabled={pending || editing}
                    name={`${formId}-transport`}
                    onChange={() => update("transport", "streamable_http")}
                    type="radio"
                  />
                  <span className="flex min-h-10 items-center justify-center rounded-[9px] border border-border-default bg-canvas px-2.5 text-xs font-semibold text-text-secondary transition-colors peer-checked:border-brand-border peer-checked:bg-brand-subtle peer-checked:text-brand peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-ring-focus peer-disabled:cursor-not-allowed peer-disabled:opacity-55">
                    Streamable HTTP
                  </span>
                </label>
                <label className="relative min-w-0 cursor-pointer">
                  <input
                    checked={values.transport === "stdio"}
                    className="peer sr-only"
                    disabled={pending || editing}
                    name={`${formId}-transport`}
                    onChange={() => update("transport", "stdio")}
                    type="radio"
                  />
                  <span className="flex min-h-10 items-center justify-center rounded-[9px] border border-border-default bg-canvas px-2.5 text-xs font-semibold text-text-secondary transition-colors peer-checked:border-brand-border peer-checked:bg-brand-subtle peer-checked:text-brand peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-ring-focus peer-disabled:cursor-not-allowed peer-disabled:opacity-55">
                    STDIO
                  </span>
                </label>
              </div>
              {editing ? (
                <small className="mt-1 block text-xs text-text-tertiary">
                  编辑模式下不可切换传输协议；如需更改协议请新建 MCP Server。
                </small>
              ) : null}
            </fieldset>

            {values.transport === "streamable_http" ? (
              <>
                <label className={FIELD_CLASS} htmlFor={fieldId("url")}>
                  <span>HTTPS MCP URL</span>
                  <input
                    aria-describedby={
                      errors.url
                        ? fieldId("url-error")
                        : fieldId("url-help")
                    }
                    aria-invalid={Boolean(errors.url)}
                    autoComplete="url"
                    className={FIELD_INPUT_CLASS}
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
                <FieldError error={errors.url} id={fieldId("url-error")} />
              </>
            ) : (
              <>
                <label className={FIELD_CLASS} htmlFor={fieldId("command")}>
                  <span>绝对命令路径</span>
                  <input
                    aria-invalid={Boolean(errors.command)}
                    autoComplete="off"
                    className={FIELD_INPUT_CLASS}
                    disabled={pending}
                    id={fieldId("command")}
                    onChange={(event) => update("command", event.target.value)}
                    placeholder="/usr/local/bin/mcp-server"
                    value={values.command}
                  />
                </label>
                <FieldError
                  error={errors.command}
                  id={fieldId("command-error")}
                />
                <label className={FIELD_CLASS} htmlFor={fieldId("cwd")}>
                  <span>工作目录（可选，必须是绝对路径）</span>
                  <input
                    aria-invalid={Boolean(errors.cwd)}
                    autoComplete="off"
                    className={FIELD_INPUT_CLASS}
                    disabled={pending}
                    id={fieldId("cwd")}
                    onChange={(event) => update("cwd", event.target.value)}
                    placeholder="/srv/mcp"
                    value={values.cwd}
                  />
                </label>
                <FieldError error={errors.cwd} id={fieldId("cwd-error")} />
                <label className={FIELD_CLASS} htmlFor={fieldId("args")}>
                  <span>参数（每行一个，不使用 shell 引号）</span>
                  <textarea
                    aria-invalid={Boolean(errors.argsText)}
                    className={FIELD_TEXTAREA_CLASS}
                    disabled={pending}
                    id={fieldId("args")}
                    onChange={(event) => update("argsText", event.target.value)}
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

            <AdminVisibilityFields
              disabled={pending || !editReady}
              formId={formId}
              grantsEditorResetKey={grantsEditorResetKey}
              onDepartmentChange={handleDepartmentChange}
              onGrantsChange={(nextGrants) =>
                setValues((current) => ({
                  ...current,
                  visibility_grants: nextGrants,
                }))
              }
              onScopeChange={handleScopeChange}
              open={open}
              ownerDepartmentError={errors.owner_department_id}
              ownerDepartmentId={values.owner_department_id}
              resourceLabel="MCP Server"
              visibilityGrants={values.visibility_grants}
              visibilityScope={values.visibility_scope}
            />

            <section
              aria-labelledby={fieldId("secrets-title")}
              className="rounded-[10px] border border-border-default bg-subtle p-[13px]"
            >
              <header className="mb-2.5 flex flex-col items-start justify-between gap-3 sm:flex-row">
                <div>
                  <h3
                    className="text-xs font-bold text-text-primary"
                    id={fieldId("secrets-title")}
                  >
                    连接秘密
                  </h3>
                  <p className="mt-[3px] text-xs text-text-tertiary">
                    {values.transport === "streamable_http"
                      ? "名称作为 HTTPS header，值只提交给后端加密存储。"
                      : "名称作为子进程环境变量，值只提交给后端加密存储。"}
                  </p>
                </div>
                <Button
                  className="w-full sm:w-auto"
                  disabled={pending || !editReady}
                  onClick={() =>
                    setValues((current) => ({
                      ...current,
                      secrets: [...current.secrets, newSecretRow()],
                    }))
                  }
                  size="sm"
                  type="button"
                  variant="outline"
                >
                  <Plus
                    aria-hidden="true"
                    focusable="false"
                    size={13}
                    strokeWidth={1.7}
                  />
                  添加秘密
                </Button>
              </header>

              {values.secrets.length === 0 ? (
                <p className={EMPTY_INLINE_CLASS}>
                  当前没有秘密。需要认证的 Server 请添加对应名称和值。
                </p>
              ) : (
                <div className="flex flex-col gap-[9px]">
                  {values.secrets.map((row, index) => (
                    <div
                      className="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-2 rounded-[9px] border border-border-default bg-canvas p-2.5 sm:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)_auto] sm:items-end"
                      key={row.id}
                    >
                      <label
                        className={cn(
                          FIELD_CLASS,
                          "col-start-1 sm:col-start-auto",
                        )}
                        htmlFor={`${row.id}-name`}
                      >
                        <span>秘密名称 {index + 1}</span>
                        <input
                          autoComplete="off"
                          className={FIELD_INPUT_CLASS}
                          disabled={pending || row.existing}
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
                        className={cn(
                          FIELD_CLASS,
                          "col-start-1 row-start-2 sm:col-start-auto sm:row-start-auto",
                        )}
                        htmlFor={`${row.id}-value`}
                      >
                        <span>
                          {row.existing && row.hasValue !== false
                            ? "秘密值（已加密保存，留空保留原值）"
                            : "秘密值"}
                        </span>
                        <input
                          autoComplete="new-password"
                          className={FIELD_INPUT_CLASS}
                          disabled={pending}
                          id={`${row.id}-value`}
                          onChange={(event) =>
                            updateSecret(row.id, {
                              value: event.target.value,
                            })
                          }
                          placeholder={
                            row.existing && row.hasValue !== false
                              ? "留空保留原值"
                              : undefined
                          }
                          type="password"
                          value={row.value}
                        />
                      </label>
                      <button
                        aria-label={`移除秘密 ${row.name || index + 1}`}
                        className={cn(
                          ICON_BUTTON_CLASS,
                          ICON_BUTTON_DANGER_CLASS,
                          "col-start-2 row-start-1 mt-[30px] sm:col-start-auto sm:row-start-auto sm:mt-0 sm:mb-[4.5px]",
                        )}
                        disabled={pending}
                        onClick={() => removeSecret(row.id)}
                        title="移除秘密"
                        type="button"
                      >
                        <Trash2
                          aria-hidden="true"
                          focusable="false"
                          size={15}
                          strokeWidth={1.7}
                        />
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

            {editing ? (
              <label className={CHECKBOX_LABEL_CLASS}>
                <input
                  checked={values.rediscover}
                  className={CHECKBOX_INPUT_CLASS}
                  disabled={pending}
                  onChange={(event) => {
                    rediscoverTouchedRef.current = true;
                    setValues((current) => ({
                      ...current,
                      rediscover: event.target.checked,
                    }));
                  }}
                  type="checkbox"
                />
                <span>保存后重新发现工具（rediscover）</span>
              </label>
            ) : null}
          </fieldset>

          {editing && configVersion !== null ? (
            <p className="text-xs leading-[1.65] text-text-tertiary">
              本次提交的 expected_config_version：{configVersion}
            </p>
          ) : null}
          {pending ? (
            <p
              className={cn(
                NOTICE_BRAND_CLASS,
                "before:mt-1 before:h-[7px] before:w-[7px] before:shrink-0 before:animate-pulse before:rounded-full before:bg-current before:content-['']",
              )}
              role="status"
            >
              {editing && !values.rediscover
                ? "正在保存 MCP Server 配置…"
                : "正在连接 MCP Server 并发现工具。后端完成前不会显示连接成功；配置结果以服务端响应为准。"}
            </p>
          ) : null}
          {requestError ? (
            <InlineAlert size="compact" variant="danger">
              {requestError}
            </InlineAlert>
          ) : null}
        </form>
      )}
    </Modal>
  );
}
