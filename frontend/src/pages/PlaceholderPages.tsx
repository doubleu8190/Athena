import { Icon } from "../shared/ui/Icons";

export function AccessDeniedPage() {
  return (
    <div className="page-shell">
      <header className="page-heading">
        <div className="page-heading-copy">
          <p className="page-eyebrow">Admin / Restricted</p>
          <h1>无法访问管理控制台</h1>
          <p className="page-description">
            管理入口仅对具有管理员角色的账号开放。
          </p>
        </div>
      </header>

      <section className="empty-panel" aria-labelledby="access-denied-title">
        <span className="empty-panel-icon" aria-hidden="true">
          <Icon name="shield" size={26} />
        </span>
        <p className="empty-panel-kicker">Permission required</p>
        <h2 id="access-denied-title">当前账号没有管理员权限</h2>
        <p>你仍可使用认证后的工作空间，但无法查看或修改管理配置。</p>
      </section>
    </div>
  );
}
