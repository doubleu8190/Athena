import { useEffect, useRef, useState, type FormEvent } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { Icon } from "../../shared/ui/Icons";
import { AuthLayout } from "./AuthLayout";
import { authErrorMessage } from "./errorMessages";
import { getSafeReturnTo } from "./returnTo";
import { useRegisterMutation } from "./useAuthMutations";
import {
  validateDisplayName,
  validateEmail,
  validatePassword,
  type AuthFieldErrors,
} from "./validation";

export function RegisterPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const registerMutation = useRegisterMutation();
  const [errors, setErrors] = useState<AuthFieldErrors>({});
  const [showPassword, setShowPassword] = useState(false);
  const emailRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  const displayNameRef = useRef<HTMLInputElement>(null);
  const formErrorRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (errors.email) {
      emailRef.current?.focus();
    } else if (errors.password) {
      passwordRef.current?.focus();
    } else if (errors.displayName) {
      displayNameRef.current?.focus();
    } else if (errors.form) {
      formErrorRef.current?.focus();
    }
  }, [errors]);

  const clearError = (field: keyof AuthFieldErrors) => {
    if (errors[field] || errors.form) {
      setErrors((current) => ({
        ...current,
        [field]: undefined,
        form: undefined,
      }));
    }
  };

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const formData = new FormData(event.currentTarget);
    const email = String(formData.get("email") ?? "").trim();
    const password = String(formData.get("password") ?? "");
    const displayName = String(formData.get("display_name") ?? "").trim();
    const nextErrors: AuthFieldErrors = {
      displayName: validateDisplayName(displayName),
      email: validateEmail(email),
      password: validatePassword(password),
    };

    if (nextErrors.email || nextErrors.password || nextErrors.displayName) {
      setErrors(nextErrors);
      return;
    }

    setErrors({});
    try {
      await registerMutation.mutateAsync({
        display_name: displayName || null,
        email,
        password,
      });
      navigate(getSafeReturnTo(location.state), { replace: true });
    } catch (error) {
      setErrors({ form: authErrorMessage(error) });
    }
  };

  return (
    <AuthLayout
      switchLabel="直接登录"
      switchPrompt="已经有账号？"
      switchState={location.state}
      switchTo="/login"
    >
      <header className="auth-form-heading">
        <span className="auth-form-kicker">New workspace identity</span>
        <h1 id="auth-form-title">创建你的工作身份</h1>
        <p>使用工作邮箱注册。注册成功后会直接进入当前设备的工作区。</p>
      </header>

      <form
        aria-busy={registerMutation.isPending}
        className="auth-form"
        noValidate
        onSubmit={handleSubmit}
      >
        <div className="field">
          <label htmlFor="register-email">工作邮箱</label>
          <div className={`field-input${errors.email ? " is-invalid" : ""}`}>
            <Icon name="mail" size={16} />
            <input
              aria-describedby={
                errors.email ? "register-email-error" : undefined
              }
              aria-invalid={Boolean(errors.email)}
              autoComplete="email"
              autoFocus
              id="register-email"
              maxLength={254}
              name="email"
              onChange={() => clearError("email")}
              placeholder="you@example.com"
              ref={emailRef}
              required
              type="email"
            />
          </div>
          {errors.email && (
            <p
              aria-live="polite"
              className="field-error"
              id="register-email-error"
            >
              {errors.email}
            </p>
          )}
        </div>

        <div className="field">
          <label htmlFor="register-password">密码</label>
          <div className={`field-input${errors.password ? " is-invalid" : ""}`}>
            <Icon name="lock" size={16} />
            <input
              aria-describedby={
                errors.password
                  ? "register-password-error"
                  : "register-password-hint"
              }
              aria-invalid={Boolean(errors.password)}
              autoComplete="new-password"
              id="register-password"
              name="password"
              onChange={() => clearError("password")}
              placeholder="设置 12–128 个字符"
              ref={passwordRef}
              required
              type={showPassword ? "text" : "password"}
            />
            <button
              aria-label={showPassword ? "隐藏密码" : "显示密码"}
              className="field-visibility"
              onClick={() => setShowPassword((visible) => !visible)}
              type="button"
            >
              <Icon name={showPassword ? "eye-off" : "eye"} size={16} />
            </button>
          </div>
          {errors.password ? (
            <p
              aria-live="polite"
              className="field-error"
              id="register-password-error"
            >
              {errors.password}
            </p>
          ) : (
            <p className="field-hint" id="register-password-hint">
              密码长度为 12–128 个字符。
            </p>
          )}
        </div>

        <div className="field">
          <label htmlFor="register-display-name">
            显示名 <span className="field-optional">可选</span>
          </label>
          <div
            className={`field-input${errors.displayName ? " is-invalid" : ""}`}
          >
            <Icon name="user" size={16} />
            <input
              aria-describedby={
                errors.displayName
                  ? "register-display-name-error"
                  : "register-display-name-hint"
              }
              aria-invalid={Boolean(errors.displayName)}
              autoComplete="nickname"
              id="register-display-name"
              name="display_name"
              onChange={() => clearError("displayName")}
              placeholder="例如：林知远"
              ref={displayNameRef}
              type="text"
            />
          </div>
          {errors.displayName ? (
            <p
              aria-live="polite"
              className="field-error"
              id="register-display-name-error"
            >
              {errors.displayName}
            </p>
          ) : (
            <p className="field-hint" id="register-display-name-hint">
              可留空；留空时工作区会显示你的邮箱。
            </p>
          )}
        </div>

        {errors.form && (
          <div
            aria-live="polite"
            className="form-alert"
            id="register-form-error"
            ref={formErrorRef}
            role="alert"
            tabIndex={-1}
          >
            <Icon name="shield" size={16} />
            <span>{errors.form}</span>
          </div>
        )}

        <button
          className="auth-submit"
          disabled={registerMutation.isPending}
          type="submit"
        >
          <span>{registerMutation.isPending ? "正在创建…" : "创建账号"}</span>
          <Icon name="arrow-right" size={15} />
        </button>
      </form>
    </AuthLayout>
  );
}
