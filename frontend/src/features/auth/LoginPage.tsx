import { useEffect, useRef, useState, type FormEvent } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { Icon } from "../../shared/ui/Icons";
import { AuthLayout } from "./AuthLayout";
import { authErrorMessage } from "./errorMessages";
import { getSafeReturnTo } from "./returnTo";
import { useLoginMutation } from "./useAuthMutations";
import {
  validateEmail,
  validatePassword,
  type AuthFieldErrors,
} from "./validation";

export function LoginPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const loginMutation = useLoginMutation();
  const [errors, setErrors] = useState<AuthFieldErrors>({});
  const [showPassword, setShowPassword] = useState(false);
  const emailRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  const formErrorRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (errors.email) {
      emailRef.current?.focus();
    } else if (errors.password) {
      passwordRef.current?.focus();
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
    const rememberMe = formData.get("remember_me") === "on";
    const nextErrors: AuthFieldErrors = {
      email: validateEmail(email),
      password: validatePassword(password),
    };

    if (nextErrors.email || nextErrors.password) {
      setErrors(nextErrors);
      return;
    }

    setErrors({});
    try {
      await loginMutation.mutateAsync({
        email,
        password,
        remember_me: rememberMe,
      });
      navigate(getSafeReturnTo(location.state), { replace: true });
    } catch (error) {
      setErrors({ form: authErrorMessage(error) });
    }
  };

  return (
    <AuthLayout
      switchLabel="创建账号"
      switchPrompt="还没有账号？"
      switchState={location.state}
      switchTo="/register"
    >
      <header className="auth-form-heading">
        <span className="auth-form-kicker">Account access</span>
        <h1 id="auth-form-title">欢迎回来</h1>
        <p>登录你的工作空间，继续上一次思考。</p>
      </header>

      <form
        aria-busy={loginMutation.isPending}
        className="auth-form"
        noValidate
        onSubmit={handleSubmit}
      >
        <div className="field">
          <label htmlFor="login-email">工作邮箱</label>
          <div className={`field-input${errors.email ? " is-invalid" : ""}`}>
            <Icon name="mail" size={16} />
            <input
              aria-describedby={errors.email ? "login-email-error" : undefined}
              aria-invalid={Boolean(errors.email)}
              autoComplete="email"
              autoFocus
              id="login-email"
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
              id="login-email-error"
            >
              {errors.email}
            </p>
          )}
        </div>

        <div className="field">
          <label htmlFor="login-password">密码</label>
          <div className={`field-input${errors.password ? " is-invalid" : ""}`}>
            <Icon name="lock" size={16} />
            <input
              aria-describedby={
                errors.password ? "login-password-error" : undefined
              }
              aria-invalid={Boolean(errors.password)}
              autoComplete="current-password"
              id="login-password"
              name="password"
              onChange={() => clearError("password")}
              placeholder="输入你的密码"
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
          {errors.password && (
            <p
              aria-live="polite"
              className="field-error"
              id="login-password-error"
            >
              {errors.password}
            </p>
          )}
        </div>

        <label className="checkbox-field" htmlFor="login-remember">
          <input id="login-remember" name="remember_me" type="checkbox" />
          <span>在此设备保持登录 30 天</span>
        </label>

        {errors.form && (
          <div
            aria-live="polite"
            className="form-alert"
            id="login-form-error"
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
          disabled={loginMutation.isPending}
          type="submit"
        >
          <span>{loginMutation.isPending ? "正在登录…" : "进入 Athena"}</span>
          <Icon name="arrow-right" size={15} />
        </button>
      </form>
    </AuthLayout>
  );
}
