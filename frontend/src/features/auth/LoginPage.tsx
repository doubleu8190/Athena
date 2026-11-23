import { useQuery } from "@tanstack/react-query";
import { ChevronDown, Loader2, Lock, Mail, ShieldCheck } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";

import { ApiError } from "../../shared/api/client";
import {
  Badge,
  Brand,
  Button,
  Checkbox,
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
  EmptyState,
  InlineAlert,
  Input,
  Skeleton,
  cn,
  type BadgeProps,
} from "../../shared/ui";
import {
  buildFeishuAuthorizeUrl,
  getAuthProviders,
  listMockUsers,
} from "./api";
import { useAuth } from "./AuthProvider";
import { authErrorCodeMessage, authErrorMessage } from "./errorMessages";
import {
  authKeys,
  getPrimaryDepartment,
  getUserDisplayName,
  getUserRoleLabel,
  type MockUser,
  type UserRole,
} from "./model";
import { getSafeReturnTo } from "./returnTo";
import { useLoginMutation, useMockLoginMutation } from "./useAuthMutations";
import {
  validateEmail,
  validatePassword,
  type AuthFieldErrors,
} from "./validation";

/**
 * 浏览器整页外跳代理（默认调用 window.location.assign），
 * 方便单测在不触发 jsdom 导航异常的前提下断言飞书 SSO 授权地址。
 */
export const authBrowserNavigation = {
  assign(url: string): void {
    window.location.assign(url);
  },
};

export interface LoginPageProps {
  /** 可选注入的外部整页跳转函数（优先于 authBrowserNavigation.assign）。 */
  onNavigateExternal?: (url: string) => void;
}

function getRoleBadgeVariant(role: UserRole): BadgeProps["variant"] {
  if (role === "super_admin") {
    return "brand";
  }
  if (role === "dept_admin") {
    return "info";
  }
  return "neutral";
}

export function LoginPage({ onNavigateExternal }: LoginPageProps = {}) {
  const location = useLocation();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { sessionExpired } = useAuth();
  const loginMutation = useLoginMutation();
  const mockLoginMutation = useMockLoginMutation();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [rememberMe, setRememberMe] = useState(false);
  const [errors, setErrors] = useState<AuthFieldErrors>({});
  const [feishuError, setFeishuError] = useState<string | null>(null);
  const [mockError, setMockError] = useState<string | null>(null);
  const [pendingMockUserId, setPendingMockUserId] = useState<string | null>(
    null,
  );
  const [feishuRedirecting, setFeishuRedirecting] = useState(false);
  const [localExpanded, setLocalExpanded] = useState(false);
  const [focusFormErrorTick, setFocusFormErrorTick] = useState(0);
  const [focusFeishuErrorTick, setFocusFeishuErrorTick] = useState(0);
  const [focusMockErrorTick, setFocusMockErrorTick] = useState(0);

  const emailRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  const formErrorRef = useRef<HTMLDivElement>(null);
  const feishuErrorRef = useRef<HTMLDivElement>(null);
  const mockErrorRef = useRef<HTMLDivElement>(null);
  // 同步锁用于防高频连点 Mock 卡片在 React 重渲染前触发并发登录竞态。
  const loginActionLockRef = useRef(false);

  const returnTo = getSafeReturnTo(location.state, location.search);
  const isExpiredNotice =
    searchParams.get("reason") === "expired" || sessionExpired;
  const oauthErrorMessage = authErrorCodeMessage(searchParams.get("error"));

  const providersQuery = useQuery({
    queryKey: authKeys.providers,
    queryFn: getAuthProviders,
    retry: false,
    refetchOnWindowFocus: false,
  });

  const feishuEnabled = providersQuery.data?.feishu_enabled === true;
  const mockEnabled = providersQuery.data?.mock_enabled === true;
  const localLoginEnabled = providersQuery.data?.local_login_enabled === true;
  const allProvidersDisabled =
    providersQuery.isSuccess &&
    !feishuEnabled &&
    !mockEnabled &&
    !localLoginEnabled;
  const shouldCollapseLocal = feishuEnabled || mockEnabled;

  const mockUsersQuery = useQuery({
    queryKey: authKeys.mockUsers,
    queryFn: listMockUsers,
    enabled: mockEnabled,
    retry: false,
    refetchOnWindowFocus: false,
  });

  const isAnyLoginPending =
    feishuRedirecting ||
    loginMutation.isPending ||
    mockLoginMutation.isPending ||
    pendingMockUserId !== null;

  useEffect(() => {
    if (focusFormErrorTick > 0 && errors.form) {
      formErrorRef.current?.focus();
    }
  }, [errors.form, focusFormErrorTick]);

  useEffect(() => {
    if (focusFeishuErrorTick > 0 && feishuError) {
      feishuErrorRef.current?.focus();
    }
  }, [feishuError, focusFeishuErrorTick]);

  useEffect(() => {
    if (focusMockErrorTick > 0 && mockError) {
      mockErrorRef.current?.focus();
    }
  }, [mockError, focusMockErrorTick]);

  useEffect(() => {
    // 用户点击飞书授权后通过浏览器后退恢复 bfcache 页面快照时，
    // 必须释放外跳锁与 loading 状态，防止登录页按钮永久卡死。
    const handlePageShow = (event: PageTransitionEvent) => {
      if (event.persisted) {
        loginActionLockRef.current = false;
        setFeishuRedirecting(false);
      }
    };
    window.addEventListener("pageshow", handlePageShow);
    return () => {
      window.removeEventListener("pageshow", handlePageShow);
    };
  }, []);

  const handleFeishuLogin = () => {
    if (loginActionLockRef.current || isAnyLoginPending) {
      return;
    }
    loginActionLockRef.current = true;
    setFeishuRedirecting(true);
    setFeishuError(null);
    setMockError(null);
    const authorizeUrl = buildFeishuAuthorizeUrl(returnTo);
    const navigateExternal = onNavigateExternal ?? authBrowserNavigation.assign;
    try {
      navigateExternal(authorizeUrl);
    } catch {
      // 浏览器策略拦截或导航同步抛错时立即释放锁和加载态并提示重试。
      loginActionLockRef.current = false;
      setFeishuRedirecting(false);
      setFeishuError("无法跳转至飞书登录页，请检查浏览器设置后重试。");
      setFocusFeishuErrorTick((tick) => tick + 1);
    }
  };

  const handleMockLogin = async (mockUser: MockUser) => {
    if (loginActionLockRef.current || isAnyLoginPending) {
      return;
    }
    loginActionLockRef.current = true;
    setPendingMockUserId(mockUser.id);
    setFeishuError(null);
    setMockError(null);
    setErrors({});
    try {
      await mockLoginMutation.mutateAsync(mockUser.id);
      navigate(returnTo, { replace: true });
    } catch (error) {
      // 后端契约 1.4 节：Mock 体验账号被停用时返回 401 invalid_credentials，给出语义准确的提示。
      const message =
        error instanceof ApiError && error.detail === "invalid_credentials"
          ? "该体验账号已被停用，无法登录。"
          : authErrorMessage(error);
      setMockError(message);
      setFocusMockErrorTick((tick) => tick + 1);
    } finally {
      loginActionLockRef.current = false;
      setPendingMockUserId(null);
    }
  };

  const handleEmailChange = (nextEmail: string) => {
    setEmail(nextEmail);
    if (errors.email || errors.form) {
      setErrors((current) => ({
        ...current,
        email: undefined,
        form: undefined,
      }));
    }
  };

  const handlePasswordChange = (nextPassword: string) => {
    setPassword(nextPassword);
    if (errors.password || errors.form) {
      setErrors((current) => ({
        ...current,
        password: undefined,
        form: undefined,
      }));
    }
  };

  const handleEmailBlur = () => {
    const emailError = validateEmail(email.trim());
    if (emailError) {
      setErrors((current) => ({ ...current, email: emailError }));
    }
  };

  const handlePasswordBlur = () => {
    const passwordError = validatePassword(password);
    if (passwordError) {
      setErrors((current) => ({ ...current, password: passwordError }));
    }
  };

  const handleLocalSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (loginActionLockRef.current || isAnyLoginPending) {
      return;
    }

    const normalizedEmail = email.trim();
    const nextErrors: AuthFieldErrors = {
      email: validateEmail(normalizedEmail),
      password: validatePassword(password),
    };

    if (nextErrors.email || nextErrors.password) {
      setErrors(nextErrors);
      // 仅在显式提交校验失败时将焦点移至首个错误输入框，避免失焦校验抢走焦点。
      if (nextErrors.email) {
        emailRef.current?.focus();
      } else if (nextErrors.password) {
        passwordRef.current?.focus();
      }
      return;
    }

    loginActionLockRef.current = true;
    setErrors({});
    setFeishuError(null);
    setMockError(null);
    try {
      await loginMutation.mutateAsync({
        email: normalizedEmail,
        password,
        remember_me: rememberMe,
      });
      navigate(returnTo, { replace: true });
    } catch (error) {
      setErrors({ form: authErrorMessage(error) });
      setFocusFormErrorTick((tick) => tick + 1);
    } finally {
      loginActionLockRef.current = false;
    }
  };

  const renderLocalLoginForm = () => (
    <form
      aria-busy={loginMutation.isPending}
      className="space-y-4"
      noValidate
      onSubmit={(event) => void handleLocalSubmit(event)}
    >
      <div className="space-y-1.5">
        <label
          className="block text-sm font-medium text-text-primary"
          htmlFor="login-email"
        >
          工作邮箱
        </label>
        <Input
          aria-describedby={errors.email ? "login-email-error" : undefined}
          autoComplete="email"
          disabled={isAnyLoginPending}
          error={Boolean(errors.email)}
          id="login-email"
          maxLength={254}
          name="email"
          onBlur={handleEmailBlur}
          onChange={(event) => handleEmailChange(event.target.value)}
          placeholder="admin@company.com"
          prefixSlot={<Mail aria-hidden="true" className="h-4 w-4" />}
          ref={emailRef}
          required
          type="email"
          value={email}
        />
        {errors.email ? (
          <p
            aria-live="polite"
            className="text-xs text-danger"
            id="login-email-error"
          >
            {errors.email}
          </p>
        ) : null}
      </div>

      <div className="space-y-1.5">
        <label
          className="block text-sm font-medium text-text-primary"
          htmlFor="login-password"
        >
          密码
        </label>
        <Input
          aria-describedby={
            errors.password ? "login-password-error" : undefined
          }
          autoComplete="current-password"
          disabled={isAnyLoginPending}
          error={Boolean(errors.password)}
          id="login-password"
          name="password"
          onBlur={handlePasswordBlur}
          onChange={(event) => handlePasswordChange(event.target.value)}
          passwordToggle
          placeholder="请输入密码"
          prefixSlot={<Lock aria-hidden="true" className="h-4 w-4" />}
          ref={passwordRef}
          required
          value={password}
        />
        {errors.password ? (
          <p
            aria-live="polite"
            className="text-xs text-danger"
            id="login-password-error"
          >
            {errors.password}
          </p>
        ) : null}
      </div>

      <div className="flex items-center gap-2">
        <Checkbox
          checked={rememberMe}
          disabled={isAnyLoginPending}
          id="login-remember"
          onCheckedChange={(checked) => setRememberMe(checked === true)}
        />
        <label
          className="cursor-pointer select-none text-sm text-text-secondary"
          htmlFor="login-remember"
        >
          在此设备保持登录 30 天
        </label>
      </div>

      {errors.form ? (
        <div
          className="outline-hidden"
          id="login-form-error"
          ref={formErrorRef}
          tabIndex={-1}
        >
          <InlineAlert description={errors.form} variant="danger" />
        </div>
      ) : null}

      <Button
        className="w-full"
        disabled={isAnyLoginPending}
        loading={loginMutation.isPending}
        type="submit"
        variant="primary"
      >
        使用邮箱登录
      </Button>
    </form>
  );

  return (
    <main className="flex min-h-dvh flex-col items-center justify-center bg-subtle px-4 py-10">
      <section
        aria-labelledby="login-card-title"
        className="w-full max-w-[440px] rounded-2xl border border-border-subtle bg-canvas p-7 shadow-sm"
      >
        <header className="flex flex-col items-center text-center">
          <Brand compact />
          <h1
            className="mt-3 text-lg font-semibold tracking-tight text-text-primary"
            id="login-card-title"
          >
            登录工作台
          </h1>
          <p className="mt-1 text-sm text-text-secondary">
            企业内部智能办公工作台
          </p>
        </header>

        {isExpiredNotice || oauthErrorMessage ? (
          <div className="mt-5 space-y-2.5">
            {isExpiredNotice ? (
              <InlineAlert
                description="登录状态已过期或账号权限已变更，请重新登录。"
                variant="warning"
              />
            ) : null}
            {oauthErrorMessage ? (
              <InlineAlert description={oauthErrorMessage} variant="danger" />
            ) : null}
          </div>
        ) : null}

        {providersQuery.isPending ? (
          <div
            aria-busy="true"
            aria-label="正在加载登录配置"
            className="mt-6 space-y-3"
            role="status"
          >
            <Skeleton className="h-10 w-full rounded-lg" />
            <Skeleton className="h-24 w-full rounded-lg" />
            <Skeleton className="h-9 w-full rounded-lg" />
          </div>
        ) : providersQuery.isError ? (
          <div className="mt-6">
            <InlineAlert
              action={
                <Button
                  onClick={() => void providersQuery.refetch()}
                  size="xs"
                  variant="outline"
                >
                  重新加载
                </Button>
              }
              description={authErrorMessage(providersQuery.error)}
              title="无法加载登录配置"
              variant="danger"
            />
          </div>
        ) : allProvidersDisabled ? (
          <div className="mt-6">
            <EmptyState
              compact
              description="当前环境暂未启用任何登录方式，请联系企业 IT 管理员检查登录配置。"
              icon="shield"
              title="暂不可登录"
            />
          </div>
        ) : (
          <div className="mt-6 space-y-5">
            {feishuEnabled ? (
              <div className="space-y-2.5">
                {feishuError ? (
                  <div
                    className="outline-hidden"
                    ref={feishuErrorRef}
                    tabIndex={-1}
                  >
                    <InlineAlert description={feishuError} variant="danger" />
                  </div>
                ) : null}
                <Button
                  className="h-10 w-full"
                  disabled={isAnyLoginPending}
                  loading={feishuRedirecting}
                  onClick={handleFeishuLogin}
                  type="button"
                  variant="primary"
                >
                  <ShieldCheck aria-hidden="true" className="h-4 w-4" />
                  <span>使用飞书账号登录</span>
                </Button>
              </div>
            ) : null}

            {mockEnabled ? (
              <section
                aria-labelledby="mock-login-section-title"
                className="space-y-3"
              >
                <div className="flex items-center gap-3">
                  <span className="h-px flex-1 bg-border-subtle" />
                  <h2
                    className="text-xs font-medium text-text-tertiary"
                    id="mock-login-section-title"
                  >
                    非生产联调 · 快捷切换角色
                  </h2>
                  <span className="h-px flex-1 bg-border-subtle" />
                </div>

                {mockError ? (
                  <div
                    className="outline-hidden"
                    ref={mockErrorRef}
                    tabIndex={-1}
                  >
                    <InlineAlert description={mockError} variant="danger" />
                  </div>
                ) : null}

                {mockUsersQuery.isPending ? (
                  <div
                    aria-busy="true"
                    aria-label="正在加载体验账号"
                    className="grid grid-cols-1 gap-2 sm:grid-cols-2"
                    role="status"
                  >
                    <Skeleton className="h-[74px] w-full rounded-xl" />
                    <Skeleton className="h-[74px] w-full rounded-xl" />
                    <Skeleton className="h-[74px] w-full rounded-xl" />
                    <Skeleton className="h-[74px] w-full rounded-xl" />
                  </div>
                ) : mockUsersQuery.isError ? (
                  <InlineAlert
                    action={
                      <Button
                        onClick={() => void mockUsersQuery.refetch()}
                        size="xs"
                        variant="outline"
                      >
                        重试
                      </Button>
                    }
                    description={authErrorMessage(mockUsersQuery.error)}
                    title="体验账号列表加载失败"
                    variant="danger"
                  />
                ) : mockUsersQuery.data.items.length === 0 ? (
                  <p className="rounded-xl border border-border-subtle bg-subtle px-3 py-4 text-center text-xs text-text-secondary">
                    当前环境暂无可用的快捷体验账号。
                  </p>
                ) : (
                  <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                    {mockUsersQuery.data.items.map((mockUser) => {
                      const displayName = getUserDisplayName(mockUser);
                      const roleLabel = getUserRoleLabel(mockUser.role);
                      const department = getPrimaryDepartment(mockUser);
                      const isCardLoading = pendingMockUserId === mockUser.id;
                      return (
                        <button
                          aria-busy={isCardLoading || undefined}
                          className={cn(
                            "group relative flex w-full flex-col gap-1.5 rounded-xl border border-border-default bg-canvas p-3 text-left transition-colors",
                            "hover:border-brand-border hover:bg-subtle",
                            "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus",
                            "disabled:cursor-not-allowed disabled:opacity-60",
                            isCardLoading &&
                              "border-brand bg-brand-subtle/40 opacity-100",
                          )}
                          disabled={isAnyLoginPending}
                          key={mockUser.id}
                          onClick={() => void handleMockLogin(mockUser)}
                          type="button"
                        >
                          <div className="flex items-center justify-between gap-2">
                            <span className="truncate text-sm font-medium text-text-primary">
                              {displayName}
                            </span>
                            {isCardLoading ? (
                              <Loader2
                                aria-hidden="true"
                                className="h-3.5 w-3.5 shrink-0 animate-spin text-brand motion-reduce:animate-none"
                              />
                            ) : (
                              <Badge
                                className="shrink-0"
                                variant={getRoleBadgeVariant(mockUser.role)}
                              >
                                {roleLabel}
                              </Badge>
                            )}
                          </div>
                          <div className="flex items-center justify-between gap-2 text-xs text-text-secondary">
                            <span className="truncate">
                              {department?.name ?? "未分配部门"}
                            </span>
                            {mockUser.employee_no ? (
                              <span className="shrink-0 font-mono text-[11px] text-text-tertiary">
                                {mockUser.employee_no}
                              </span>
                            ) : null}
                          </div>
                        </button>
                      );
                    })}
                  </div>
                )}
              </section>
            ) : null}

            {localLoginEnabled ? (
              shouldCollapseLocal ? (
                <Collapsible
                  className="border-t border-border-subtle pt-4"
                  onOpenChange={setLocalExpanded}
                  open={localExpanded}
                >
                  <CollapsibleTrigger asChild>
                    <button
                      className={cn(
                        "flex w-full items-center justify-between rounded-lg px-2 py-1.5 text-sm font-medium text-text-secondary transition-colors",
                        "hover:bg-subtle hover:text-text-primary",
                        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring-focus",
                      )}
                      type="button"
                    >
                      <span>管理员 / 应急邮箱密码登录</span>
                      <ChevronDown
                        aria-hidden="true"
                        className={cn(
                          "h-4 w-4 shrink-0 text-text-tertiary transition-transform duration-150",
                          localExpanded && "rotate-180",
                        )}
                      />
                    </button>
                  </CollapsibleTrigger>
                  <CollapsibleContent className="pt-4">
                    {renderLocalLoginForm()}
                  </CollapsibleContent>
                </Collapsible>
              ) : (
                renderLocalLoginForm()
              )
            ) : null}
          </div>
        )}

        <footer className="mt-6 border-t border-border-subtle pt-4 text-center">
          <p className="text-xs text-text-tertiary">
            遇账号或权限问题，请联系企业 IT 管理员
          </p>
        </footer>
      </section>
    </main>
  );
}
