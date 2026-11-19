import { Link, useLocation } from "react-router-dom";

import { Icon } from "../../shared/ui/Icons";

export function PersonalSkillsSidebar() {
  const location = useLocation();
  const view = new URLSearchParams(location.search).get("view");
  const mineActive = view === "mine";

  return (
    <div className="sidebar-scroll">
      <section className="sidebar-section" aria-labelledby="skills-nav-title">
        <div className="sidebar-section-heading">
          <span id="skills-nav-title">技能库</span>
        </div>
        <nav className="sidebar-links" aria-label="技能库导航">
          <Link
            aria-current={mineActive ? undefined : "page"}
            className={`sidebar-link${mineActive ? "" : " is-active"}`}
            to="/skills"
          >
            <Icon name="spark" size={16} />
            <span>可用技能</span>
          </Link>
          <Link
            aria-current={mineActive ? "page" : undefined}
            className={`sidebar-link${mineActive ? " is-active" : ""}`}
            to="/skills?view=mine"
          >
            <Icon name="user" size={16} />
            <span>我的技能</span>
          </Link>
        </nav>
      </section>

      <div className="sidebar-guidance">
        <Icon name="book" size={17} />
        <p>公共技能由管理员发布，所有用户都可以在后续对话中按任务使用。</p>
      </div>
      <div className="sidebar-guidance">
        <Icon name="lock" size={17} />
        <p>
          我的技能只对你本人可见；启用后会进入你的可用技能目录，但不会自动加入当前对话。
        </p>
      </div>
    </div>
  );
}
