import { useState, type FormEvent } from "react";

import { authErrorMessage } from "./errorMessages";
import { useAuth } from "./AuthProvider";
import { getUserDisplayName, getUserRoleLabel } from "./model";
import {
  useChangePasswordMutation,
  useRefreshAuthenticatedUserMutation,
  useUpdateCurrentUserMutation,
} from "./useAuthMutations";
import { validateDisplayName, validatePassword } from "./validation";

export function ProfilePage() {
  const { user } = useAuth();
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

  if (!user) return null;

  const displayMutationPending =
    updateProfile.isPending || refreshProfile.isPending;

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
        setDisplayNotice(
          "显示名已保存，但登录资料刷新失败。请重试刷新。",
        );
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
        setDisplayNotice(
          "登录资料刷新仍然失败。显示名已保存，请稍后重试。",
        );
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
    <div className="page-shell profile-page">
      <header className="page-heading">
        <div className="page-heading-copy">
          <p className="page-eyebrow">Workspace / Account</p>
          <h1>个人资料</h1>
          <p className="page-description">管理个人显示信息和登录密码。</p>
        </div>
      </header>

      <section className="profile-card" aria-labelledby="profile-info-title">
        <div className="profile-card-heading">
          <div>
            <p className="profile-kicker">Account</p>
            <h2 id="profile-info-title">账号信息</h2>
          </div>
          <span className="profile-avatar" aria-hidden="true">
            {Array.from(
              getUserDisplayName({
                display_name: displayName,
                email: user.email,
                employee_no: user.employee_no,
              }),
            )[0]?.toUpperCase() ?? "A"}
          </span>
        </div>
        <form
          className="profile-form"
          onSubmit={(event) => void saveDisplayName(event)}
        >
          <label className="profile-field" htmlFor="profile-display-name">
            <span>显示名</span>
            <input
              autoComplete="nickname"
              disabled={displayMutationPending}
              id="profile-display-name"
              onChange={(event) => setDisplayName(event.target.value)}
              value={displayName}
            />
            <small>清空后将自动回退使用企业邮箱前缀或工号。</small>
          </label>
          <dl className="profile-readonly-grid">
            <div><dt>邮箱</dt><dd>{user.email ?? "未绑定企业邮箱"}</dd></div>
            <div><dt>角色</dt><dd>{getUserRoleLabel(user.role)}</dd></div>
            <div><dt>注册时间</dt><dd>{new Date(user.created_at).toLocaleString("zh-CN")}</dd></div>
          </dl>
          {displayError ? (
            <p className="profile-error" role="alert">
              {displayError}
            </p>
          ) : null}
          {displayNotice ? (
            <p
              className="profile-notice"
              role={displayRefreshFailed ? "alert" : "status"}
            >
              {displayNotice}
            </p>
          ) : null}
          <div className="profile-actions">
            <button
              className="button-primary"
              disabled={displayMutationPending}
              type="submit"
            >
              {updateProfile.isPending ? "正在保存…" : "保存显示名"}
            </button>
            {displayRefreshFailed ? (
              <button
                className="button-secondary"
                disabled={displayMutationPending}
                onClick={() => void retryDisplayRefresh()}
                type="button"
              >
                {refreshProfile.isPending ? "正在刷新…" : "重试刷新"}
              </button>
            ) : null}
          </div>
        </form>
      </section>

      <section className="profile-card" aria-labelledby="profile-password-title">
        <div className="profile-card-heading">
          <div>
            <p className="profile-kicker">Security</p>
            <h2 id="profile-password-title">修改密码</h2>
            <p>新密码需为 12–128 个字符；成功后会撤销其他设备的登录会话。</p>
          </div>
        </div>
        <form
          className="profile-form profile-password-form"
          onSubmit={(event) => void savePassword(event)}
        >
          <label className="profile-field" htmlFor="profile-current-password">
            <span>当前密码</span>
            <input
              autoComplete="current-password"
              disabled={updatePassword.isPending}
              id="profile-current-password"
              onChange={(event) => setCurrentPassword(event.target.value)}
              type="password"
              value={currentPassword}
            />
          </label>
          <label className="profile-field" htmlFor="profile-new-password">
            <span>新密码</span>
            <input
              autoComplete="new-password"
              disabled={updatePassword.isPending}
              id="profile-new-password"
              onChange={(event) => setNewPassword(event.target.value)}
              type="password"
              value={newPassword}
            />
          </label>
          <label className="profile-field" htmlFor="profile-confirm-password">
            <span>确认新密码</span>
            <input
              autoComplete="new-password"
              disabled={updatePassword.isPending}
              id="profile-confirm-password"
              onChange={(event) => setConfirmPassword(event.target.value)}
              type="password"
              value={confirmPassword}
            />
          </label>
          {passwordError ? (
            <p className="profile-error" role="alert">
              {passwordError}
            </p>
          ) : null}
          {passwordNotice ? (
            <p className="profile-notice" role="status">
              {passwordNotice}
            </p>
          ) : null}
          <div className="profile-actions">
            <button
              className="button-primary"
              disabled={updatePassword.isPending}
              type="submit"
            >
              {updatePassword.isPending ? "正在更新…" : "更新密码"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
