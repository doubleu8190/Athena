import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { Brand } from "../../shared/ui/Brand";
import { Icon } from "../../shared/ui/Icons";

interface AuthLayoutProps {
  children: ReactNode;
  switchLabel: string;
  switchPrompt: string;
  switchState?: unknown;
  switchTo: string;
}

export function AuthLayout({
  children,
  switchLabel,
  switchPrompt,
  switchState,
  switchTo,
}: AuthLayoutProps) {
  return (
    <main className="auth-page">
      <section className="auth-story" aria-label="Athena 工作空间介绍">
        <header className="auth-story-header">
          <Brand light />
        </header>

        <div className="auth-story-copy">
          <span className="auth-eyebrow">A shared mind for serious work</span>
          <h2 className="auth-story-title">
            把上下文，
            <br />
            留在<em>工作里</em>。
          </h2>
          <p className="auth-story-description">
            Athena 将项目、资料与执行轨迹放在同一个可信空间。先从一个安全、
            可撤销的工作身份开始。
          </p>
        </div>

        <div className="auth-story-note">
          <span className="auth-story-note-icon">
            <Icon name="shield" size={18} />
          </span>
          <div>
            <strong>服务端会话</strong>
            <p>登录凭据保存在 HttpOnly Cookie 中，前端不读取原始 token。</p>
          </div>
        </div>

        <footer className="auth-story-footer">
          <span>Athena / Intelligence Workspace</span>
          <span>Private by design</span>
        </footer>
      </section>

      <section className="auth-form-pane" aria-label="账号表单">
        <div className="auth-form-wrap">
          <div className="auth-switch">
            <span>{switchPrompt}</span>
            <Link state={switchState} to={switchTo}>
              {switchLabel}
            </Link>
          </div>

          {children}

          <div className="auth-divider">
            <span>安全登录由 Athena 账号系统提供</span>
          </div>
          <div className="auth-security-note">
            <Icon name="shield" size={16} />
            <p>
              当前使用服务端可撤销会话。登出会撤销当前 Cookie，受保护页面随后立即失效。
            </p>
          </div>
        </div>
      </section>
    </main>
  );
}
