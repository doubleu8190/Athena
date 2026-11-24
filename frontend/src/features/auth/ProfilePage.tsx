import { Building2, KeyRound, ShieldAlert, ShieldCheck } from "lucide-react";
import { useEffect, useId, useRef, useState, type FormEvent } from "react";

import { formatDateTime } from "../../shared/lib/format";
import { Badge, Button, InlineAlert, Input } from "../../shared/ui";
import { SettingsLayout } from "../preferences/SettingsLayout";
import { useAuth } from "./AuthProvider";
import { authErrorMessage } from "./errorMessages";
import {
  getUserDisplayName,
  getUserRoleLabel,
  type AuthProvider,
} from "./model";
import {
  useChangePasswordMutation,
  useRefreshAuthenticatedUserMutation,
  useUpdateCurrentUserMutation,
} from "./useAuthMutations";
import { validateDisplayName, validatePassword } from "./validation";

function authProviderLabel(provider: AuthProvider): string {
  switch (provider) {
    case "feishu":
      return "飞书企业账号";
    case "mock":
      return "本地体验账号";
    case "local":
      return "本地应急账号";
  }
}

export function ProfilePage() {
  const { user } = useAuth();
  const displayNameErrorId = useId();
  const passwordErrorId = useId();
  const updateProfile = useUpdateCurrentUserMutation();
  const refreshProfile = useRefreshAuthenticatedUserMutation();
  const updatePassword = useChangePasswordMutation();
  const [displayName, setDisplayName] = useState(user?.display_name ?? "");
  const [displayError, setDisplayError] = useState<string | null>(null);
  const [displayNotice, setDisplayNotice] = useState<string | null>(null);
  const [displayRefreshFailed, setDisplayRefreshFailed] = useState(false);
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [passwordError, setPasswordError] = useState<string | null>(null);
  const [passwordNotice, setPasswordNotice] = useState<string | null>(null);

  const previousUserIdRef = useRef(user?.id);
  // 仅在切换登录账号时重置表单与提示，避免显示名保存成功后被 /auth/me 刷新清空成功状态提示。
  useEffect(() => {
    if (previousUserIdRef.current !== user?.id) {
      previousUserIdRef.current = user?.id;
      setDisplayName(user?.display_name ?? "");
      setDisplayError(null);
      setDisplayNotice(null);
      setDisplayRefreshFailed(false);
    }
  }, [user?.display_name, user?.id]);

  if (!user) return null;

  const displayMutationPending =
    updateProfile.isPending || refreshProfile.isPending;

  const departmentNameById = new Map(
    user.departments.map((department) => [department.id, department.name]),
  );

  const resolvedDisplayName = getUserDisplayName({
    display_name: displayName,
    email: user.email,
    employee_no: user.employee_no,
  });
  const avatarInitial = Array.from(resolvedDisplayName)[0]?.toUpperCase() ?? "A";

  const saveDisplayName = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (displayMutationPending) return;
    setDisplayError(null);
    setDisplayNotice(null);
    const validationError = validateDisplayName(displayName);
    if (validationError) {
      setDisplayError(validationError);
      return;
    }
    try {
      const result = await updateProfile.mutateAsync(displayName.trim());
      if (result.status === "failed") {
        setDisplayRefreshFailed(true);
        setDisplayNotice("显示名已保存，但登录资料刷新失败。请重试刷新。");
      } else {
        setDisplayRefreshFailed(false);
        setDisplayNotice("显示名已更新。");
      }
    } catch (error) {
      setDisplayError(authErrorMessage(error));
    }
  };

  const retryDisplayRefresh = async () => {
    if (displayMutationPending) return;
    setDisplayError(null);
    setDisplayNotice(null);
    try {
      const result = await refreshProfile.mutateAsync();
      if (result.status === "failed") {
        setDisplayRefreshFailed(true);
        setDisplayNotice("登录资料刷新仍然失败。显示名已保存，请稍后重试。");
      } else {
        setDisplayRefreshFailed(false);
        setDisplayNotice("登录资料已刷新。");
      }
    } catch (error) {
      setDisplayError(authErrorMessage(error));
    }
  };

  const savePassword = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (updatePassword.isPending) return;
    setPasswordError(null);
    setPasswordNotice(null);
    const validationError = validatePassword(newPassword);
    if (validationError) {
      setPasswordError(validationError);
      return;
    }
    if (!currentPassword) {
      setPasswordError("请输入当前密码。");
      return;
    }
    if (newPassword !== confirmPassword) {
      setPasswordError("两次输入的新密码不一致。");
      return;
    }
    try {
      await updatePassword.mutateAsync({
        current_password: currentPassword,
        new_password: newPassword,
      });
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      setPasswordNotice("密码已更新，当前设备仍保持登录。");
    } catch (error) {
      setPasswordError(authErrorMessage(error));
    }
  };

  return (
    <SettingsLayout>
      {/* 企业组织名片与显示名设置 */}
      <section
        aria-labelledby="profile-info-title"
        className="rounded-xl border border-border-subtle bg-canvas p-5 shadow-xs sm:p-6"
      >
        <div className="flex flex-wrap items-center justify-between gap-4 border-b border-border-subtle pb-5">
          <div className="flex min-w-0 items-center gap-3.5">
            <span
              aria-hidden="true"
              className="flex h-12 w-12 shrink-0 items-center justify-center rounded-full bg-brand-subtle text-lg font-semibold text-brand"
            >
              {avatarInitial}
            </span>
            <div className="min-w-0 space-y-1">
              <div className="flex flex-wrap items-center gap-2">
                <h2
                  className="text-base font-semibold text-text-primary"
                  id="profile-info-title"
                >
                  个人资料
                </h2>
                <Badge variant="brand">{getUserRoleLabel(user.role)}</Badge>
                <Badge variant="neutral">
                  {authProviderLabel(user.auth_provider)}
                </Badge>
              </div>
              <p className="truncate text-sm text-text-secondary">
                {resolvedDisplayName}
                {user.job_title ? ` · ${user.job_title}` : ""}
              </p>
            </div>
          </div>
        </div>

        {/* 企业组织身份字段投影 */}
        <dl className="mt-5 grid grid-cols-1 gap-4 rounded-xl border border-border-subtle bg-subtle/60 p-4 sm:grid-cols-2">
          <div className="space-y-0.5">
            <dt className="text-xs text-text-tertiary">工号</dt>
            <dd className="font-mono text-sm text-text-primary">
              {user.employee_no ?? "未配置"}
            </dd>
          </div>
          <div className="space-y-0.5">
            <dt className="text-xs text-text-tertiary">职位</dt>
            <dd className="text-sm text-text-primary">
              {user.job_title ?? "未配置"}
            </dd>
          </div>
          <div className="space-y-0.5">
            <dt className="text-xs text-text-tertiary">企业邮箱</dt>
            <dd className="break-all text-sm text-text-primary">
              {user.email ?? "未绑定企业邮箱"}
            </dd>
          </div>
          <div className="space-y-0.5">
            <dt className="text-xs text-text-tertiary">注册时间</dt>
            <dd className="text-sm text-text-primary">
              {formatDateTime(user.created_at)}
            </dd>
          </div>

          <div className="space-y-1.5 sm:col-span-2">
            <dt className="text-xs text-text-tertiary">所属部门</dt>
            <dd>
              {user.departments.length === 0 ? (
                <span className="text-sm text-text-secondary">未分配部门</span>
              ) : (
                <ul className="flex flex-wrap gap-2">
                  {user.departments.map((department) => (
                    <li
                      className="inline-flex items-center gap-1.5 rounded-lg border border-border-default bg-canvas px-2.5 py-1 text-xs font-medium text-text-primary"
                      key={department.id}
                    >
                      <Building2
                        aria-hidden="true"
                        className="h-3.5 w-3.5 text-text-tertiary"
                      />
                      <span>{department.name}</span>
                      {department.is_primary ? (
                        <Badge variant="brand">主部门</Badge>
                      ) : null}
                    </li>
                  ))}
                </ul>
              )}
            </dd>
          </div>

          {user.managed_department_ids.length > 0 ? (
            <div className="space-y-1.5 sm:col-span-2">
              <dt className="text-xs text-text-tertiary">管辖部门</dt>
              <dd>
                <ul className="flex flex-wrap gap-2">
                  {user.managed_department_ids.map((departmentId) => (
                    <li key={departmentId}>
                      <Badge variant="info">
                        {departmentNameById.get(departmentId) ?? departmentId}
                      </Badge>
                    </li>
                  ))}
                </ul>
              </dd>
            </div>
          ) : null}
        </dl>

        {/* 显示名修改表单 */}
        <form
          className="mt-5 space-y-4 border-t border-border-subtle pt-5"
          noValidate
          onSubmit={(event) => void saveDisplayName(event)}
        >
          <div className="max-w-md space-y-1.5">
            <label
              className="block text-xs font-medium text-text-secondary"
              htmlFor="profile-display-name"
            >
              显示名
            </label>
            <Input
              aria-describedby={displayError ? displayNameErrorId : undefined}
              autoComplete="nickname"
              disabled={displayMutationPending}
              error={Boolean(displayError)}
              id="profile-display-name"
              onChange={(event) => {
                setDisplayName(event.target.value);
                if (displayError) {
                  setDisplayError(null);
                }
              }}
              value={displayName}
            />
            <p className="text-xs text-text-tertiary">
              清空后将自动回退使用企业邮箱前缀或工号；最多 80 个字符。
            </p>
          </div>

          {displayError ? (
            <p
              className="text-xs leading-snug text-danger"
              id={displayNameErrorId}
              role="alert"
            >
              {displayError}
            </p>
          ) : null}

          {displayNotice ? (
            <p
              className={
                displayRefreshFailed
                  ? "rounded-lg border border-warning-border bg-warning-bg px-3 py-2 text-xs text-warning"
                  : "rounded-lg border border-success-border bg-success-bg px-3 py-2 text-xs text-success"
              }
              role={displayRefreshFailed ? "alert" : "status"}
            >
              {displayNotice}
            </p>
          ) : null}

          <div className="flex flex-wrap items-center gap-2">
            <Button
              disabled={displayMutationPending}
              loading={updateProfile.isPending}
              size="sm"
              type="submit"
              variant="primary"
            >
              {updateProfile.isPending ? "正在保存…" : "保存显示名"}
            </Button>
            {displayRefreshFailed ? (
              <Button
                disabled={displayMutationPending}
                loading={refreshProfile.isPending}
                onClick={() => void retryDisplayRefresh()}
                size="sm"
                type="button"
                variant="outline"
              >
                {refreshProfile.isPending ? "正在刷新…" : "重试刷新"}
              </Button>
            ) : null}
          </div>
        </form>
      </section>

      {/* 账号安全区：按 auth_provider 明确分支，仅本地账号允许修改密码 */}
      <section
        aria-labelledby="profile-security-title"
        className="rounded-xl border border-border-subtle bg-canvas p-5 shadow-xs sm:p-6"
      >
        {user.auth_provider === "local" ? (
          <>
            <div className="space-y-1">
              <div className="flex items-center gap-2">
                <KeyRound
                  aria-hidden="true"
                  className="h-4 w-4 text-brand"
                />
                <h2
                  className="text-base font-semibold text-text-primary"
                  id="profile-security-title"
                >
                  修改密码
                </h2>
              </div>
              <p className="text-sm text-text-secondary">
                新密码需为 12–128 个字符；成功后会撤销其他设备的登录会话。
              </p>
            </div>

            <form
              className="mt-5 max-w-md space-y-4"
              noValidate
              onSubmit={(event) => void savePassword(event)}
            >
              <div className="space-y-1.5">
                <label
                  className="block text-xs font-medium text-text-secondary"
                  htmlFor="profile-current-password"
                >
                  当前密码
                </label>
                <Input
                  aria-describedby={passwordError ? passwordErrorId : undefined}
                  autoComplete="current-password"
                  disabled={updatePassword.isPending}
                  error={Boolean(passwordError)}
                  id="profile-current-password"
                  onChange={(event) => {
                    setCurrentPassword(event.target.value);
                    if (passwordError) setPasswordError(null);
                  }}
                  passwordToggle
                  value={currentPassword}
                />
              </div>

              <div className="space-y-1.5">
                <label
                  className="block text-xs font-medium text-text-secondary"
                  htmlFor="profile-new-password"
                >
                  新密码
                </label>
                <Input
                  aria-describedby={passwordError ? passwordErrorId : undefined}
                  autoComplete="new-password"
                  disabled={updatePassword.isPending}
                  error={Boolean(passwordError)}
                  id="profile-new-password"
                  onChange={(event) => {
                    setNewPassword(event.target.value);
                    if (passwordError) setPasswordError(null);
                  }}
                  passwordToggle
                  value={newPassword}
                />
              </div>

              <div className="space-y-1.5">
                <label
                  className="block text-xs font-medium text-text-secondary"
                  htmlFor="profile-confirm-password"
                >
                  确认新密码
                </label>
                <Input
                  aria-describedby={passwordError ? passwordErrorId : undefined}
                  autoComplete="new-password"
                  disabled={updatePassword.isPending}
                  error={Boolean(passwordError)}
                  id="profile-confirm-password"
                  onChange={(event) => {
                    setConfirmPassword(event.target.value);
                    if (passwordError) setPasswordError(null);
                  }}
                  passwordToggle
                  value={confirmPassword}
                />
              </div>

              {passwordError ? (
                <p
                  className="text-xs leading-snug text-danger"
                  id={passwordErrorId}
                  role="alert"
                >
                  {passwordError}
                </p>
              ) : null}

              {passwordNotice ? (
                <p
                  className="rounded-lg border border-success-border bg-success-bg px-3 py-2 text-xs text-success"
                  role="status"
                >
                  {passwordNotice}
                </p>
              ) : null}

              <div>
                <Button
                  disabled={updatePassword.isPending}
                  loading={updatePassword.isPending}
                  size="sm"
                  type="submit"
                  variant="primary"
                >
                  {updatePassword.isPending ? "正在更新…" : "更新密码"}
                </Button>
              </div>
            </form>
          </>
        ) : user.auth_provider === "feishu" ? (
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <ShieldCheck
                aria-hidden="true"
                className="h-4 w-4 text-brand"
              />
              <h2
                className="text-base font-semibold text-text-primary"
                id="profile-security-title"
              >
                账号安全
              </h2>
            </div>
            <InlineAlert
              description="当前账号通过飞书单点登录（SSO）托管，密码与在职状态由企业统一管理，无需也不支持在此修改本地密码。"
              title="飞书企业身份托管"
              variant="info"
            />
          </div>
        ) : (
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <ShieldAlert
                aria-hidden="true"
                className="h-4 w-4 text-warning"
              />
              <h2
                className="text-base font-semibold text-text-primary"
                id="profile-security-title"
              >
                账号安全
              </h2>
            </div>
            <InlineAlert
              description="当前账号为非生产环境预置的本地 Mock 体验账号，仅用于开发联调与角色权限验证，不支持修改密码。"
              title="非生产 Mock 体验账号"
              variant="warning"
            />
          </div>
        )}
      </section>
    </SettingsLayout>
  );
}
